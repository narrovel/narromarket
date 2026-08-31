# Users and roles.

import asyncio
import logging
import weakref
from html import escape
from typing import Optional

from db import connection

logger = logging.getLogger(__name__)

# Privacy erasure and checkout both cross the database/Telegram boundary. A lock per
# internal user keeps either operation whole without making unrelated customers wait.
# Locks disappear once nobody is using or waiting for them.
_lifecycle_locks = weakref.WeakValueDictionary()

ROLES = ("user", "manager", "admin", "owner")
ROLE_TITLES = {
    "user": "👤 User",
    "manager": "🧰 Manager",
    "admin": "🛠 Admin",
    "owner": "👑 Owner",
}


class EraseBlockedError(RuntimeError):
    """The user's financial or fulfilment state must be resolved before erasure."""


def lifecycle_lock(user_id: int) -> asyncio.Lock:
    """Return the shared checkout/erasure lock for one internal user id."""
    key = int(user_id)
    lock = _lifecycle_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _lifecycle_locks[key] = lock
    return lock


def role_level(role: str) -> int:
    return ROLES.index(role) if role in ROLES else 0


async def get(telegram_id: int) -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    )


async def get_by_id(user_id: int) -> Optional[dict]:
    return await connection.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))


async def get_by_username(username: str) -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM users WHERE lower(username) = lower(?)",
        (username.lstrip("@"),),
    )


async def get_or_create(
    telegram_id: int,
    username: str = None,
    first_name: str = None,
    last_name: str = None,
) -> Optional[dict]:
    user = await get(telegram_id)
    if user:
        async with lifecycle_lock(user["id"]):
            current = await get_by_id(user["id"])
            if (
                not current
                or current["telegram_id"] != telegram_id
                or current["telegram_id"] <= 0
            ):
                # Erasure won after the first lookup. This old update must not recreate
                # names on the retained financial row or silently make a new profile.
                return None
            changed = (
                current["username"] != username
                or current["first_name"] != first_name
                or current["last_name"] != last_name
            )
            if changed:
                await connection.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_name = ?, "
                    "updated_at = datetime('now') "
                    "WHERE id = ? AND telegram_id = ? AND telegram_id > 0",
                    (username, first_name, last_name, current["id"], telegram_id),
                )
                current = await get_by_id(current["id"])
            return current

    # Two updates from a brand new user arrive together, so the insert must tolerate
    # losing the race instead of raising on the UNIQUE constraint.
    await connection.execute(
        "INSERT INTO users (telegram_id, username, first_name, last_name) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(telegram_id) DO NOTHING",
        (telegram_id, username, first_name, last_name),
    )
    created = await get(telegram_id)
    if not created:
        return None
    async with lifecycle_lock(created["id"]):
        current = await get_by_id(created["id"])
        if not current or current["telegram_id"] != telegram_id:
            return None
        return current


async def set_role(user_id: int, role: str) -> bool:
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    return await _update_live(user_id, role=role)


async def set_blocked(user_id: int, blocked: bool) -> bool:
    return await _update_live(user_id, is_blocked=1 if blocked else 0)


async def set_payment_method(user_id: int, method_id: Optional[int]) -> bool:
    return await _update_live(user_id, payment_method_id=method_id)


async def set_note(user_id: int, note: str) -> bool:
    return await _update_live(user_id, note=note)


async def _update_live(user_id: int, **fields) -> bool:
    async with lifecycle_lock(user_id):
        current = await get_by_id(user_id)
        if not current or current["telegram_id"] <= 0:
            return False
        await connection.update_row("users", user_id, **fields)
        return True


async def ensure_owners(telegram_ids: list[int]) -> None:
    # The .env list is the whole truth about who owns the bot: anyone on it is promoted,
    # anyone else holding the role is demoted. Without the second half, taking an id out
    # of .env would leave full access behind with no way to revoke it from the panel.
    async with connection.transaction():
        for telegram_id in telegram_ids:
            user = await get(telegram_id)
            if user is None:
                await connection.execute(
                    "INSERT INTO users (telegram_id, role) VALUES (?, 'owner')",
                    (telegram_id,),
                )
            elif user["role"] != "owner" or user["is_blocked"]:
                # Blocked as well as promoted: otherwise an owner who was blocked before
                # being listed in .env cannot use the bot at all, and only a database
                # editor could let them back in.
                await connection.execute(
                    "UPDATE users SET role = 'owner', is_blocked = 0, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (user["id"],),
                )

        if telegram_ids:
            placeholders = ", ".join("?" for _ in telegram_ids)
            demoted = await connection.execute_change(
                f"UPDATE users SET role = 'user', updated_at = datetime('now') "
                f"WHERE role = 'owner' AND telegram_id NOT IN ({placeholders})",
                telegram_ids,
            )
        else:
            demoted = await connection.execute_change(
                "UPDATE users SET role = 'user', updated_at = datetime('now') "
                "WHERE role = 'owner'"
            )
        if demoted:
            logger.warning("Demoted %s owner(s) missing from OWNER_IDS", demoted)


async def staff() -> list[dict]:
    return await connection.fetch_all(
        "SELECT * FROM users WHERE role IN ('manager', 'admin', 'owner') "
        "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, id"
    )


async def staff_telegram_ids() -> list[int]:
    return [row["telegram_id"] for row in await staff_recipients()]


async def staff_recipients() -> list[dict]:
    # Blocked staff are left out: they cannot act on what they would be notified about.
    return await connection.fetch_all(
        "SELECT id, telegram_id FROM users "
        "WHERE role IN ('manager', 'admin', 'owner') AND is_blocked = 0 "
        "AND telegram_id > 0"
    )


