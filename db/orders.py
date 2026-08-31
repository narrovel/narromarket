# Orders: one payment is one order. Subscriptions are stored separately.

from typing import Optional

from db import connection

PENDING_RECEIPT = "pending_receipt"
PENDING_REVIEW = "pending_review"
PAID = "paid"
DELIVERED = "delivered"
COMPLETED = "completed"
PROBLEM = "problem"
REFUND_PENDING = "refund_pending"
REJECTED = "rejected"
CANCELLED = "cancelled"
REFUNDED = "refunded"
PAYMENT_EXPIRED = "payment_expired"

STATUS_TITLES = {
    PENDING_RECEIPT: ("📎", "waiting for receipt"),
    PENDING_REVIEW: ("🔍", "payment under review"),
    PAID: ("💰", "paid, access pending"),
    DELIVERED: ("📩", "access sent"),
    COMPLETED: ("✅", "completed"),
    PROBLEM: ("🆘", "problem reported"),
    REFUND_PENDING: ("⏳", "refund pending"),
    REJECTED: ("❌", "rejected"),
    CANCELLED: ("❌", "cancelled"),
    REFUNDED: ("💸", "refunded"),
    PAYMENT_EXPIRED: ("⏰", "payment window closed"),
}

NEEDS_ADMIN = (PENDING_REVIEW, PAID, PROBLEM, REFUND_PENDING)

# Statuses that must stop a second payment for the same product. DELIVERED is missing on
# purpose: access is already handed over, so renewing must stay possible even when the
# customer never taps the confirm button.
BLOCKING_STATUSES = (PENDING_RECEIPT, PENDING_REVIEW, PAID, PROBLEM, REFUND_PENDING)

# A closed order never turns back into a paying one.
FINAL_STATUSES = (COMPLETED, REJECTED, CANCELLED, REFUNDED, PAYMENT_EXPIRED)

# Money that was taken and not given back. Keyed on paid_at rather than on the current
# status: an order moves through the workflow for many reasons, and a status set says
# nothing about whether a payment ever arrived.
REVERSED_STATUSES = (REFUNDED, REJECTED, CANCELLED)
PAID_CONDITION = (
    f"paid_at IS NOT NULL AND status NOT IN ({', '.join('?' for _ in REVERSED_STATUSES)})"
)

_WITH_USER = """
    SELECT o.*, u.telegram_id, u.username, u.first_name, u.last_name
    FROM orders o
    JOIN users u ON u.id = o.user_id
"""


def status_label(status: str) -> str:
    emoji, title = STATUS_TITLES.get(status, ("❓", status))
    return f"{emoji} {title}"


async def create(
    user_id: int,
    product: dict,
    amount_stars: int = 0,
    amount_rub: int = 0,
    payment_method: str = "stars",
    status: str = PENDING_RECEIPT,
    is_personal: bool = False,
    is_renewal: bool = False,
    payment_charge_id: str = None,
    payment_provider_charge_id: str = None,
    payment_recipient_id: int = None,
    payment_method_id: int = None,
    subscription_id: int = None,
) -> int:
    return await connection.execute(
        "INSERT INTO orders (user_id, product_id, product_slug, product_name, emoji, "
        "subscription_id, is_personal, is_renewal, duration_days, amount_stars, amount_rub, "
        "payment_method, payment_method_id, payment_charge_id, payment_provider_charge_id, "
        "payment_recipient_id, status, paid_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "CASE WHEN ? = 'paid' THEN datetime('now') END)",
        (
            user_id,
            product["id"],
            product["slug"],
            product["name"],
            product["emoji"],
            subscription_id,
            1 if is_personal else 0,
            1 if is_renewal else 0,
            product["duration_days"],
            int(amount_stars or 0),
            int(amount_rub or 0),
            payment_method,
            payment_method_id,
            payment_charge_id,
            payment_provider_charge_id,
            payment_recipient_id,
            status,
            status,
        ),
    )


async def get(order_id: int) -> Optional[dict]:
    return await connection.fetch_one(f"{_WITH_USER} WHERE o.id = ?", (order_id,))


async def by_charge_id(charge_id: str) -> Optional[dict]:
    # A redelivered payment update carries a charge id that is already on an order.
    if not charge_id:
        return None
    return await connection.fetch_one(
        f"{_WITH_USER} WHERE o.payment_charge_id = ?", (charge_id,)
    )


async def set_status(order_id: int, status: str, processed_by: int = None) -> None:
    await update(
        order_id,
        status=status,
        **({"processed_by_telegram_id": processed_by} if processed_by is not None else {}),
    )


