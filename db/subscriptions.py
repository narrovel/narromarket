# Subscriptions: what a client currently has access to.

from typing import Optional

from db import connection
from utils import dates

ACTIVE = "active"
EXPIRED = "expired"
CANCELLED = "cancelled"

_NOTIFY_FLAGS = ("notified_3d", "notified_1d", "notified_expired")

_WITH_USER = """
    SELECT s.*, u.telegram_id, u.username, u.first_name, u.last_name
    FROM subscriptions s
    JOIN users u ON u.id = s.user_id
"""


async def get(subscription_id: int) -> Optional[dict]:
    return await connection.fetch_one(f"{_WITH_USER} WHERE s.id = ?", (subscription_id,))


async def create(user_id: int, product: dict, expires_at, is_personal: bool) -> int:
    return await connection.execute(
        "INSERT INTO subscriptions "
        "(user_id, product_id, product_slug, product_name, emoji, is_personal, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            product["id"],
            product["slug"],
            product["name"],
            product["emoji"],
            1 if is_personal else 0,
            dates.to_sql(expires_at),
        ),
    )


async def active_for_user(user_id: int) -> list[dict]:
    return await connection.fetch_all(
        "SELECT * FROM subscriptions WHERE user_id = ? AND status = ? ORDER BY expires_at, id",
        (user_id, ACTIVE),
    )


async def active_for_slug(user_id: int, slug: str) -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM subscriptions WHERE user_id = ? AND product_slug = ? AND status = ? "
        "ORDER BY expires_at DESC, id DESC LIMIT 1",
        (user_id, slug, ACTIVE),
    )


async def extend(subscription_id: int, new_expires_at) -> bool:
    parsed_expiry = dates.parse(new_expires_at)
    if parsed_expiry <= dates.utcnow():
        changed = await connection.execute_change(
            "UPDATE subscriptions SET expires_at = ?, "
            "status = CASE WHEN status = ? THEN ? ELSE status END, "
            "credentials = CASE WHEN status = ? THEN NULL ELSE credentials END, "
            "notified_expired = CASE WHEN status = ? THEN 1 ELSE notified_expired END, "
            "updated_at = datetime('now') WHERE id = ? AND status = ?",
            (
                dates.to_sql(parsed_expiry),
                ACTIVE,
                EXPIRED,
                ACTIVE,
                ACTIVE,
                subscription_id,
                ACTIVE,
            ),
        )
        return changed == 1
    changed = await connection.execute_change(
        "UPDATE subscriptions SET expires_at = ?, "
        "notified_3d = 0, notified_1d = 0, notified_expired = 0, "
        "updated_at = datetime('now') WHERE id = ? AND status = ?",
        (dates.to_sql(new_expires_at), subscription_id, ACTIVE),
    )
    return changed == 1


async def set_expiry(subscription_id: int, new_expires_at) -> None:
    # Moves the date without touching the status, so rolling a refund back cannot turn
    # a closed subscription active again, or collide with a newer active one on
    # idx_subs_active_slug.
    await connection.execute(
        "UPDATE subscriptions SET expires_at = ?, updated_at = datetime('now') WHERE id = ?",
        (dates.to_sql(new_expires_at), subscription_id),
    )


async def add_days(subscription_id: int, days: int) -> Optional[str]:
    subscription = await get(subscription_id)
    if not subscription or subscription["status"] != ACTIVE:
        return None
    new_expires = dates.add_days(subscription["expires_at"], days)
    if not await extend(subscription_id, new_expires):
        return None
    return dates.to_sql(new_expires)


async def set_credentials(subscription_id: int, credentials: str) -> bool:
    # The status predicate is a final line of defence if a close commits after the
    # caller read the row but before this write reaches SQLite.
    changed = await connection.execute_change(
        "UPDATE subscriptions SET credentials = ?, updated_at = datetime('now') "
        "WHERE id = ? AND status = ?",
        (credentials, subscription_id, ACTIVE),
    )
    return changed == 1


