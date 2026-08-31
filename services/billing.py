# Prices, available payment methods and subscription renewal.

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from db import connection, settings
from db import orders as orders_db
from db import requisites as requisites_db
from db import subscriptions as subs_db
from utils import dates

logger = logging.getLogger(__name__)

# Serialize multi-step subscription updates per user and product.
_subscription_lock = asyncio.Lock()


class UserNotEligibleError(ValueError):
    """Access cannot be created for a blocked, erased or missing user."""


async def _require_access_recipient(user_id: int) -> None:
    user = await connection.fetch_one(
        "SELECT id FROM users WHERE id = ? AND telegram_id > 0 AND is_blocked = 0",
        (user_id,),
    )
    if user is None:
        raise UserNotEligibleError(f"User {user_id} cannot receive access")


def next_expiry(current_expires, duration_days: int, now: datetime = None) -> datetime:
    # Return the new expiry date.
    #
    # Paying early never moves the date backwards: the period is added to the current
    # expiry date, so prepaid days are not lost. If the subscription already ended,
    # the period starts from now.
    now = now or dates.utcnow()
    current = dates.parse(current_expires)
    base = current if current and current > now else now
    return base + timedelta(days=max(1, duration_days))


def rub_price(product: dict) -> int:
    # Rounded, never truncated: int(299.99) would quietly sell a 300 rouble plan for 299.
    if product.get("price_rub"):
        return int(round(product["price_rub"]))
    if product.get("price_stars"):
        return int(round(product["price_stars"] * settings.get_float("star_to_rub", 1.5)))
    return 0


def stars_price(product: dict) -> int:
    return int(product.get("price_stars") or 0)


def can_pay_stars(product: dict) -> bool:
    return stars_price(product) > 0


async def transfer_method(user: dict, product: dict) -> Optional[dict]:
    # Bank details this client may use for this product, if transfers are allowed.
    if rub_price(product) <= 0 or not user:
        return None
    if not user.get("payment_method_id") and not settings.get_bool("transfer_for_all"):
        return None
    return await requisites_db.for_user(user)


def price_label(product: dict, with_transfer: bool = False) -> str:
    parts = []
    if can_pay_stars(product):
        parts.append(f"{stars_price(product)}⭐")
    if with_transfer and rub_price(product) > 0:
        parts.append(f"{rub_price(product)}₽")
    if not parts and rub_price(product) > 0:
        parts.append(f"{rub_price(product)}₽")
    return " / ".join(parts) if parts else "price not set"


def product_from_order(order: dict) -> dict:
    # Product snapshot taken from the order, works even if the product was deleted.
    return {
        "id": order.get("product_id"),
        "slug": order["product_slug"],
        "name": order["product_name"],
        "emoji": order.get("emoji") or "📦",
        "duration_days": order.get("duration_days") or 30,
        "price_stars": int(order.get("amount_stars") or 0),
        "price_rub": int(order.get("amount_rub") or 0),
    }


async def revoke_payment(order: dict) -> Optional[dict]:
    # Take back exactly the days this order granted, used on refunds and cancellations.
    #
    # Remove only this order's period, whether it was a first purchase or a renewal.
    async with _subscription_lock:
        fresh = await orders_db.get(order["id"]) or order
        if fresh.get("reversed_at"):
            return None
        subscription_id = fresh.get("subscription_id")
        if not subscription_id:
            if fresh.get("paid_at"):
                await orders_db.mark_reversed(order["id"])
            return None
        subscription = await subs_db.get(subscription_id)
        if not subscription:
            if fresh.get("paid_at"):
                await orders_db.mark_reversed(order["id"])
            return None

        duration = fresh.get("duration_days") or 30
        rolled_back = dates.parse(subscription["expires_at"]) - timedelta(days=duration)
        async with connection.transaction():
            if rolled_back <= dates.utcnow() and subscription["status"] == subs_db.ACTIVE:
                await subs_db.close(subscription_id, subs_db.CANCELLED)
            else:
                # Date only. Reactivating here would hand access back to someone who
                # was just paid out, and could collide with a subscription they bought
                # again in the meantime.
                await subs_db.set_expiry(subscription_id, rolled_back)
            # The link is kept: after a refund the operator still has to be able to say
            # what the order granted. reversed_at is what stops a second rollback.
            await orders_db.mark_reversed(order["id"])
        logger.info("Order %s revoked, subscription %s adjusted", order["id"], subscription_id)
        return await subs_db.get(subscription_id)