async def claim_status(
    order_id: int, new_status: str, expected: tuple, processed_by: int = None
) -> bool:
    # Move the order to new_status only if it is still in one of the expected states.
    # Two managers tapping the same button leave exactly one winner.
    placeholders = ", ".join("?" for _ in expected)
    assignments = "status = ?, updated_at = datetime('now')"
    if new_status == PAID:
        # Stamped once, the first time the money is confirmed.
        assignments += ", paid_at = COALESCE(paid_at, datetime('now'))"
    params = [new_status]
    if processed_by is not None:
        assignments += (
            ", processed_by_telegram_id = CASE WHEN EXISTS ("
            "SELECT 1 FROM users WHERE telegram_id = ? AND telegram_id > 0"
            ") THEN ? ELSE NULL END"
        )
        params.extend((processed_by, processed_by))
    changed = await connection.execute_change(
        f"UPDATE orders SET {assignments} WHERE id = ? AND status IN ({placeholders})",
        [*params, order_id, *expected],
    )
    return changed == 1


async def cancel_unpaid(order_id: int, expected: tuple, processed_by: int) -> bool:
    """Cancel only while no payment has been recorded, with a live actor snapshot."""
    placeholders = ", ".join("?" for _ in expected)
    changed = await connection.execute_change(
        "UPDATE orders SET status = ?, processed_by_telegram_id = CASE WHEN EXISTS ("
        "SELECT 1 FROM users WHERE telegram_id = ? AND telegram_id > 0"
        ") THEN ? ELSE NULL END, updated_at = datetime('now') "
        f"WHERE id = ? AND paid_at IS NULL AND status IN ({placeholders})",
        (CANCELLED, processed_by, processed_by, order_id, *expected),
    )
    return changed == 1


async def mark_reversed(order_id: int) -> None:
    await connection.execute(
        "UPDATE orders SET reversed_at = COALESCE(reversed_at, datetime('now')), "
        "updated_at = datetime('now') WHERE id = ?",
        (order_id,),
    )


async def update(order_id: int, **fields) -> None:
    allowed = (
        "status",
        "processed_by_telegram_id",
        "receipt_file",
        "subscription_id",
        "payment_charge_id",
        "payment_provider_charge_id",
        "payment_recipient_id",
        "payment_method_id",
        "amount_rub",
        "amount_stars",
    )
    data = {key: value for key, value in fields.items() if key in allowed}
    if not data:
        return
    if "processed_by_telegram_id" not in data:
        await connection.update_row("orders", order_id, **data)
        return
    actor_id = data.pop("processed_by_telegram_id")
    async with connection.transaction():
        live_actor = None
        if actor_id is not None:
            live_actor = await connection.fetch_one(
                "SELECT 1 FROM users WHERE telegram_id = ? AND telegram_id > 0",
                (actor_id,),
            )
        data["processed_by_telegram_id"] = actor_id if live_actor else None
        await connection.update_row("orders", order_id, **data)


async def list_for_user(user_id: int, limit: int = 10) -> list[dict]:
    return await connection.fetch_all(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )


async def oldest_awaiting_receipt(user_id: int) -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM orders WHERE user_id = ? AND status = ? ORDER BY id LIMIT 1",
        (user_id, PENDING_RECEIPT),
    )


async def recently_expired_transfer(user_id: int, days: int = 7) -> Optional[dict]:
    # A transfer the sweep closed for a missing receipt. The customer may well have paid
    # and simply been slow, so a late receipt has to be able to reopen it.
    return await connection.fetch_one(
        "SELECT * FROM orders WHERE user_id = ? AND status = ? AND payment_method = ? "
        "AND datetime(updated_at, '+' || ? || ' days') > datetime('now') "
        "ORDER BY id DESC LIMIT 1",
        (user_id, PAYMENT_EXPIRED, "transfer", days),
    )


async def receipt_candidates(user_id: int, late_days: int = 7) -> list[dict]:
    """Return every order a bare receipt could belong to, including recent timeouts."""
    return await connection.fetch_all(
        "SELECT * FROM orders WHERE user_id = ? AND (status = ? OR ("
        "status = ? AND payment_method = 'transfer' "
        "AND datetime(updated_at, '+' || ? || ' days') > datetime('now'))) "
        "ORDER BY id",
        (user_id, PENDING_RECEIPT, PAYMENT_EXPIRED, late_days),
    )


async def attach_receipt(
    order_id: int,
    filename: str,
    user_id: int,
    telegram_id: int,
    late_days: int = 7,
) -> bool:
    """Attach once, reopening only a recently expired transfer order."""
    changed = await connection.execute_change(
        "UPDATE orders SET status = ?, receipt_file = ?, updated_at = datetime('now') "
        "WHERE id = ? AND user_id = ? AND receipt_file IS NULL AND EXISTS ("
        "SELECT 1 FROM users WHERE users.id = orders.user_id "
        "AND users.telegram_id = ? AND users.telegram_id > 0"
        ") AND (status = ? OR ("
        "status = ? AND payment_method = 'transfer' "
        "AND datetime(updated_at, '+' || ? || ' days') > datetime('now')))",
        (
            PENDING_REVIEW,
            filename,
            order_id,
            user_id,
            telegram_id,
            PENDING_RECEIPT,
            PAYMENT_EXPIRED,
            late_days,
        ),
    )
    return changed == 1


