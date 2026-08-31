# Payments: Telegram Stars invoices and manual bank transfers.

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.functions.messages import SetBotPrecheckoutResultsRequest
from telethon.tl.types import (
    DataJSON,
    InputMediaInvoice,
    Invoice,
    LabeledPrice,
    MessageActionPaymentSentMe,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageService,
    UpdateBotPrecheckoutQuery,
    UpdateNewMessage,
)

from config import (
    INVOICE_CURRENCY,
    INVOICE_TTL_MINUTES,
    RECEIPT_MAX_BYTES,
    RECEIPT_MIME_HINTS,
    RECEIPT_SIGNATURES,
    RECEIPTS_DIR,
)
from db import connection, journal
from db import invoices as invoices_db
from db import orders as orders_db
from db import products as products_db
from db import refunds as refunds_db
from db import subscriptions as subs_db
from db import users as users_db
from handlers import catalog, common
from handlers.admin import cards
from services import billing, notify, stars
from utils import dates, keyboards, texts

logger = logging.getLogger(__name__)

# Telethon runs every update in its own task, so "no open order, create one" is a race
# unless the check and the insert are held together.
_order_lock = asyncio.Lock()


async def _resolve_stars_product(event, user: dict) -> Optional[dict]:
    product = await catalog.resolve_visible_product(user, common.callback_int(event))
    if not product:
        await event.answer("Product not found", alert=True)
        return None
    if not billing.can_pay_stars(product):
        await event.answer("This product is transfer only", alert=True)
        return None
    if int(product.get("duration_days") or 0) <= 0:
        logger.error("Product %s has an invalid subscription duration", product["id"])
        await event.answer("This product is temporarily unavailable", alert=True)
        return None
    return product