async def apply_payment(order: dict) -> dict:
    # Grant or extend a subscription for a paid order.
    #
    # Returns {'subscription', 'renewed', 'previous_expires'}. Calling it twice for the
    # same order does not add the period twice.
    async with _subscription_lock:
        async with connection.transaction():
            # Recheck the financial state in the same transaction that grants access.
            # A refund or rejection that won the race must never receive a subscription.
            fresh = await orders_db.get(order["id"]) or order
            if (
                not fresh.get("paid_at")
                or fresh.get("reversed_at")
                or fresh["status"] in orders_db.REVERSED_STATUSES
                or fresh["status"] == orders_db.REFUND_PENDING
            ):
                raise ValueError(f"Order {fresh['id']} is not eligible for fulfilment")
            await _require_access_recipient(fresh["user_id"])

            if fresh.get("subscription_id"):
                subscription = await subs_db.get(fresh["subscription_id"])
                if subscription:
                    return {
                        "subscription": subscription,
                        "renewed": bool(fresh.get("is_renewal")),
                        "previous_expires": None,
                    }

            snapshot = product_from_order(fresh)
            duration = snapshot["duration_days"]

            existing = await subs_db.active_for_slug(fresh["user_id"], fresh["product_slug"])
            if existing:
                previous_expires = existing["expires_at"]
                if not await subs_db.extend(
                    existing["id"], next_expiry(previous_expires, duration)
                ):
                    raise RuntimeError("The active subscription changed during fulfilment")
                subscription_id = existing["id"]
                renewed = True
            else:
                previous_expires = None
                subscription_id = await subs_db.create(
                    user_id=fresh["user_id"],
                    product=snapshot,
                    expires_at=next_expiry(None, duration),
                    is_personal=bool(fresh.get("is_personal")),
                )
                renewed = False

            await orders_db.update(fresh["id"], subscription_id=subscription_id)
        subscription = await subs_db.get(subscription_id)
        logger.info(
            "Order %s applied: subscription %s until %s (renewal=%s)",
            fresh["id"],
            subscription_id,
            subscription["expires_at"],
            renewed,
        )
        return {
            "subscription": subscription,
            "renewed": renewed,
            "previous_expires": previous_expires,
        }


async def close_subscription(
    subscription_id: int, status: str, notified: bool = False
) -> Optional[dict]:
    # Staff closing a subscription takes the same lock as a payment. Without it a
    # cancel racing a renewal left the row active with its access details wiped.
    async with _subscription_lock:
        current = await subs_db.get(subscription_id)
        if not current or current["status"] != subs_db.ACTIVE:
            return None
        await subs_db.close(subscription_id, status, notified=notified)
        return await subs_db.get(subscription_id)


async def claim_expired(subscription_id: int, expected_expires_at) -> bool:
    # Expiry, credentials, payments and staff changes all serialize on the same lock.
    async with _subscription_lock:
        return await subs_db.claim_expired(subscription_id, expected_expires_at)


async def gift_days(subscription_id: int, days: int) -> Optional[dict]:
    async with _subscription_lock:
        # The eligibility check and extension must be one database decision. Privacy
        # erasure closes subscriptions in its own write transaction; whichever starts
        # first now finishes completely before the other can inspect the customer.
        async with connection.transaction():
            current = await subs_db.get(subscription_id)
            if not current or current["status"] != subs_db.ACTIVE:
                return None
            try:
                await _require_access_recipient(current["user_id"])
            except UserNotEligibleError:
                return None
            await subs_db.add_days(subscription_id, days)
            return await subs_db.get(subscription_id)


async def set_credentials(subscription_id: int, credentials: str) -> Optional[dict]:
    # Under the same lock as the sweeps, so credentials cannot be written back onto a
    # subscription that was closed a moment earlier.
    async with _subscription_lock:
        subscription = await subs_db.get(subscription_id)
        if not subscription or subscription["status"] != subs_db.ACTIVE:
            return None
        if not await subs_db.set_credentials(subscription_id, credentials):
            return None
        return await subs_db.get(subscription_id)


async def grant_subscription(user_id: int, product: dict, days: int) -> Optional[dict]:
    # Access handed over without a payment: a gift, a compensation, a migration from
    # whatever the shop used before. Returns None when the customer already has this
    # product running, because the existing subscription should be extended.
    #
    # A zero amount order is written alongside it. It carries no paid_at, so it is not
    # income, but it does give the subscription something to point back at: without it
    # the "access nobody paid for" reconciliation query would flag every gift.
    async with _subscription_lock:
        snapshot = {**product, "duration_days": max(1, days)}
        async with connection.transaction():
            # Eligibility and creation share the write transaction with erasure/blocking:
            # whichever operation wins is fully visible to the one that follows.
            await _require_access_recipient(user_id)
            if await subs_db.active_for_slug(user_id, product["slug"]):
                return None
            subscription_id = await subs_db.create(
                user_id=user_id,
                product=snapshot,
                expires_at=next_expiry(None, days),
                is_personal=bool(product["owner_user_id"]),
            )
            await orders_db.create(
                user_id=user_id,
                product=snapshot,
                amount_stars=0,
                amount_rub=0,
                payment_method="grant",
                status=orders_db.COMPLETED,
                is_personal=bool(product["owner_user_id"]),
                subscription_id=subscription_id,
            )
        logger.info(
            "Subscription %s granted to user %s for %s days", subscription_id, user_id, days
        )
        return await subs_db.get(subscription_id)