async def search(query: str, limit: int = 20) -> list[dict]:
    query = query.strip().lstrip("@")
    if query.isdigit():
        row = await get(int(query))
        return [row] if row else []
    like = f"%{query}%"
    return await connection.fetch_all(
        "SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? OR last_name LIKE ? "
        "ORDER BY id DESC LIMIT ?",
        (like, like, like, limit),
    )


async def recent(limit: int = 20) -> list[dict]:
    return await connection.fetch_all("SELECT * FROM users ORDER BY id DESC LIMIT ?", (limit,))


async def all_telegram_ids() -> list[int]:
    return [row["telegram_id"] for row in await all_recipients()]


async def all_recipients() -> list[dict]:
    return await connection.fetch_all(
        "SELECT id, telegram_id FROM users WHERE is_blocked = 0 AND telegram_id > 0"
    )


async def erase(user_id: int) -> Optional[dict]:
    async with lifecycle_lock(user_id):
        return await _erase(user_id)


async def _erase(user_id: int) -> Optional[dict]:
    # Everything that identifies a person goes; the money history stays, because the
    # books have to add up. The telegram id is replaced rather than deleted: it is NOT
    # NULL and UNIQUE, and the row is referenced by orders and subscriptions.
    #
    # Returns receipt order ids and names the caller has to unlink, or None if there was
    # no such live user. The pointer stays until that unlink succeeds, so a failed file
    # operation remains visible and retryable.
    from db import orders as orders_db
    from db import refunds as refunds_db

    async with connection.transaction():
        # Look up and validate under the same BEGIN IMMEDIATE transaction as the erase.
        # A payment/refund state transition can therefore win before this check or after
        # the commit, but cannot appear halfway through destructive cleanup.
        user = await get_by_id(user_id)
        if user is None or user["telegram_id"] <= 0:
            return None
        blocking_statuses = (
            orders_db.PENDING_REVIEW,
            orders_db.PAID,
            orders_db.DELIVERED,
            orders_db.PROBLEM,
            orders_db.REFUND_PENDING,
        )
        placeholders = ", ".join("?" for _ in blocking_statuses)
        blocking_order = await connection.fetch_one(
            f"SELECT id, status FROM orders WHERE user_id = ? "
            f"AND status IN ({placeholders}) ORDER BY id LIMIT 1",
            (user_id, *blocking_statuses),
        )
        if blocking_order:
            raise EraseBlockedError(
                f"Resolve order #{blocking_order['id']} "
                f"({orders_db.status_label(blocking_order['status'])}) before erasing"
            )
        blocking_refund = await connection.fetch_one(
            "SELECT r.id FROM refunds r LEFT JOIN orders o ON o.id = r.order_id "
            "WHERE r.status != ? AND (r.telegram_id = ? OR r.user_id = ? OR o.user_id = ?) "
            "ORDER BY r.id LIMIT 1",
            (refunds_db.COMPLETED, user["telegram_id"], user_id, user_id),
        )
        if blocking_refund:
            raise EraseBlockedError(
                f"Resolve refund #{blocking_refund['id']} before erasing personal data"
            )

        # Anything still running is closed first: an erased row keeps no working
        # subscription, and the reminder sweeps must not aim messages at an id that no
        # longer belongs to anybody.
        await connection.execute(
            "UPDATE orders SET status = ?, updated_at = datetime('now') "
            "WHERE user_id = ? AND status = ?",
            (orders_db.CANCELLED, user_id, orders_db.PENDING_RECEIPT),
        )
        receipts = await connection.fetch_all(
            "SELECT id AS order_id, receipt_file FROM orders "
            "WHERE user_id = ? AND receipt_file IS NOT NULL",
            (user_id,),
        )
        await connection.execute(
            "UPDATE subscriptions SET status = 'cancelled', credentials = NULL, "
            "notified_expired = 1, updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        await connection.execute(
            "DELETE FROM events WHERE telegram_id = ?", (user["telegram_id"],)
        )
        await connection.execute(
            "UPDATE invoices SET telegram_id = 0, "
            "status = CASE WHEN status IN ('quote', 'pending') THEN 'cancelled' ELSE status END "
            "WHERE telegram_id = ?",
            (user["telegram_id"],),
        )
        await connection.execute(
            "UPDATE audit_log SET target = ? WHERE target IN (?, ?)",
            (f"user:{user_id}", str(user["telegram_id"]), f"tg:{user['telegram_id']}"),
        )
        await connection.execute(
            "UPDATE audit_log SET admin_id = 0 WHERE admin_id = ?",
            (user["telegram_id"],),
        )
        await connection.execute(
            "UPDATE orders SET processed_by_telegram_id = NULL "
            "WHERE processed_by_telegram_id = ?",
            (user["telegram_id"],),
        )
        await connection.execute(
            "UPDATE refunds SET telegram_id = 0 "
            "WHERE (telegram_id = ? OR user_id = ?) AND status = ?",
            (user["telegram_id"], user_id, refunds_db.COMPLETED),
        )
        await connection.execute(
            "UPDATE users SET telegram_id = ?, username = NULL, first_name = 'erased', "
            "last_name = NULL, note = NULL, is_blocked = 1, role = 'user', "
            "payment_method_id = NULL, updated_at = datetime('now') WHERE id = ?",
            (-user_id, user_id),
        )
    logger.warning("Erased personal data of user %s", user_id)
    return {
        "telegram_id": user["telegram_id"],
        "receipts": receipts,
    }


def display_name(user: Optional[dict]) -> str:
    # Name for staff messages. Escaped: a customer picks their own first name.
    if not user:
        return "unknown user"
    parts = [user.get("first_name") or "", user.get("last_name") or ""]
    name = escape(" ".join(part for part in parts if part).strip(), quote=False)
    username = escape(user.get("username") or "", quote=False)
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or str(user.get("telegram_id", ""))