def _stars_terms_hash(product: dict, prompt: str) -> str:
    # The random quote token makes the callback unforgeable in practice; this digest
    # makes any change to what was shown detectable before the quote becomes payable.
    identity = {
        "id": int(product["id"]),
        "slug": product.get("slug") or "",
        "owner_user_id": int(product.get("owner_user_id") or 0),
        "name": product.get("name") or "",
        "emoji": product.get("emoji") or "📦",
        "price_stars": int(product.get("price_stars") or 0),
        "duration_days": int(product.get("duration_days") or 0),
        "prompt": prompt,
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _product_from_invoice(record: dict) -> dict:
    return {
        "id": record["product_id"],
        "slug": record["product_slug"],
        "name": record["product_name"],
        "emoji": record.get("emoji") or "📦",
        "owner_user_id": int(record.get("owner_user_id") or 0),
        "price_stars": int(record["amount_stars"]),
        "duration_days": int(record["duration_days"]),
    }


async def _show_stars_terms(event, user: dict, product: dict, notice: str = "") -> bool:
    async with users_db.lifecycle_lock(user["id"]):
        return await _show_stars_terms_locked(event, user, product, notice)


async def _show_stars_terms_locked(event, user: dict, product: dict, notice: str = "") -> bool:
    prompt = texts.stars_terms_prompt(product)
    quote_token = secrets.token_urlsafe(16)
    try:
        async with connection.transaction():
            current_user = await users_db.get_by_id(user["id"])
            if (
                not current_user
                or current_user["telegram_id"] != user["telegram_id"]
                or current_user["is_blocked"]
            ):
                quote_id = False
            else:
                quote_id = await invoices_db.create_quote(
                    current_user["telegram_id"],
                    product,
                    quote_token,
                    _stars_terms_hash(product, prompt),
                )
    except Exception as error:
        logger.error("Purchase terms could not be prepared: %s", error)
        await event.answer("Could not prepare the purchase", alert=True)
        return False
    if quote_id is False:
        await event.answer("This account can no longer make the purchase", alert=True)
        return False
    if quote_id is None:
        await event.answer("The invoice is already in the chat below", alert=True)
        return False
    await event.answer(notice or None, alert=bool(notice))
    await common.edit_or_reply(
        event,
        prompt,
        keyboards.stars_terms(product, quote_token),
        lifecycle_held=True,
    )
    return True


async def _confirm_stars_invoice(event, user: dict) -> None:
    # The caller holds this user's lifecycle lock through every database and Telegram
    # step. Erasure therefore either cleans up a fully sent invoice or wins first and
    # prevents any invoice or follow-up message from being sent.
    quote_token = common.callback_arg(event)
    invoice_id = None
    invoice_record = None
    refresh_product = None
    problem = None
    try:
        async with _order_lock:
            async with connection.transaction():
                quote = await invoices_db.get(quote_token)
                current_user = await users_db.get_by_id(user["id"])
                if (
                    not current_user
                    or current_user["telegram_id"] != user["telegram_id"]
                    or current_user["is_blocked"]
                ):
                    problem = "This account can no longer make the purchase."
                elif (
                    not quote
                    or quote["status"] != invoices_db.QUOTE
                    or quote["telegram_id"] != user["telegram_id"]
                ):
                    problem = "This confirmation has expired or was already used."
                else:
                    product = await catalog.resolve_visible_product(
                        current_user, quote["product_id"]
                    )
                    if (
                        not product
                        or not billing.can_pay_stars(product)
                        or int(product.get("duration_days") or 0) <= 0
                    ):
                        await invoices_db.cancel_quote(quote_token)
                        problem = "This product is no longer available."
                    else:
                        prompt = texts.stars_terms_prompt(product)
                        current_hash = _stars_terms_hash(product, prompt)
                        if current_hash != quote.get("terms_hash"):
                            await invoices_db.cancel_quote(quote_token)
                            refresh_product = product
                        elif await orders_db.open_for_product(
                            current_user["id"], product["slug"]
                        ):
                            await invoices_db.cancel_quote(quote_token)
                            problem = "An order for this product is still open."
                        else:
                            invoice_id = await invoices_db.activate_quote(
                                quote_token, current_user["telegram_id"], current_hash
                            )
                            if invoice_id is not None:
                                invoice_record = await invoices_db.get(quote_token)
                            else:
                                problem = (
                                    "This confirmation has expired or an invoice is "
                                    "already open."
                                )
        if refresh_product is not None:
            await _show_stars_terms_locked(
                event,
                user,
                refresh_product,
                "The product or terms changed. Please review them again.",
            )
            return
        if problem:
            await event.answer(problem, alert=True)
            return
        product = _product_from_invoice(invoice_record)
        await send_invoice(event.client, user["telegram_id"], product, quote_token)
    except Exception as error:
        logger.error("Invoice was not sent: %s", error)
        if invoice_id is not None:
            try:
                await invoices_db.cancel(quote_token)
            except Exception as cancel_error:
                logger.error(
                    "Failed to cancel unsent invoice %s: %s",
                    quote_token,
                    cancel_error,
                )
        await event.answer("Could not create the invoice", alert=True)
        return

    try:
        await event.client.send_message(
            user["telegram_id"],
            f"⏱ The invoice is payable for {INVOICE_TTL_MINUTES} minutes.",
            parse_mode="html",
        )
    except Exception as error:
        # The invoice itself was sent successfully; a missing explanatory note must not
        # invalidate a charge Telegram can still complete.
        logger.warning("Invoice expiry note was not sent: %s", error)
    try:
        await journal.event("pay_stars", user["telegram_id"], product["slug"])
    except Exception as error:
        logger.error("Invoice %s was sent but not journaled: %s", invoice_id, error)
    await event.answer("Invoice sent, see the message below")


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^pay_stars:(\d+)$"))
    async def pay_stars(event):
        user = await common.guard(event)
        if not user:
            return
        product = await _resolve_stars_product(event, user)
        if not product:
            return
        if await orders_db.open_for_product(user["id"], product["slug"]):
            await event.answer("An order for this product is still open", alert=True)
            return
        # This creates an expiring one-time quote, not a payable invoice. The next tap
        # can activate only the exact product and terms shown here.
        await _show_stars_terms(event, user, product)

    @client.on(events.CallbackQuery(pattern=rb"^pay_stars_confirm:(\d+):(\d+):(\d+)$"))
    async def retired_stars_confirmation(event):
        user = await common.guard(event)
        if not user:
            return
        product = await _resolve_stars_product(event, user)
        if not product:
            return
        await _show_stars_terms(
            event,
            user,
            product,
            "This confirmation has expired. Please review the current terms.",
        )

    @client.on(events.CallbackQuery(pattern=rb"^pay_stars_confirm:([A-Za-z0-9_-]{8,64})$"))
    async def pay_stars_confirm(event):
        user = await common.guard(event)
        if not user:
            return
        async with users_db.lifecycle_lock(user["id"]):
            await _confirm_stars_invoice(event, user)

    @client.on(events.CallbackQuery(pattern=rb"^pay_tr:"))
    async def pay_transfer(event):
        user = await common.guard(event)
        if not user:
            return
        product = await catalog.resolve_visible_product(user, common.callback_int(event))
        if not product:
            await event.answer("Product not found", alert=True)
            return
        await start_transfer(event, user, product)

    @client.on(events.CallbackQuery(pattern=rb"^cancel_order:"))
    async def cancel_order(event):
        user = await common.guard(event)
        if not user:
            return
        order = await orders_db.get(common.callback_int(event))
        if not order or order["user_id"] != user["id"]:
            await event.answer("Order not found", alert=True)
            return
        if not await orders_db.claim_status(
            order["id"], orders_db.CANCELLED, (orders_db.PENDING_RECEIPT,)
        ):
            await event.answer("This order can no longer be cancelled", alert=True)
            return
        await common.edit_or_reply(
            event, f"❌ Order #{order['id']} cancelled.", keyboards.back_home()
        )
        await event.answer()

    # Matched on the media type, not on e.photo/e.document: those fall through to the
    # image of a link preview, so any URL a customer pasted was accepted as a receipt.
    @client.on(
        events.NewMessage(
            incoming=True,
            func=lambda e: (
                e.is_private and isinstance(e.media, (MessageMediaPhoto, MessageMediaDocument))
            ),
        )
    )
    async def receipt(event):
        await handle_receipt(event)

    @client.on(events.Raw)
    async def raw_updates(event):
        if isinstance(event, UpdateBotPrecheckoutQuery):
            await handle_precheckout(client, event)
            return
        if isinstance(event, UpdateNewMessage) and isinstance(event.message, MessageService):
            action = event.message.action
            if isinstance(action, MessageActionPaymentSentMe):
                await handle_payment(client, event.message, action)


async def send_invoice(
    client: TelegramClient, telegram_id: int, product: dict, token: str
) -> None:
    title = f"{product['emoji']} {product['name']}"[:32]
    description = (
        product.get("short_description") or f"Subscription for {product['duration_days']} days"
    )[:255]

    invoice = InputMediaInvoice(
        title=title,
        description=description,
        invoice=Invoice(
            currency=INVOICE_CURRENCY,
            prices=[LabeledPrice(label=product["name"][:32], amount=product["price_stars"])],
        ),
        payload=f"buy:{product['id']}:{token}".encode(),
        provider="",
        provider_data=DataJSON(data="{}"),
    )
    await client.send_message(telegram_id, file=invoice)


def _parse_payload(payload: bytes) -> tuple[Optional[int], Optional[str]]:
    try:
        parts = payload.decode().split(":")
    except Exception:
        return None, None
    if len(parts) != 3 or parts[0] != "buy":
        return None, None
    try:
        product_id = int(parts[1])
    except ValueError:
        return None, None
    token = parts[2]
    if (
        product_id <= 0
        or not 8 <= len(token) <= 128
        or any(not (char.isalnum() or char in "-_") for char in token)
    ):
        return None, None
    return product_id, token


def _invoice_expired(record: dict, *, approved_window: bool = False) -> bool:
    approved = dates.parse(record.get("precheckout_approved_at"))
    if approved_window and record.get("precheckout_approved_at"):
        if approved is None:
            return True
        return dates.utcnow() - approved > timedelta(minutes=invoices_db.APPROVED_GRACE_MINUTES)
    created = dates.parse(record.get("created_at"))
    if created is None:
        return True
    return dates.utcnow() - created > timedelta(minutes=INVOICE_TTL_MINUTES)


def _invoice_problem(
    record: Optional[dict],
    telegram_id: int = None,
    product_id: int = None,
    currency: str = None,
    amount: int = None,
    *,
    check_expiry: bool = True,
) -> Optional[str]:
    if record is None:
        return "Invoice not found. Please start a new order."
    if telegram_id is not None and record["telegram_id"] != telegram_id:
        # A forwarded invoice must not be payable by somebody else.
        return "This invoice belongs to another account."
    if product_id is not None and record["product_id"] != product_id:
        return "The invoice product does not match the payment request."
    if record["status"] == invoices_db.CANCELLED:
        return "This invoice was cancelled. Please start a new order."
    if record["status"] == invoices_db.PAID:
        return "This invoice is already paid."
    if record["status"] != invoices_db.PENDING:
        return "This invoice is not payable. Please start a new order."
    if _invoice_expired(record, approved_window=not check_expiry):
        return "The payment window is closed. Please start a new order."
    if (
        not record.get("product_slug")
        or not record.get("product_name")
        or int(record.get("amount_stars") or 0) <= 0
        or int(record.get("duration_days") or 0) <= 0
    ):
        return "The invoice details are incomplete. Please start a new order."
    if record.get("currency") != INVOICE_CURRENCY:
        return "The invoice currency is invalid. Please start a new order."
    if currency is not None and currency != record["currency"]:
        return "The payment currency does not match the invoice."
    if amount is not None and int(amount) != int(record["amount_stars"]):
        return "The payment amount does not match the invoice."
    return None


async def _validate_invoice(
    product_id: int,
    token: str,
    telegram_id: int,
    currency: str,
    amount: int,
    *,
    check_expiry: bool = True,
) -> tuple[Optional[dict], Optional[dict], Optional[dict], Optional[str]]:
    record = await invoices_db.get(token)
    # Even a rejected payment needs a notice bound to the live payer's internal row.
    # Erasure scrubs invoice.telegram_id, so a later re-registration cannot revive an
    # old invoice through this lookup.
    user = await users_db.get(telegram_id)
    problem = _invoice_problem(
        record,
        telegram_id,
        product_id,
        currency,
        amount,
        check_expiry=check_expiry,
    )
    if problem:
        return record, user, None, problem

    if not user:
        return record, None, None, "The account for this invoice no longer exists."
    if user["is_blocked"]:
        return record, user, None, "Payments are disabled for this account."

    # Pre-checkout is the merchant's authorization boundary. Once it succeeds, honour
    # the immutable terms stored on the invoice even if an administrator edits or
    # retires the catalog row before Telegram delivers the paid update. Account removal
    # and blocking remain live checks because there would be no valid access recipient.
    if not check_expiry and record.get("precheckout_approved_at"):
        product = _product_from_invoice(record)
    else:
        product = await catalog.resolve_visible_product(user, record["product_id"])
        if (
            not product
            or int(product["id"]) != int(record["product_id"])
            or not product["is_active"]
            or product["owner_user_id"] not in (products_db.PUBLIC, user["id"])
            or int(product["owner_user_id"]) != int(record["owner_user_id"])
            or product["slug"] != record["product_slug"]
            or not billing.can_pay_stars(product)
        ):
            return record, user, product, "This product is no longer available."

    if await orders_db.open_for_product(user["id"], record["product_slug"]):
        return record, user, product, "An order for this product is still open."
    return record, user, product, None


async def handle_precheckout(client: TelegramClient, update) -> None:
    product_id, token = _parse_payload(update.payload)
    telegram_id = int(getattr(update, "user_id", 0) or 0)
    if not product_id or not token:
        problem = "Broken invoice."
    else:
        try:
            amount = int(getattr(update, "total_amount", 0) or 0)
        except (TypeError, ValueError):
            amount = -1
        user = await users_db.get(telegram_id)
        if not user:
            problem = "The account for this invoice no longer exists."
        else:
            # Keep erasure/blocking outside until Telegram has received our answer. The
            # database transaction makes every mutable validation check and the approval
            # stamp one indivisible decision; _order_lock gives transfer checkout the same
            # ordering boundary.
            async with users_db.lifecycle_lock(user["id"]):
                try:
                    async with _order_lock:
                        async with connection.transaction():
                            _record, _, _, problem = await _validate_invoice(
                                product_id,
                                token,
                                telegram_id,
                                str(getattr(update, "currency", "") or ""),
                                amount,
                            )
                            if problem is None and not await invoices_db.approve_precheckout(
                                token
                            ):
                                problem = (
                                    "The payment window is closed. Please start a new order."
                                )
                except Exception as error:
                    logger.exception("Could not validate pre-checkout query: %s", error)
                    problem = "The invoice could not be checked. Please try again."
                await _answer_precheckout(client, update, problem)
                return
    await _answer_precheckout(client, update, problem)


async def _answer_precheckout(client: TelegramClient, update, problem: str = None) -> None:
    try:
        await client(
            SetBotPrecheckoutResultsRequest(
                query_id=update.query_id,
                success=problem is None,
                error=problem,
            )
        )
    except Exception as error:
        logger.error("Pre-checkout failed: %s", error)


async def _refund_refused_payment(
    client,
    telegram_id: int,
    telegram_charge_id: Optional[str],
    provider_charge_id: Optional[str],
    reason: str,
    *,
    token: Optional[str] = None,
    amount: int = 0,
    currency: str = "",
    user_id: int = 0,
) -> None:
    # The obligation, invoice cancellation and audit event commit together before the
    # external RPC. A crash or failed Telegram call therefore leaves a retryable row.
    refund_record = None
    try:
        async with connection.transaction():
            refund_record = await refunds_db.create(
                telegram_id=telegram_id,
                user_id=user_id,
                source="automatic",
                reason=reason,
                telegram_charge_id=telegram_charge_id,
                provider_charge_id=provider_charge_id,
                amount_stars=amount,
                currency=currency or "XTR",
            )
            if token:
                await invoices_db.cancel(token)
            await journal.event(
                "payment_refused",
                telegram_id,
                {
                    "refund": refund_record["id"],
                    "telegram_charge": telegram_charge_id,
                    "provider_charge": provider_charge_id,
                    "amount": amount,
                    "currency": currency,
                    "reason": reason,
                },
            )
    except Exception as error:
        logger.error("Could not record refused payment %s: %s", telegram_charge_id, error)
        # The local object came from a transaction that just rolled back. Passing its id
        # on would make process_refund quietly find no row and skip the direct fallback.
        refund_record = None
        if token:
            try:
                await invoices_db.cancel(token)
            except Exception as cancel_error:
                logger.error(
                    "Could not cancel invoice %s after the refund transaction failed: %s",
                    token,
                    cancel_error,
                )

    try:
        await stars.refund(
            client,
            telegram_id,
            telegram_charge_id,
            reason,
            provider_charge_id=provider_charge_id,
            refund_id=refund_record["id"] if refund_record else None,
            amount_stars=amount,
            currency=currency or "XTR",
            user_id=user_id,
        )
    except ValueError as error:
        # Conflicting charge/customer identifiers are not a transient database outage.
        # Never risk a second refund for an obligation already on file.
        logger.error("Refund identity conflict for charge %s: %s", telegram_charge_id, error)
        await notify.to_user(
            client,
            telegram_id,
            f"⚠️ {reason}\n\nThe refund needs manual review. Use /paysupport.",
            expected_user_id=user_id,
        )
        await notify.to_staff(
            client,
            "⚠️ Conflicting identifiers stopped an automatic refund for "
            f"customer <code>{telegram_id}</code>. Charge "
            f"<code>{telegram_charge_id or '-'}</code> needs reconciliation.",
        )
    except Exception as error:
        # A database outage must not suppress the only chance to return money while the
        # update is in hand. This branch is intentionally best-effort and loudly logged.
        logger.exception("Could not queue refund for charge %s: %s", telegram_charge_id, error)
        returned = await stars.refund_charge(
            client,
            telegram_id,
            telegram_charge_id,
            provider_charge_id=provider_charge_id,
        )
        if not returned:
            await notify.to_staff(
                client,
                "⚠️ A refused payment could not be persisted or refunded. "
                f"Customer <code>{telegram_id}</code>, Telegram charge "
                f"<code>{telegram_charge_id or '-'}</code>, provider reference "
                f"<code>{provider_charge_id or '-'}</code>.",
            )


async def handle_payment(client: TelegramClient, message, action) -> None:
    telegram_id = getattr(message.peer_id, "user_id", None)
    if telegram_id is None:
        logger.error("Payment without a user id")
        return

    charge = getattr(action, "charge", None)
    telegram_charge_id = getattr(charge, "id", None)
    provider_charge_id = getattr(charge, "provider_charge_id", None)
    try:
        paid_amount = int(getattr(action, "total_amount", 0) or 0)
    except (TypeError, ValueError):
        paid_amount = -1
    currency = str(getattr(action, "currency", "") or "")
    product_id, token = _parse_payload(action.payload)
    if not product_id or not token or not telegram_charge_id:
        reason = "The payment data is incomplete."
        logger.error(
            "Payment with unreadable data: telegram_charge=%s provider_charge=%s",
            telegram_charge_id,
            provider_charge_id,
        )
        payer = await users_db.get(telegram_id)
        await _refund_refused_payment(
            client,
            telegram_id,
            telegram_charge_id,
            provider_charge_id,
            reason,
            token=token,
            amount=paid_amount,
            currency=currency,
            user_id=payer["id"] if payer else 0,
        )
        return

    # Telegram can redeliver unacknowledged updates after a reconnect. Match duplicate
    # charge identifiers before validating the open invoice so an acknowledged payment
    # is not refunded after fulfillment.
    duplicate = await orders_db.by_charge_id(telegram_charge_id)
    if duplicate:
        logger.info(
            "Payment charge %s redelivered, already recorded as order %s",
            telegram_charge_id,
            duplicate["id"],
        )
        if (
            duplicate.get("paid_at")
            and not duplicate.get("subscription_id")
            and not duplicate.get("reversed_at")
        ):
            try:
                result = await billing.apply_payment(duplicate)
            except Exception as error:
                logger.exception(
                    "Could not recover fulfilment for order %s: %s", duplicate["id"], error
                )
            else:
                product = billing.product_from_order(duplicate)
                try:
                    await journal.event(
                        "payment_fulfilment_recovered",
                        telegram_id,
                        {"order": duplicate["id"]},
                    )
                except Exception as error:
                    logger.error(
                        "Could not journal recovered order %s: %s", duplicate["id"], error
                    )
                await notify.to_user(
                    client,
                    telegram_id,
                    texts.payment_done(
                        duplicate["id"],
                        texts.product_title(product),
                        result["subscription"]["expires_at"],
                        result["renewed"],
                    ),
                    keyboards.after_payment(),
                    expected_user_id=duplicate["user_id"],
                )
                fresh = await orders_db.get(duplicate["id"])
                await notify.to_staff(
                    client, cards.order_card(fresh), cards.order_buttons(fresh)
                )
        return

    try:
        record, user, product, problem = await _validate_invoice(
            product_id,
            token,
            telegram_id,
            currency,
            paid_amount,
            # Telegram has already accepted this charge after our pre-checkout
            # approval. The update may arrive just after the local TTL boundary; all
            # identity, state, amount and product checks still apply, but age alone
            # must not turn an approved payment into an automatic refund.
            check_expiry=False,
        )
    except Exception as error:
        logger.exception("Could not validate paid invoice %s: %s", token, error)
        record = user = None
        problem = "A technical problem stopped the order."
    if problem:
        await _refund_refused_payment(
            client,
            telegram_id,
            telegram_charge_id,
            provider_charge_id,
            problem,
            token=token,
            amount=paid_amount,
            currency=currency,
            user_id=user["id"] if user else 0,
        )
        return

    # Fulfil and report from the terms the customer accepted, not from a catalog row an
    # administrator may have edited while Telegram's invoice was still payable.
    snapshot = {
        "id": record["product_id"],
        "slug": record["product_slug"],
        "name": record["product_name"],
        "emoji": record.get("emoji") or "📦",
        "owner_user_id": int(record.get("owner_user_id") or 0),
        "duration_days": int(record["duration_days"]),
    }

    # The lock covers only the database decision. Refunds and replies talk to Telegram
    # and can sleep for a minute on a flood wait; the lock is global, so holding it
    # across them would queue every other customer's purchase behind one round trip.
    refuse = None
    order_id = None
    claimed = False
    duplicate = False
    async with _order_lock:
        try:
            # Recheck every mutable part of the decision under the same database write
            # transaction. In particular, privacy erasure can finish after invoice
            # validation; creating an order against that erased row would leave no valid
            # Telegram recipient for either access or a refund.
            async with connection.transaction():
                current_user = await users_db.get_by_id(user["id"])
                if (
                    not current_user
                    or int(current_user.get("telegram_id") or 0) != int(telegram_id)
                    or current_user["is_blocked"]
                ):
                    refuse = "This account can no longer receive the order."
                elif await orders_db.by_charge_id(telegram_charge_id):
                    duplicate = True
                elif await orders_db.open_for_product(user["id"], snapshot["slug"]):
                    refuse = "An order for this product is still open."
                else:
                    existing = await subs_db.active_for_slug(user["id"], snapshot["slug"])
                    # Claiming the invoice and recording the order are one unit. Apart,
                    # a crash between them leaves a paid invoice with no order behind it.
                    claimed = await invoices_db.claim_for_payment(token)
                    if claimed:
                        order_id = await orders_db.create(
                            user_id=user["id"],
                            product=snapshot,
                            amount_stars=paid_amount,
                            amount_rub=0,
                            payment_method="stars",
                            status=orders_db.PAID,
                            is_personal=bool(snapshot["owner_user_id"]),
                            is_renewal=bool(existing),
                            payment_charge_id=telegram_charge_id,
                            payment_provider_charge_id=provider_charge_id,
                            payment_recipient_id=telegram_id,
                        )
        except sqlite3.IntegrityError:
            # The unique index on the charge id caught a concurrent duplicate.
            if await orders_db.by_charge_id(telegram_charge_id):
                duplicate = True
            else:
                logger.exception("Payment could not be recorded because of a data conflict")
                claimed = False
                refuse = "A technical problem stopped the order."
        except Exception as error:
            # Attempt a refund even when local payment journaling fails.
            logger.exception("Could not record payment %s: %s", telegram_charge_id, error)
            claimed = False
            refuse = "A technical problem stopped the order."

        if duplicate:
            logger.info("Payment charge %s already recorded, skipping", telegram_charge_id)
            return
        if not claimed and not refuse:
            # Somebody claimed it first. Another delivery of this same charge is a
            # duplicate and the winner handles it.
            if await orders_db.by_charge_id(telegram_charge_id):
                return
            refuse = "This invoice is already paid."

    if refuse:
        await _refund_refused_payment(
            client,
            telegram_id,
            telegram_charge_id,
            provider_charge_id,
            refuse,
            token=token,
            amount=paid_amount,
            currency=currency,
            user_id=user["id"] if user else 0,
        )
        return

    order = await orders_db.get(order_id)
    try:
        result = await billing.apply_payment(order)
    except Exception as error:
        # The paid order remains in the staff queue and keeps both charge identifiers,
        # so it can be fulfilled or refunded instead of disappearing on redelivery.
        logger.exception("Could not fulfil paid order %s: %s", order_id, error)
        try:
            await journal.event("payment_fulfilment_failed", telegram_id, {"order": order_id})
        except Exception as journal_error:
            logger.error("Could not journal failed order %s: %s", order_id, journal_error)
        await notify.to_user(
            client,
            telegram_id,
            f"⚠️ Payment for order #{order_id} was recorded, but access could not be "
            "prepared automatically. Use /paysupport; do not pay again.",
            keyboards.support_menu(),
            expected_user_id=order["user_id"],
        )
        await notify.to_staff(client, cards.order_card(order), cards.order_buttons(order))
        return
    try:
        await journal.event("payment", telegram_id, {"order": order_id, "method": "stars"})
    except Exception as error:
        logger.error("Could not journal completed payment for order %s: %s", order_id, error)

    await notify.to_user(
        client,
        telegram_id,
        texts.payment_done(
            order_id,
            texts.product_title(snapshot),
            result["subscription"]["expires_at"],
            result["renewed"],
        ),
        keyboards.after_payment(),
        expected_user_id=order["user_id"],
    )
    order = await orders_db.get(order_id)
    await notify.to_staff(client, cards.order_card(order), cards.order_buttons(order))


async def start_transfer(event, user: dict, product: dict) -> None:
    async with users_db.lifecycle_lock(user["id"]):
        method = None
        current_product = None
        current_user = None
        open_order = None
        existing = None
        pending_invoice = False
        order_id = None
        async with _order_lock:
            async with connection.transaction():
                current_user = await users_db.get_by_id(user["id"])
                if (
                    current_user
                    and current_user["telegram_id"] == user["telegram_id"]
                    and not current_user["is_blocked"]
                ):
                    current_product = await catalog.resolve_visible_product(
                        current_user, product["id"]
                    )
                if current_product:
                    pending_invoice = await invoices_db.has_pending_for_product(
                        current_user["telegram_id"], current_product["id"]
                    )
                    method = await billing.transfer_method(current_user, current_product)
                if method and not pending_invoice:
                    amount = billing.rub_price(current_product)
                    open_order = await orders_db.open_for_product(
                        current_user["id"], current_product["slug"]
                    )
                    if not open_order:
                        existing = await subs_db.active_for_slug(
                            current_user["id"], current_product["slug"]
                        )
                        order_id = await orders_db.create(
                            user_id=current_user["id"],
                            product=current_product,
                            amount_stars=0,
                            amount_rub=amount,
                            payment_method="transfer",
                            payment_method_id=method["id"],
                            status=orders_db.PENDING_RECEIPT,
                            is_personal=bool(current_product["owner_user_id"]),
                            is_renewal=bool(existing),
                        )

        if not current_user or current_user["telegram_id"] != user["telegram_id"]:
            await event.answer("This account can no longer make the purchase", alert=True)
            return
        if not method:
            await event.answer("Bank transfer is not available for you", alert=True)
            return
        if pending_invoice:
            await event.answer("A Stars invoice for this product is already open", alert=True)
            return
        if open_order:
            await event.answer(f"Order #{open_order['id']} is still open", alert=True)
            return

        await journal.event(
            "pay_transfer", current_user["telegram_id"], current_product["slug"]
        )

        text = texts.transfer_instructions(current_product, amount, method, order_id)
        if existing:
            text = "🔄 <b>Renewal</b>\n\n" + text
        try:
            await common.reply(
                event,
                text,
                buttons=keyboards.waiting_receipt(order_id),
                lifecycle_held=True,
            )
        except Exception as error:
            # The order exists, so the customer must not be left without payment details.
            logger.error(
                "Transfer instructions were not sent for order %s: %s", order_id, error
            )
            await orders_db.claim_status(
                order_id, orders_db.CANCELLED, (orders_db.PENDING_RECEIPT,)
            )
            await event.answer("Could not show the payment details, try again", alert=True)
            return
        await event.answer()


def _clean_partial(stem: Path) -> None:
    # Remove any partial file sharing the generated temporary stem.
    for leftover in stem.parent.glob(stem.name + "*"):
        leftover.unlink(missing_ok=True)


def _receipt_extension(head: bytes) -> Optional[str]:
    for signature, extension in RECEIPT_SIGNATURES:
        if head.startswith(signature):
            return extension
    return None


async def handle_receipt(event) -> None:
    sender = await event.get_sender()
    if sender is None:
        return
    user = await users_db.get(sender.id)
    # Blocked users cannot submit receipts to staff.
    if not user or user["is_blocked"]:
        return
    event._narromarket_user_id = user["id"]

    # A bare file carries no order id. Accept it only when there is exactly one possible
    # destination; a transfer that timed out in the last week remains a candidate because
    # the customer may have paid on time and sent the document late.
    candidates = await orders_db.receipt_candidates(user["id"])
    if not candidates:
        await common.reply(
            event,
            "❌ There is no open order waiting for a receipt. Open My orders or contact "
            "payment support if the transfer has already left your account.",
            buttons=keyboards.support_menu(),
        )
        return
    if len(candidates) > 1:
        await common.reply(
            event,
            f"⚠️ You have {len(candidates)} orders that could take this receipt. I cannot tell which "
            "order this file belongs to. Open My orders and cancel the extra order first, "
            "then send the receipt again.",
            buttons=[
                [keyboards.Button.inline(keyboards.BTN_ORDERS, "menu:orders")],
                *keyboards.back_home(),
            ],
        )
        return
    order = candidates[0]

    if event.document:
        mime = str(event.document.mime_type or "").lower()
        # The mime type only filters obvious mistakes. What the file really is gets
        # decided from its first bytes after the download, and the size limit applies to
        # photos too, not just documents.
        if not any(hint in mime for hint in RECEIPT_MIME_HINTS):
            await common.reply(event, "❌ Only photos and PDF files are accepted.")
            return
        if (event.document.size or 0) > RECEIPT_MAX_BYTES:
            await common.reply(event, "❌ The file is over 5 MB. Send a lighter screenshot.")
            return

    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = RECEIPTS_DIR / (f"incoming_{order['id']}_{int(time.time())}_{secrets.token_hex(4)}")
    try:
        # Telethon appends the extension it guessed when the path it is given has none,
        # so the only reliable name is the one it returns.
        written = await event.download_media(str(stem))
    except Exception as error:
        logger.error("Receipt was not saved: %s", error)
        _clean_partial(stem)
        await common.reply(event, "❌ Could not save the file, please try again.")
        return
    if not written:
        logger.error("Receipt download returned nothing for order %s", order["id"])
        _clean_partial(stem)
        await common.reply(event, "❌ Could not save the file, please try again.")
        return

    temporary = Path(written)
    try:
        if temporary.stat().st_size > RECEIPT_MAX_BYTES:
            temporary.unlink(missing_ok=True)
            await common.reply(event, "❌ The file is over 5 MB. Send a lighter screenshot.")
            return
        with open(temporary, "rb") as handle:
            head = handle.read(16)
        extension = _receipt_extension(head)
        if extension is None:
            # The mime type is whatever the sending client claimed. The bytes are not.
            temporary.unlink(missing_ok=True)
            await common.reply(event, "❌ That file is not a photo or a PDF.")
            return
        filename = f"receipt_{order['id']}_{int(time.time())}_{secrets.token_hex(3)}{extension}"
        path = RECEIPTS_DIR / filename
        temporary.replace(path)
    except OSError as error:
        logger.error("Receipt was not checked: %s", error)
        temporary.unlink(missing_ok=True)
        await common.reply(event, "❌ Could not save the file, please try again.")
        return

    # One compare-and-swap: staff never see an order under review without the file that
    # belongs to it, and a recent timeout can be reopened without a separate race window.
    claimed = await orders_db.attach_receipt(
        order["id"], filename, user["id"], user["telegram_id"]
    )
    if not claimed:
        # Nothing points at the file any more, so it must not be left behind.
        path.unlink(missing_ok=True)
        await common.reply(event, "❌ This order is no longer waiting for a receipt.")
        return

    await common.reply(
        event,
        f"✅ Receipt received. Order #{order['id']} is under review, "
        "this usually takes up to 24 hours.",
        buttons=keyboards.back_home(),
    )

    fresh = await orders_db.get(order["id"])
    card = cards.order_card(fresh)
    buttons = cards.order_buttons(fresh)
    for recipient in await users_db.staff_recipients():
        async with users_db.lifecycle_lock(recipient["id"]):
            current = await users_db.get_by_id(recipient["id"])
            if (
                not current
                or current["telegram_id"] != recipient["telegram_id"]
                or current["is_blocked"]
                or current["role"] not in ("manager", "admin", "owner")
            ):
                continue
            try:
                await event.client.send_file(
                    current["telegram_id"],
                    str(path),
                    caption=texts.clamp(card, texts.CAPTION_LIMIT),
                    buttons=buttons,
                    parse_mode="html",
                )
            except Exception as error:
                logger.warning("Receipt not delivered to %s: %s", current["telegram_id"], error)
