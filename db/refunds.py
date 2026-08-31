# Durable refund obligations and their retry leases.

import secrets
from typing import Optional

from db import connection

PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"

TELEGRAM = "telegram"
MANUAL = "manual"


async def get(refund_id: int) -> Optional[dict]:
    return await connection.fetch_one("SELECT * FROM refunds WHERE id = ?", (refund_id,))


async def for_order(order_id: int) -> Optional[dict]:
    return await connection.fetch_one("SELECT * FROM refunds WHERE order_id = ?", (order_id,))


async def by_charge(
    telegram_charge_id: str = None, provider_charge_id: str = None
) -> Optional[dict]:
    if telegram_charge_id:
        row = await connection.fetch_one(
            "SELECT * FROM refunds WHERE telegram_charge_id = ?",
            (telegram_charge_id,),
        )
        if row:
            return row
    if provider_charge_id:
        return await connection.fetch_one(
            "SELECT * FROM refunds WHERE provider_charge_id = ?",
            (provider_charge_id,),
        )
    return None


async def create(
    *,
    telegram_id: int,
    source: str,
    reason: str,
    order_id: int = None,
    user_id: int = None,
    telegram_charge_id: str = None,
    provider_charge_id: str = None,
    payment_method: str = "stars",
    amount_stars: int = 0,
    amount_rub: int = 0,
    currency: str = "XTR",
) -> dict:
    """Record one refund obligation, returning the existing row on redelivery."""
    order_id = order_id or None
    telegram_charge_id = telegram_charge_id or None
    provider_charge_id = provider_charge_id or None
    async with connection.transaction():
        order_owner_id = None
        if order_id:
            order_owner_id = await connection.fetch_value(
                "SELECT user_id FROM orders WHERE id = ?", (order_id,)
            )
            if order_owner_id is None:
                raise ValueError("Refund order does not exist")
            if user_id not in (None, 0, order_owner_id):
                raise ValueError("Refund user does not own the order")
            user_id = order_owner_id
        elif user_id is None:
            live = await connection.fetch_one(
                "SELECT id FROM users WHERE telegram_id = ? AND telegram_id > 0",
                (telegram_id,),
            )
            user_id = live["id"] if live else None
        else:
            user_id = int(user_id) or None

        order_row = await for_order(order_id) if order_id else None
        telegram_row = (
            await connection.fetch_one(
                "SELECT * FROM refunds WHERE telegram_charge_id = ?",
                (telegram_charge_id,),
            )
            if telegram_charge_id
            else None
        )
        provider_row = (
            await connection.fetch_one(
                "SELECT * FROM refunds WHERE provider_charge_id = ?",
                (provider_charge_id,),
            )
            if provider_charge_id
            else None
        )
        candidates = [row for row in (order_row, telegram_row, provider_row) if row]
        if len({row["id"] for row in candidates}) > 1:
            raise ValueError("Refund identifiers already belong to different obligations")
        existing = candidates[0] if candidates else None
        if existing:
            for field, supplied in (
                ("order_id", order_id),
                ("telegram_charge_id", telegram_charge_id),
                ("provider_charge_id", provider_charge_id),
            ):
                if supplied and existing.get(field) and existing[field] != supplied:
                    raise ValueError(f"Refund {field} does not match the recorded obligation")
            if existing.get("user_id") and user_id and int(existing["user_id"]) != int(user_id):
                raise ValueError("Refund user does not match the recorded obligation")
            # A completed refund may already have scrubbed an erased customer's id.
            # Zero is therefore an accepted redelivery sentinel, never a new recipient.
            if int(existing["telegram_id"]) not in (0, int(telegram_id)):
                raise ValueError("Refund customer does not match the recorded obligation")
            # Legacy/admin-created rows may know only one charge id initially. Fill in
            # missing reconciliation data without replacing the original obligation.
            await connection.execute(
                "UPDATE refunds SET "
                "order_id = COALESCE(order_id, ?), "
                "user_id = COALESCE(user_id, ?), "
                "telegram_charge_id = COALESCE(telegram_charge_id, ?), "
                "provider_charge_id = COALESCE(provider_charge_id, ?), "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    order_id,
                    order_owner_id,
                    telegram_charge_id,
                    provider_charge_id,
                    existing["id"],
                ),
            )
            return await get(existing["id"])

        refund_id = await connection.execute(
            "INSERT INTO refunds (order_id, user_id, telegram_id, telegram_charge_id, "
            "provider_charge_id, source, payment_method, amount_stars, amount_rub, "
            "currency, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                user_id,
                telegram_id,
                telegram_charge_id,
                provider_charge_id,
                source,
                payment_method,
                max(0, int(amount_stars or 0)),
                max(0, int(amount_rub or 0)),
                currency or ("XTR" if payment_method == "stars" else "RUB"),
                reason,
            ),
        )
        return await get(refund_id)