async def count_awaiting_receipt(user_id: int) -> int:
    return await connection.fetch_value(
        "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = ?",
        (user_id, PENDING_RECEIPT),
        0,
    )


async def needs_attention(limit: int = 100, offset: int = 0) -> list[dict]:
    placeholders = ", ".join("?" for _ in NEEDS_ADMIN)
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE o.status IN ({placeholders}) ORDER BY o.id LIMIT ? OFFSET ?",
        (*NEEDS_ADMIN, limit, offset),
    )


async def count_needs_attention() -> int:
    placeholders = ", ".join("?" for _ in NEEDS_ADMIN)
    return await connection.fetch_value(
        f"SELECT COUNT(*) FROM orders WHERE status IN ({placeholders})", NEEDS_ADMIN, 0
    )


async def open_for_product(user_id: int, product_slug: str) -> Optional[dict]:
    # Order that must be closed before this product can be bought again.
    placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
    return await connection.fetch_one(
        f"SELECT * FROM orders WHERE user_id = ? AND product_slug = ? "
        f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (user_id, product_slug, *BLOCKING_STATUSES),
    )


async def stale_receipt_orders(minutes: int, limit: int = 500) -> list[dict]:
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE o.status = ? "
        "AND datetime(o.created_at, '+' || ? || ' minutes') < datetime('now') "
        "ORDER BY o.id LIMIT ?",
        (PENDING_RECEIPT, minutes, limit),
    )


async def stale_review_orders(hours: int, limit: int = 500) -> list[dict]:
    # Receipts nobody looked at. Left alone they block the customer from buying again.
    return await connection.fetch_all(
        f"{_WITH_USER} WHERE o.status = ? "
        "AND datetime(o.updated_at, '+' || ? || ' hours') < datetime('now') "
        "ORDER BY o.id LIMIT ?",
        (PENDING_REVIEW, hours, limit),
    )


async def inconsistencies() -> dict:
    # Report inconsistent states that need manual reconciliation after an interrupted
    # multi-step operation.
    reversed_marks = ", ".join("?" for _ in REVERSED_STATUSES)
    return {
        "paid_without_subscription": await connection.fetch_all(
            f"SELECT id FROM orders WHERE paid_at IS NOT NULL AND subscription_id IS NULL "
            f"AND status NOT IN ({reversed_marks}) LIMIT 50",
            REVERSED_STATUSES,
        ),
        "review_without_receipt": await connection.fetch_all(
            "SELECT id FROM orders WHERE status = ? AND receipt_file IS NULL LIMIT 50",
            (PENDING_REVIEW,),
        ),
        "closed_but_still_granting": await connection.fetch_all(
            """
            SELECT o.id FROM orders o
            JOIN subscriptions s ON s.id = o.subscription_id
            WHERE o.status IN ('refunded', 'rejected', 'cancelled')
              AND o.paid_at IS NOT NULL AND o.reversed_at IS NULL
              AND s.status = 'active'
            LIMIT 50
            """
        ),
    }


async def receipts_to_forget(days: int, limit: int = 200) -> list[dict]:
    # Access may be delivered without the customer ever tapping Confirm. Treat those
    # receipts like closed orders for retention; pending reviews and problems stay put.
    retention_statuses = (*FINAL_STATUSES, DELIVERED)
    placeholders = ", ".join("?" for _ in retention_statuses)
    return await connection.fetch_all(
        f"SELECT id, receipt_file FROM orders WHERE receipt_file IS NOT NULL "
        f"AND status IN ({placeholders}) "
        "AND datetime(updated_at, '+' || ? || ' days') < datetime('now') "
        "ORDER BY id LIMIT ?",
        (*retention_statuses, days, limit),
    )


async def known_receipt_files() -> set:
    rows = await connection.fetch_all(
        "SELECT receipt_file FROM orders WHERE receipt_file IS NOT NULL"
    )
    return {row["receipt_file"] for row in rows}


async def forget_receipt(order_id: int, expected_file: str = None) -> bool:
    # Clear only the pointer whose file was just removed. The comparison prevents a
    # stale cleanup task from forgetting a replacement uploaded under another name.
    if expected_file is None:
        changed = await connection.execute_change(
            "UPDATE orders SET receipt_file = NULL, updated_at = datetime('now') "
            "WHERE id = ? AND receipt_file IS NOT NULL",
            (order_id,),
        )
    else:
        changed = await connection.execute_change(
            "UPDATE orders SET receipt_file = NULL, updated_at = datetime('now') "
            "WHERE id = ? AND receipt_file = ?",
            (order_id, expected_file),
        )
    return changed == 1
