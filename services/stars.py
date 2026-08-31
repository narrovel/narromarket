# Telegram Stars refunds.

import logging

from telethon.tl.functions.payments import RefundStarsChargeRequest

from db import refunds as refunds_db
from db import settings
from services import notify
from utils import keyboards, texts

logger = logging.getLogger(__name__)


async def refund_charge(
    client,
    telegram_id: int,
    charge_id: str,
    provider_charge_id: str = None,
) -> bool:
    # Telegram's refund method expects PaymentCharge.id. The provider id is retained in
    # the database for reconciliation, but it is not accepted by this MTProto request.
    if not charge_id:
        logger.error("Cannot refund payment: Telegram charge id was not recorded")
        return False
    try:
        await client(RefundStarsChargeRequest(user_id=telegram_id, charge_id=charge_id))
        return True
    except Exception as error:
        logger.error("Stars refund failed for %s: %s", telegram_id, error)
        return False


def _already_refunded(error: Exception) -> bool:
    value = f"{type(error).__name__} {error}".upper().replace(" ", "_")
    return "CHARGE_ALREADY_REFUNDED" in value


async def process_refund(client, refund_id: int) -> bool:
    """Run one leased attempt. Completed jobs never issue another Telegram RPC."""
    record = await refunds_db.get(refund_id)
    if not record:
        return False
    if record["status"] == refunds_db.COMPLETED:
        return True

    lease = await refunds_db.claim(refund_id)
    if not lease:
        current = await refunds_db.get(refund_id)
        return bool(current and current["status"] == refunds_db.COMPLETED)

    if record["payment_method"] != "stars":
        await refunds_db.mark_failed(refund_id, lease, "Manual refund required")
        return False
    telegram_charge_id = record.get("telegram_charge_id")
    if not telegram_charge_id:
        await refunds_db.mark_failed(
            refund_id, lease, "Telegram charge id was not recorded; manual refund required"
        )
        return False

    try:
        await client(
            RefundStarsChargeRequest(
                user_id=record["telegram_id"], charge_id=telegram_charge_id
            )
        )
    except Exception as error:
        if _already_refunded(error):
            return await refunds_db.mark_completed(refund_id, lease, refunds_db.TELEGRAM)
        logger.error(
            "Stars refund %s failed for %s: %s", refund_id, record["telegram_id"], error
        )
        await refunds_db.mark_failed(refund_id, lease, str(error))
        return False
    return await refunds_db.mark_completed(refund_id, lease, refunds_db.TELEGRAM)


async def refund(
    client,
    telegram_id: int,
    charge_id: str,
    reason: str,
    provider_charge_id: str = None,
    *,
    refund_id: int = None,
    amount_stars: int = 0,
    currency: str = "XTR",
    user_id: int = 0,
) -> bool:
    # Persist before the RPC. Callers that need invoice cancellation in the same
    # transaction create the row first and pass its id here.
    if refund_id is None:
        record = await refunds_db.create(
            telegram_id=telegram_id,
            user_id=user_id,
            source="automatic",
            reason=reason,
            telegram_charge_id=charge_id,
            provider_charge_id=provider_charge_id,
            amount_stars=amount_stars,
            currency=currency,
        )
        refund_id = record["id"]
    returned = await process_refund(client, refund_id)
    tail = (
        "💰 Your Telegram Stars have been refunded."
        if returned
        else f"Please contact {settings.manager_mention()} for a refund."
    )
    record = await refunds_db.get(refund_id)
    await notify.to_user(
        client,
        telegram_id,
        f"⚠️ {reason}\n\n{tail}",
        expected_user_id=int(record.get("user_id") or 0) if record else 0,
    )
    if not returned:
        record = await refunds_db.get(refund_id)
        detail = record.get("last_error") if record else "refund record unavailable"
        await notify.to_staff(
            client,
            f"⚠️ <b>Refund #{refund_id} is waiting</b>\n\n"
            f"Customer: <code>{telegram_id}</code>\n"
            f"Reason: {texts.escape(reason)}\n"
            f"Error: {texts.escape(detail or 'another worker is processing it')}",
            keyboards.refund_retry(refund_id),
        )
    return returned