async def set_status(subscription_id: int, status: str) -> None:
    # Access details die with the subscription: the inline button that shows them stays
    # tappable in the customer's chat forever.
    fields = {"status": status}
    if status != ACTIVE:
        fields["credentials"] = None
    await connection.update_row("subscriptions", subscription_id, **fields)


async def close(subscription_id: int, status: str, notified: bool = False) -> None:
    # Close and flag in one write so the sweep cannot leave a half finished row behind.
    fields = {"status": status, "credentials": None}
    if notified:
        fields["notified_expired"] = 1
    await connection.update_row("subscriptions", subscription_id, **fields)


async def mark_notified(subscription_id: int, flag: str) -> None:
    if flag not in _NOTIFY_FLAGS:
        raise ValueError(f"Unknown notification flag: {flag}")
    await connection.update_row("subscriptions", subscription_id, **{flag: 1})


async def claim_notification(subscription_id: int, flag: str, expected_expires_at) -> bool:
    if flag not in _NOTIFY_FLAGS:
        raise ValueError(f"Unknown notification flag: {flag}")
    changed = await connection.execute_change(
        f"UPDATE subscriptions SET {flag} = 1, updated_at = datetime('now') "
        f"WHERE id = ? AND status = ? AND expires_at = ? AND {flag} = 0",
        (subscription_id, ACTIVE, dates.to_sql(expected_expires_at)),
    )
    return changed == 1


async def release_notifications(
    subscription_id: int, flags: tuple[str, ...], expected_expires_at
) -> None:
    if not flags or any(flag not in _NOTIFY_FLAGS for flag in flags):
        raise ValueError("Unknown notification flag")
    assignments = ", ".join(f"{flag} = 0" for flag in flags)
    await connection.execute(
        f"UPDATE subscriptions SET {assignments}, updated_at = datetime('now') "
        "WHERE id = ? AND status = ? AND expires_at = ?",
        (subscription_id, ACTIVE, dates.to_sql(expected_expires_at)),
    )


async def claim_expired(subscription_id: int, expected_expires_at) -> bool:
    changed = await connection.execute_change(
        "UPDATE subscriptions SET status = ?, credentials = NULL, notified_expired = 1, "
        "updated_at = datetime('now') "
        "WHERE id = ? AND status = ? AND expires_at = ? AND expires_at <= ?",
        (
            EXPIRED,
            subscription_id,
            ACTIVE,
            dates.to_sql(expected_expires_at),
            dates.to_sql(dates.utcnow()),
        ),
    )
    return changed == 1


async def list_active(limit: int = 100, offset: int = 0) -> list[dict]:
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE s.status = ? ORDER BY s.expires_at, s.id LIMIT ? OFFSET ?",
        (ACTIVE, limit, offset),
    )


async def list_for_user_with_user(user_id: int) -> list[dict]:
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE s.user_id = ? ORDER BY s.expires_at DESC, s.id DESC",
        (user_id,),
    )


async def expiring_in(days: int, flag: str) -> list[dict]:
    # Everything that ends no later than the end of that local day and was not reminded
    # yet. A missed run is caught up on the next one instead of being lost: the flag,
    # not the exact day, is what stops a second reminder.
    if flag not in _NOTIFY_FLAGS:
        raise ValueError(f"Unknown notification flag: {flag}")
    _, end = dates.day_bounds_utc(days)
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE s.status = ? AND s.{flag} = 0 "
        "AND s.expires_at > ? AND s.expires_at < ? ORDER BY s.expires_at, s.id",
        (ACTIVE, dates.to_sql(dates.utcnow()), end),
    )


async def expired(limit: int = 500) -> list[dict]:
    # Capped: the sweep runs again on the next pass, and an unbounded result set is what
    # turns a wrong server clock into one enormous destructive run.
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE s.status = ? AND s.expires_at <= ? ORDER BY s.expires_at LIMIT ?",
        (ACTIVE, dates.to_sql(dates.utcnow()), limit),
    )


async def count_active() -> int:
    return await connection.fetch_value(
        "SELECT COUNT(*) FROM subscriptions WHERE status = ?", (ACTIVE,), 0
    )
