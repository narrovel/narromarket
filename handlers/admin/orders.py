# Order and subscription handling for staff.

import logging

from telethon import Button, TelegramClient, events

from db import connection, journal
from db import orders as orders_db
from db import products as products_db
from db import refunds as refunds_db
from db import subscriptions as subs_db
from db import users as users_db
from handlers.admin import base, cards
from handlers.admin.input import on_state
from services import billing, notify, stars
from utils import dates, keyboards, states, texts

logger = logging.getLogger(__name__)

# Enough to page through comfortably without materialising an unbounded join.
QUEUE_LIMIT = 200


def target_id(event) -> int:
    try:
        return int(event.data.decode().split(":")[2])
    except (ValueError, IndexError):
        return 0


def _refund_recipient(order: dict) -> int:
    recorded = int(order.get("payment_recipient_id") or 0)
    if recorded > 0:
        return recorded
    current = int(order.get("telegram_id") or 0)
    return current if current > 0 else 0


async def _notify_refund_customer(client, refund: dict, message: str, order=None) -> bool:
    """Send financial news only while the matching customer profile is still live."""
    if order is None and refund.get("order_id"):
        order = await orders_db.get(refund["order_id"])
    user_id = int(refund.get("user_id") or 0)
    if order:
        if user_id and user_id != int(order["user_id"]):
            return False
        user_id = int(order["user_id"])
    if user_id <= 0:
        return False

    async with users_db.lifecycle_lock(user_id):
        current = await users_db.get_by_id(user_id)
        if (
            not current
            or current["telegram_id"] <= 0
            or current["telegram_id"] != int(refund.get("telegram_id") or 0)
        ):
            return False
        return await notify.to_user(
            client, current["telegram_id"], message, lifecycle_held=True
        )


def _subscription_effect(subscription) -> str:
    # What happened to the subscription after a refund or a cancellation.
    if not subscription:
        return "No subscription was granted by this order."
    if subscription["status"] == subs_db.CANCELLED:
        return "The subscription was closed."
    return f"The subscription now runs until {dates.fmt_date(subscription['expires_at'])}."


async def _finalize_order_refund(refund: dict, actor_id: int):
    """Revoke access once and close the order after the refund is confirmed."""
    if not refund.get("order_id"):
        return None, None
    order = await orders_db.get(refund["order_id"])
    if not order:
        return None, None
    subscription = await billing.revoke_payment(order)
    claimed = await orders_db.claim_status(
        order["id"],
        orders_db.REFUNDED,
        (orders_db.REFUND_PENDING,),
        processed_by=actor_id,
    )
    if claimed:
        await journal.action(
            actor_id,
            "order_refund_done",
            f"order:{order['id']}",
            f"refund:{refund['id']} resolution={refund.get('resolution') or 'unknown'}",
        )
    return await orders_db.get(order["id"]), subscription


def _refund_buttons(refund: dict) -> list:
    return [
        [Button.inline("🔁 Retry", f"a:refundretry:{refund['id']}")],
        [Button.inline("✅ Mark returned", f"a:refunddoneask:{refund['id']}")],
        [Button.inline("◀️ Refund queue", "a:refunds")],
    ]