async def unresolved(limit: int = 100) -> list[dict]:
    return await connection.fetch_all(
        "SELECT r.* FROM refunds r LEFT JOIN orders o ON o.id = r.order_id "
        "WHERE r.status != ? OR o.status = 'refund_pending' "
        "ORDER BY r.created_at, r.id LIMIT ?",
        (COMPLETED, max(1, int(limit))),
    )


async def count_unresolved() -> int:
    return await connection.fetch_value(
        "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id "
        "WHERE r.status != ? OR o.status = 'refund_pending'",
        (COMPLETED,),
        0,
    )


async def claim(refund_id: int, lease_seconds: int = 300) -> Optional[str]:
    """Claim a pending or abandoned job. Exactly one concurrent caller gets a token."""
    lease_seconds = max(30, min(3600, int(lease_seconds)))
    token = secrets.token_hex(16)
    changed = await connection.execute_change(
        "UPDATE refunds SET status = ?, attempts = attempts + 1, last_error = NULL, "
        "lease_token = ?, lease_expires_at = datetime('now', ?), "
        "updated_at = datetime('now') WHERE id = ? AND "
        "(status = ? OR (status = ? AND lease_expires_at <= datetime('now')))",
        (
            PROCESSING,
            token,
            f"+{lease_seconds} seconds",
            refund_id,
            PENDING,
            PROCESSING,
        ),
    )
    return token if changed == 1 else None


async def mark_failed(refund_id: int, lease_token: str, error: str) -> bool:
    changed = await connection.execute_change(
        "UPDATE refunds SET status = ?, last_error = ?, lease_token = NULL, "
        "lease_expires_at = NULL, updated_at = datetime('now') "
        "WHERE id = ? AND status = ? AND lease_token = ?",
        (PENDING, str(error)[:1000], refund_id, PROCESSING, lease_token),
    )
    return changed == 1


async def mark_completed(refund_id: int, lease_token: str, resolution: str = TELEGRAM) -> bool:
    changed = await connection.execute_change(
        "UPDATE refunds SET status = ?, resolution = ?, completed_at = datetime('now'), "
        "last_error = NULL, lease_token = NULL, lease_expires_at = NULL, "
        "telegram_id = CASE WHEN EXISTS ("
        "SELECT 1 FROM users u WHERE u.id = refunds.user_id "
        "AND u.telegram_id = refunds.telegram_id AND u.telegram_id > 0"
        ") THEN telegram_id ELSE 0 END, "
        "updated_at = datetime('now') "
        "WHERE id = ? AND status = ? AND lease_token = ?",
        (COMPLETED, resolution, refund_id, PROCESSING, lease_token),
    )
    return changed == 1


async def complete_manually(refund_id: int) -> bool:
    current = await get(refund_id)
    if not current:
        return False
    if current["status"] == COMPLETED:
        return True
    token = await claim(refund_id)
    if not token:
        return False
    return await mark_completed(refund_id, token, MANUAL)