def _refund_summary(refund: dict) -> str:
    amount = (
        f"{refund['amount_stars']}⭐"
        if refund["payment_method"] == "stars"
        else f"{refund['amount_rub']}₽"
    )
    error = f"\nError: {texts.escape(refund['last_error'])}" if refund.get("last_error") else ""
    order = f"order #{refund['order_id']}" if refund.get("order_id") else "refused payment"
    return (
        f"💸 <b>Refund #{refund['id']}</b> · {order}\n"
        f"Customer: <code>{refund['telegram_id']}</code> · {amount}\n"
        f"Status: {refund['status']} · attempts: {refund['attempts']}{error}"
    )


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^a:orders(:\d+)?$"))
    async def orders_list(event):
        if not await base.actor(event, "orders"):
            return
        total = await orders_db.count_needs_attention()
        refund_total = await refunds_db.count_unresolved()
        items = await orders_db.needs_attention(limit=QUEUE_LIMIT)
        if not items and not refund_total:
            await base.show(event, "✅ Nothing waiting for staff right now.", [base.home_row()])
            await event.answer()
            return

        page = base.page_from(event, 2)
        lines = [f"📋 <b>Orders in progress: {total}</b>"]
        if refund_total:
            lines.append(f"💸 Refunds waiting: {refund_total}")
        lines.append("")
        if total > len(items):
            lines.append(f"<i>Showing the oldest {len(items)}.</i>")
            lines.append("")
        for order in base.page_slice(items, page):
            lines.append(
                f"#{order['id']} {texts.escape(order['emoji'])} "
                f"{texts.escape(order['product_name'])} - "
                f"{orders_db.status_label(order['status'])} | {users_db.display_name(order)}"
            )
        rows = base.paged_rows(
            items,
            page,
            "a:orders",
            lambda order: f"#{order['id']} {order['product_name'][:20]}",
            lambda order: f"a:order:{order['id']}",
        )
        if refund_total:
            rows.append([Button.inline(f"💸 Refunds waiting: {refund_total}", "a:refunds")])
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:order:(\d+)$"))
    async def order_card(event):
        if not await base.actor(event, "orders"):
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        await base.show(event, cards.order_card(order), cards.order_buttons(order))
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:confirm:(\d+)$"))
    async def confirm_transfer(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        recovering = bool(order.get("paid_at") and not order.get("subscription_id"))
        if order.get("paid_at") and not recovering:
            await event.answer("Payment is already fulfilled", alert=True)
            return
        if recovering:
            if order["status"] not in (orders_db.PAID, orders_db.PROBLEM):
                await event.answer("This order cannot be fulfilled", alert=True)
                return
            if order["status"] == orders_db.PROBLEM and not await orders_db.claim_status(
                order["id"],
                orders_db.PAID,
                (orders_db.PROBLEM,),
                processed_by=user["telegram_id"],
            ):
                await event.answer("Order is already being handled", alert=True)
                return
        # Claimed, not checked and then written: two managers tapping Confirm at the
        # same time both passed the check and the period was granted twice.
        # PROBLEM includes receipts parked by the nightly stale-review sweep.
        if not recovering and not await orders_db.claim_status(
            order["id"],
            orders_db.PAID,
            (orders_db.PENDING_REVIEW, orders_db.PROBLEM),
            processed_by=user["telegram_id"],
        ):
            await event.answer("Order is already handled", alert=True)
            return
        order = await orders_db.get(order["id"])
        try:
            result = await billing.apply_payment(order)
        except Exception:
            logger.exception("Could not fulfil order %s", order["id"])
            await orders_db.claim_status(
                order["id"],
                orders_db.PROBLEM,
                (orders_db.PAID,),
                processed_by=user["telegram_id"],
            )
            await event.answer("Payment is recorded, but fulfilment failed", alert=True)
            failed = await orders_db.get(order["id"])
            await base.show(event, cards.order_card(failed), cards.order_buttons(failed))
            return
        await journal.action(user["telegram_id"], "order_confirm", f"order:{order['id']}")

        await notify.to_user(
            event.client,
            order["telegram_id"],
            texts.payment_done(
                order["id"],
                texts.product_title(order),
                result["subscription"]["expires_at"],
                result["renewed"],
            ),
            keyboards.after_payment(),
            expected_user_id=order["user_id"],
        )
        order = await orders_db.get(order["id"])
        await event.answer("Payment confirmed")
        await base.show(event, cards.order_card(order), cards.order_buttons(order))

    @client.on(events.CallbackQuery(pattern=rb"^a:reject:(\d+)$"))
    async def reject_transfer(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        if order.get("paid_at"):
            await event.answer("This order was paid, use Refund", alert=True)
            return

        # Only an unhandled receipt can be rejected, and any period it already granted
        # goes back. Without both, a stale Reject button on a confirmed order left the
        # customer with a working subscription and no order to show for it.
        if not await orders_db.claim_status(
            order["id"],
            orders_db.REJECTED,
            (orders_db.PENDING_REVIEW, orders_db.PROBLEM),
            processed_by=user["telegram_id"],
        ):
            await event.answer("Order is already handled", alert=True)
            return
        subscription = await billing.revoke_payment(order)
        await journal.action(user["telegram_id"], "order_reject", f"order:{order['id']}")
        await notify.to_user(
            event.client,
            order["telegram_id"],
            f"❌ <b>Order #{order['id']} rejected</b>\n\n"
            "We could not confirm the payment. Contact the manager if this looks wrong.\n"
            f"{_subscription_effect(subscription)}",
            expected_user_id=order["user_id"],
        )
        await event.answer("Order rejected")
        await base.show(
            event,
            f"❌ Order #{order['id']} rejected. " + _subscription_effect(subscription),
            [base.home_row()],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:send:(\d+)$"))
    async def send_credentials(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        if (
            not order.get("paid_at")
            or not order.get("subscription_id")
            or order["status"] not in (orders_db.PAID, orders_db.DELIVERED, orders_db.PROBLEM)
        ):
            await event.answer("Payment and an active subscription are required", alert=True)
            return
        current = ""
        subscription = await subs_db.get(order["subscription_id"])
        if not subscription or subscription["status"] != subs_db.ACTIVE:
            await event.answer("This order has no active subscription", alert=True)
            return
        if subscription["credentials"]:
            current = (
                f"\n\nCurrent value:\n<code>{texts.escape(subscription['credentials'])}</code>"
            )
        await base.ask(
            event,
            "order_credentials",
            "orders",
            f"📩 Access details for order #{order['id']} "
            f"({texts.escape(order['product_name'])}, {users_db.display_name(order)}).{current}",
            order_id=order["id"],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:done:(\d+)$"))
    async def complete_order(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        if not order.get("paid_at") or not order.get("subscription_id"):
            await event.answer("This order has not been fulfilled", alert=True)
            return
        # A refunded or cancelled order must not be walked back into the paid statuses,
        # where it would be counted as collected revenue a second time.
        if not await orders_db.claim_status(
            order["id"],
            orders_db.COMPLETED,
            (orders_db.DELIVERED, orders_db.PROBLEM),
            processed_by=user["telegram_id"],
        ):
            await event.answer("Order is already closed", alert=True)
            return
        await journal.action(user["telegram_id"], "order_complete", f"order:{order['id']}")
        order = await orders_db.get(order["id"])
        await event.answer("Order completed")
        await base.show(event, cards.order_card(order), cards.order_buttons(order))

    @client.on(events.CallbackQuery(pattern=rb"^a:refundask:(\d+)$"))
    async def refund_confirmation(event):
        if not await base.actor(event, "orders"):
            return
        order = await orders_db.get(target_id(event))
        if not order or not order.get("paid_at"):
            await event.answer("Paid order not found", alert=True)
            return
        if order["payment_method"] == "stars" and (
            not order.get("payment_charge_id") or not _refund_recipient(order)
        ):
            await event.answer(
                "This legacy order has no recorded refund recipient. Reconcile it manually.",
                alert=True,
            )
            return
        if order["status"] == orders_db.REFUND_PENDING:
            refund = await refunds_db.for_order(order["id"])
            if refund:
                await base.show(event, _refund_summary(refund), _refund_buttons(refund))
                await event.answer()
                return
        if (
            order["status"] in orders_db.FINAL_STATUSES
            and order["status"] != orders_db.COMPLETED
        ):
            await event.answer("Order is already closed", alert=True)
            return
        amount = (
            f"{order['amount_stars']}⭐"
            if order["payment_method"] == "stars"
            else f"{order['amount_rub']}₽"
        )
        await base.show(
            event,
            f"💸 <b>Refund order #{order['id']}?</b>\n\n"
            f"Return {amount} to {users_db.display_name(order)}. Access granted by this "
            "order is adjusted only after the return is confirmed.",
            [
                [Button.inline("💸 Confirm refund", f"a:refundgo:{order['id']}")],
                [Button.inline("◀️ Back", f"a:order:{order['id']}")],
            ],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:refund:(\d+)$"))
    async def retired_refund_button(event):
        # Old messages still in staff chats must remain usable, but never skip the new
        # confirmation step.
        await refund_confirmation(event)

    @client.on(events.CallbackQuery(pattern=rb"^a:refundgo:(\d+)$"))
    async def refund_order(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        if not order["paid_at"]:
            await event.answer("Nothing was paid, use Reject", alert=True)
            return
        refund_recipient = _refund_recipient(order)
        if order["payment_method"] == "stars" and (
            not order.get("payment_charge_id") or not refund_recipient
        ):
            await event.answer(
                "This legacy order has no recorded refund recipient. Reconcile it manually.",
                alert=True,
            )
            return

        refund = await refunds_db.for_order(order["id"])
        claimed = False
        if order["status"] != orders_db.REFUND_PENDING:
            # Persist the obligation and the non-final order state together. Revenue
            # and access remain intact until Telegram (or staff) confirms the return.
            async with connection.transaction():
                claimed = await orders_db.claim_status(
                    order["id"],
                    orders_db.REFUND_PENDING,
                    (
                        orders_db.PAID,
                        orders_db.DELIVERED,
                        orders_db.COMPLETED,
                        orders_db.PROBLEM,
                    ),
                    processed_by=user["telegram_id"],
                )
                if claimed:
                    refund = await refunds_db.create(
                        order_id=order["id"],
                        telegram_id=refund_recipient,
                        source="admin",
                        reason=f"Refund requested for order #{order['id']}",
                        telegram_charge_id=order.get("payment_charge_id"),
                        provider_charge_id=order.get("payment_provider_charge_id"),
                        payment_method=order["payment_method"],
                        amount_stars=order["amount_stars"],
                        amount_rub=order["amount_rub"],
                        currency="XTR" if order["payment_method"] == "stars" else "RUB",
                    )
                if claimed:
                    await journal.action(
                        user["telegram_id"],
                        "order_refund",
                        f"order:{order['id']}",
                        f"PENDING refund:{refund['id']} stars={order['amount_stars']} "
                        f"rub={order['amount_rub']}",
                    )
        if not refund or (not claimed and order["status"] != orders_db.REFUND_PENDING):
            await event.answer("Order is already closed", alert=True)
            return

        returned = False
        subscription = None
        if refund["payment_method"] == "stars":
            returned = await stars.process_refund(event.client, refund["id"])
        if returned:
            refund = await refunds_db.get(refund["id"])
            _, subscription = await _finalize_order_refund(refund, user["telegram_id"])

        tail = (
            "💰 Your Telegram Stars have been refunded."
            if returned
            else "The refund is recorded and waiting for staff confirmation."
        )
        await _notify_refund_customer(
            event.client,
            refund,
            (
                f"💸 <b>Refund for order #{order['id']}</b>\n\n"
                f"📦 {texts.escape(order['emoji'])} {texts.escape(order['product_name'])}\n\n"
                f"{tail}" + (f"\n{_subscription_effect(subscription)}" if returned else "")
            ),
            order,
        )
        if returned:
            await base.show(
                event,
                f"💸 Order #{order['id']} refunded and stars returned. "
                + _subscription_effect(subscription),
                [base.home_row()],
            )
        else:
            refund = await refunds_db.get(refund["id"])
            await base.show(event, _refund_summary(refund), _refund_buttons(refund))
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:refunds$"))
    async def refund_queue(event):
        if not await base.actor(event, "orders"):
            return
        items = await refunds_db.unresolved(limit=18)
        if not items:
            await base.show(event, "✅ No refunds are waiting.", [base.home_row()])
            await event.answer()
            return
        lines = [f"💸 <b>Refunds waiting: {await refunds_db.count_unresolved()}</b>", ""]
        rows = []
        for refund in items:
            order = f"order #{refund['order_id']}" if refund.get("order_id") else "payment"
            lines.append(
                f"#{refund['id']} · {order} · <code>{refund['telegram_id']}</code> · "
                f"{refund['status']}"
            )
            rows.append(
                [
                    Button.inline(
                        f"Refund #{refund['id']} · {order}", f"a:refundjob:{refund['id']}"
                    )
                ]
            )
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:refundjob:(\d+)$"))
    async def refund_card(event):
        if not await base.actor(event, "orders"):
            return
        refund = await refunds_db.get(target_id(event))
        if not refund:
            await event.answer("Refund not found", alert=True)
            return
        await base.show(event, _refund_summary(refund), _refund_buttons(refund))
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:refundretry:(\d+)$"))
    async def retry_refund(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        refund = await refunds_db.get(target_id(event))
        if not refund:
            await event.answer("Refund not found", alert=True)
            return
        was_completed = refund["status"] == refunds_db.COMPLETED
        returned = await stars.process_refund(event.client, refund["id"])
        refund = await refunds_db.get(refund["id"])
        if returned:
            order, subscription = await _finalize_order_refund(refund, user["telegram_id"])
            if not was_completed:
                await journal.action(
                    user["telegram_id"],
                    "refund_retry_done",
                    f"refund:{refund['id']}",
                    f"resolution={refund.get('resolution')}",
                )
                await _notify_refund_customer(
                    event.client,
                    refund,
                    f"💰 Refund #{refund['id']} has been completed."
                    + (
                        f"\n{_subscription_effect(subscription)}"
                        if order
                        else " Your Telegram Stars have been refunded."
                    ),
                    order,
                )
            await event.answer("Refund completed")
            await base.show(
                event,
                f"✅ Refund #{refund['id']} is completed.",
                [base.home_row()],
            )
            return
        refund = await refunds_db.get(refund["id"])
        await event.answer("Refund is still pending", alert=True)
        await base.show(event, _refund_summary(refund), _refund_buttons(refund))

    @client.on(events.CallbackQuery(pattern=rb"^a:refunddoneask:(\d+)$"))
    async def confirm_manual_refund(event):
        if not await base.actor(event, "orders"):
            return
        refund = await refunds_db.get(target_id(event))
        if not refund:
            await event.answer("Refund not found", alert=True)
            return
        if refund["status"] == refunds_db.COMPLETED:
            await event.answer("Refund is already completed", alert=True)
            return
        await base.show(
            event,
            f"💸 <b>Confirm refund #{refund['id']} was returned manually?</b>\n\n"
            "Use this only after checking the transfer outside the bot. The order and "
            "access will be adjusted immediately.",
            [
                [Button.inline("✅ Money was returned", f"a:refunddone:{refund['id']}")],
                [Button.inline("◀️ Back", f"a:refundjob:{refund['id']}")],
            ],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:refunddone:(\d+)$"))
    async def complete_refund_manually(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        refund = await refunds_db.get(target_id(event))
        if not refund:
            await event.answer("Refund not found", alert=True)
            return
        was_completed = refund["status"] == refunds_db.COMPLETED
        if not await refunds_db.complete_manually(refund["id"]):
            await event.answer("Another refund attempt is still running", alert=True)
            return
        refund = await refunds_db.get(refund["id"])
        order, subscription = await _finalize_order_refund(refund, user["telegram_id"])
        if not was_completed:
            await journal.action(
                user["telegram_id"],
                "refund_manual_done",
                f"refund:{refund['id']}",
                "confirmed by staff",
            )
            await _notify_refund_customer(
                event.client,
                refund,
                f"💰 Refund #{refund['id']} has been completed."
                + (f"\n{_subscription_effect(subscription)}" if order else ""),
                order,
            )
        await event.answer("Refund completed")
        await base.show(
            event,
            f"✅ Refund #{refund['id']} marked as returned.",
            [base.home_row()],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:cancel:(\d+)$"))
    async def cancel_order(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        order = await orders_db.get(target_id(event))
        if not order:
            await event.answer("Order not found", alert=True)
            return
        if order["paid_at"]:
            # Cancelling would take the order out of the books while the money stays
            # with the shop and the customer hears nothing about it.
            await event.answer("This order was paid, use Refund", alert=True)
            return
        expected = tuple(
            status
            for status in orders_db.STATUS_TITLES
            if status not in orders_db.FINAL_STATUSES
        )
        changed = await orders_db.cancel_unpaid(order["id"], expected, user["telegram_id"])
        if not changed:
            await event.answer("Order is closed or already paid", alert=True)
            return
        subscription = await billing.revoke_payment(order)
        await journal.action(user["telegram_id"], "order_cancel", f"order:{order['id']}")
        await notify.to_user(
            event.client,
            order["telegram_id"],
            f"❌ <b>Order #{order['id']} cancelled</b>\n\n"
            f"📦 {texts.escape(order['emoji'])} {texts.escape(order['product_name'])}\n"
            f"{_subscription_effect(subscription)}\n\n"
            "Reach out to the manager if you have questions.",
            expected_user_id=order["user_id"],
        )
        await base.show(
            event,
            f"❌ Order #{order['id']} cancelled. " + _subscription_effect(subscription),
            [base.home_row()],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:grant:(\d+)(:\d+)?$"))
    async def grant_pick_product(event):
        user = await base.actor(event, "subscriptions")
        if not user:
            return
        target = await users_db.get_by_id(target_id(event))
        if not target or target["telegram_id"] <= 0:
            await event.answer("User not found", alert=True)
            return

        # Everything this particular customer could be given: the public catalog plus
        # the offers made for them.
        items = await products_db.list_public(active_only=False)
        items += await products_db.list_personal(target["id"], active_only=False)
        if not items:
            await event.answer("The catalog is empty", alert=True)
            return

        page = base.page_from(event, 3)
        rows = base.paged_rows(
            items,
            page,
            f"a:grant:{target['id']}",
            lambda item: f"{item['emoji']} {item['name'][:26]}",
            lambda item: f"a:grantp:{target['id']}:{item['id']}",
        )
        rows.append([Button.inline("◀️ Back", f"a:user:{target['id']}")])
        await event.answer()
        await base.show(
            event,
            f"➕ <b>Grant access to {users_db.display_name(target)}</b>\n\n"
            "Pick the product. Nothing is charged and no payment is recorded.",
            rows,
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:grantp:(\d+):(\d+)$"))
    async def grant_ask_days(event):
        user = await base.actor(event, "subscriptions")
        if not user:
            return
        _, _, target_raw, product_raw = event.data.decode().split(":")
        target = await users_db.get_by_id(int(target_raw))
        product = await products_db.get(int(product_raw))
        if not target or target["telegram_id"] <= 0 or not product:
            await event.answer("Not found", alert=True)
            return
        if product["owner_user_id"] not in (products_db.PUBLIC, target["id"]):
            await event.answer("This product is not available to that client", alert=True)
            return
        if await subs_db.active_for_slug(target["id"], product["slug"]):
            await event.answer("They already have it, use Add days", alert=True)
            return
        await base.ask(
            event,
            "grant_days",
            "subscriptions",
            f"➕ How many days of {texts.escape(product['emoji'])} "
            f"{texts.escape(product['name'])} for {users_db.display_name(target)}?",
            client_id=target["id"],
            product_id=product["id"],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:subs(:\d+)?$"))
    async def subscriptions_list(event):
        if not await base.actor(event, "subscriptions"):
            return
        total = await subs_db.count_active()
        items = await subs_db.list_active(limit=QUEUE_LIMIT)
        if not items:
            await base.show(event, "📭 No active subscriptions.", [base.home_row()])
            await event.answer()
            return

        page = base.page_from(event, 2)
        lines = [f"📦 <b>Active subscriptions: {total}</b>", ""]
        if total > len(items):
            lines.append(f"<i>Showing the {len(items)} closest to expiry.</i>")
            lines.append("")
        for item in base.page_slice(items, page):
            lines.append(
                f"#{item['id']} {texts.escape(item['emoji'])} "
                f"{texts.escape(item['product_name'])} - "
                f"{users_db.display_name(item)}, until {dates.fmt_date(item['expires_at'])} "
                f"({dates.days_left(item['expires_at'])} days)"
            )
        rows = base.paged_rows(
            items,
            page,
            "a:subs",
            lambda item: f"#{item['id']} {item['product_name'][:20]}",
            lambda item: f"a:sub:{item['id']}",
        )
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:sub:(\d+)$"))
    async def subscription_card(event):
        if not await base.actor(event, "subscriptions"):
            return
        subscription = await subs_db.get(target_id(event))
        if not subscription:
            await event.answer("Subscription not found", alert=True)
            return
        await base.show(
            event,
            cards.subscription_card(subscription),
            cards.subscription_buttons(subscription),
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:subdays:(\d+)$"))
    async def subscription_days(event):
        user = await base.actor(event, "subscriptions")
        if not user:
            return
        subscription = await subs_db.get(target_id(event))
        if not subscription:
            await event.answer("Subscription not found", alert=True)
            return
        await base.ask(
            event,
            "sub_days",
            "subscriptions",
            f"🎁 How many days to add to subscription #{subscription['id']} "
            f"({texts.escape(subscription['product_name'])}, now until "
            f"{dates.fmt_date(subscription['expires_at'])})?",
            subscription_id=subscription["id"],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:subcreds:(\d+)$"))
    async def subscription_credentials(event):
        user = await base.actor(event, "subscriptions")
        if not user:
            return
        subscription = await subs_db.get(target_id(event))
        if not subscription:
            await event.answer("Subscription not found", alert=True)
            return
        await base.ask(
            event,
            "sub_creds",
            "subscriptions",
            f"🔑 New access details for subscription #{subscription['id']} "
            f"({texts.escape(subscription['product_name'])}).",
            subscription_id=subscription["id"],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:subcancelask:(\d+)$"))
    async def subscription_cancel_confirmation(event):
        if not await base.actor(event, "subscriptions"):
            return
        subscription = await subs_db.get(target_id(event))
        if not subscription or subscription["status"] != subs_db.ACTIVE:
            await event.answer("Active subscription not found", alert=True)
            return
        await base.show(
            event,
            f"⛔️ <b>Close subscription #{subscription['id']}?</b>\n\n"
            "Access details will be removed immediately.",
            [
                [Button.inline("⛔️ Close subscription", f"a:subcancelgo:{subscription['id']}")],
                [Button.inline("◀️ Back", f"a:sub:{subscription['id']}")],
            ],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:subcancel:(\d+)$"))
    async def retired_subscription_close_button(event):
        await subscription_cancel_confirmation(event)

    @client.on(events.CallbackQuery(pattern=rb"^a:subcancelgo:(\d+)$"))
    async def subscription_cancel(event):
        user = await base.actor(event, "subscriptions")
        if not user:
            return
        subscription = await subs_db.get(target_id(event))
        if not subscription:
            await event.answer("Subscription not found", alert=True)
            return
        closed = await billing.close_subscription(subscription["id"], subs_db.CANCELLED)
        if closed is None:
            await event.answer("This subscription is no longer active", alert=True)
            return
        await journal.action(user["telegram_id"], "sub_cancel", f"sub:{subscription['id']}")
        await event.answer("Subscription closed")
        await base.show(
            event, f"⛔️ Subscription #{subscription['id']} closed.", [base.home_row()]
        )


@on_state("order_credentials")
async def save_order_credentials(event, user, data, text):
    order = await orders_db.get(data["order_id"])
    states.clear_for(event)
    if not order:
        await base.respond(event, "Order not found.")
        return
    if not order.get("paid_at") or not order.get("subscription_id"):
        await base.respond(event, "Payment and an active subscription are required.")
        return
    subscription = await subs_db.get(order["subscription_id"])
    if not subscription or subscription["status"] != subs_db.ACTIVE:
        await base.respond(event, "This order has no active subscription.")
        return

    # Claim an eligible state before sending credentials.
    if not await orders_db.claim_status(
        order["id"],
        orders_db.DELIVERED,
        (orders_db.PAID, orders_db.DELIVERED, orders_db.PROBLEM),
        processed_by=user["telegram_id"],
    ):
        await base.respond(event, "Order is closed, nothing was sent.")
        return

    stored = await billing.set_credentials(order["subscription_id"], text) is not None
    if not stored:
        await orders_db.claim_status(
            order["id"],
            orders_db.PROBLEM,
            (orders_db.DELIVERED,),
            processed_by=user["telegram_id"],
        )
        await base.respond(
            event, "The subscription was closed before the access details were saved."
        )
        return
    await journal.action(user["telegram_id"], "order_credentials", f"order:{order['id']}")

    instruction = ""
    if order.get("product_id"):
        product = await products_db.get(order["product_id"])
        if product:
            instruction = product["instruction"] or ""

    delivered = await notify.to_user(
        event.client,
        order["telegram_id"],
        texts.credentials_message(order["id"], text, instruction),
        keyboards.order_response(order["id"]),
        expected_user_id=order["user_id"],
    )
    await base.respond(
        event,
        f"Access details for order #{order['id']}: saved, "
        + ("sent to the customer." if delivered else "NOT delivered: customer unreachable."),
    )


@on_state("sub_days")
async def add_subscription_days(event, user, data, text):
    try:
        days = int(text)
    except ValueError:
        await base.respond(event, "A whole number is expected. Try again or type 'cancel'.")
        return
    if not 1 <= days <= 3650:
        await base.respond(event, "Enter a number of days between 1 and 3650.")
        return

    states.clear_for(event)
    subscription = await subs_db.get(data["subscription_id"])
    if not subscription:
        await base.respond(event, "Subscription not found.")
        return

    # Hold the customer lifecycle through the Telegram send. A database-only recheck
    # still leaves a gap in which erasure can commit after the read but before notify.
    async with users_db.lifecycle_lock(subscription["user_id"]):
        updated = await billing.gift_days(subscription["id"], days)
        if updated is None:
            await base.respond(
                event, "The subscription or customer is no longer active. No days were added."
            )
            return
        await journal.action(
            user["telegram_id"], "sub_add_days", f"sub:{subscription['id']}", str(days)
        )

        # Re-read after the atomic update instead of using the copy loaded before staff
        # typed the number. Erasure replaces telegram_id and closes the subscription.
        recipient = await subs_db.get(subscription["id"])
        if (
            not recipient
            or recipient["status"] != subs_db.ACTIVE
            or recipient["telegram_id"] <= 0
        ):
            await base.respond(
                event,
                f"Subscription #{subscription['id']} changed, but the customer became "
                "unavailable before the notice was sent.",
            )
            return
        await notify.to_user(
            event.client,
            recipient["telegram_id"],
            f"🎁 <b>{days} days added to your subscription</b>\n\n"
            f"{texts.escape(recipient['emoji'])} "
            f"{texts.escape(recipient['product_name'])}\n"
            f"📅 Now until {dates.fmt_date(recipient['expires_at'])}",
            lifecycle_held=True,
        )
        await base.respond(
            event,
            f"✅ Subscription #{subscription['id']}: {days} days added, "
            f"new date {dates.fmt_date(recipient['expires_at'])}.",
        )


@on_state("sub_creds")
async def set_subscription_credentials(event, user, data, text):
    states.clear_for(event)
    subscription = await subs_db.get(data["subscription_id"])
    if not subscription:
        await base.respond(event, "Subscription not found.")
        return

    if await billing.set_credentials(subscription["id"], text) is None:
        await base.respond(event, "That subscription is no longer active, nothing was saved.")
        return
    await journal.action(user["telegram_id"], "sub_credentials", f"sub:{subscription['id']}")
    await notify.to_user(
        event.client,
        subscription["telegram_id"],
        f"🔄 <b>Access details updated</b>\n\n"
        f"{texts.escape(subscription['emoji'])} "
        f"{texts.escape(subscription['product_name'])}\n\n"
        f"<code>{texts.escape(text)}</code>",
        expected_user_id=subscription["user_id"],
    )
    await base.respond(event, f"✅ Subscription #{subscription['id']} updated.")


@on_state("grant_days")
async def grant_days(event, user, data, text):
    try:
        days = int(text)
    except ValueError:
        await base.respond(
            event, "A whole number of days is expected. Try again or type 'cancel'."
        )
        return
    if not 1 <= days <= 3650:
        await base.respond(event, "Enter a number of days between 1 and 3650.")
        return

    states.clear_for(event)
    target = await users_db.get_by_id(data["client_id"])
    product = await products_db.get(data["product_id"])
    if not target or target["telegram_id"] <= 0 or not product:
        await base.respond(event, "The client or the product is gone.")
        return
    if product["owner_user_id"] not in (products_db.PUBLIC, target["id"]):
        await base.respond(event, "This product is not available to that client.")
        return

    try:
        subscription = await billing.grant_subscription(target["id"], product, days)
    except billing.UserNotEligibleError:
        await base.respond(event, "The client is blocked or has been erased.")
        return
    if subscription is None:
        await base.respond(
            event, "They already have this product running. Use Add days instead."
        )
        return

    await journal.action(
        user["telegram_id"],
        "sub_grant",
        f"sub:{subscription['id']}",
        f"user:{target['id']} product:{product['slug']} days:{days}",
    )
    await notify.to_user(
        event.client,
        target["telegram_id"],
        f"🎁 <b>Access granted</b>\n\n"
        f"{texts.escape(product['emoji'])} <b>{texts.escape(product['name'])}</b>\n"
        f"📅 Until {dates.fmt_date(subscription['expires_at'])}\n\n"
        "The access details will arrive shortly.",
        keyboards.after_payment(),
        expected_user_id=target["id"],
    )
    await base.respond(
        event,
        f"✅ {days} days of {texts.escape(product['name'])} granted to "
        f"{users_db.display_name(target)}, until "
        f"{dates.fmt_date(subscription['expires_at'])}. Nothing was charged.\n\n"
        + cards.subscription_card(subscription),
        buttons=cards.subscription_buttons(subscription),
        parse_mode="html",
    )
