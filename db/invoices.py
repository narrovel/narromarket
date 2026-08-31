# Telegram Stars invoices, tracked so a cancelled or stale invoice cannot be paid.

from typing import Optional

from config import INVOICE_CURRENCY, INVOICE_TTL_MINUTES
from db import connection

_TTL_MINUTES = INVOICE_TTL_MINUTES
# A successful pre-checkout normally produces a paid update immediately. Keep a full
# day for delayed Telegram delivery or downtime, but never let an abandoned approval
# block this product forever.
APPROVED_GRACE_MINUTES = 24 * 60

_LIVE_PAYMENT_WINDOW = (
    "((precheckout_approved_at IS NULL AND "
    f"datetime(created_at, '+{_TTL_MINUTES} minutes') > datetime('now')) OR "
    "(precheckout_approved_at IS NOT NULL AND "
    f"datetime(precheckout_approved_at, '+{APPROVED_GRACE_MINUTES} minutes') "
    "> datetime('now')))"
)
_EXPIRED_PAYMENT_WINDOW = (
    "((precheckout_approved_at IS NULL AND "
    f"datetime(created_at, '+{_TTL_MINUTES} minutes') <= datetime('now')) OR "
    "(precheckout_approved_at IS NOT NULL AND "
    f"datetime(precheckout_approved_at, '+{APPROVED_GRACE_MINUTES} minutes') "
    "<= datetime('now')))"
)

PENDING = "pending"
PAID = "paid"
CANCELLED = "cancelled"
QUOTE = "quote"


async def create(
    telegram_id: int,
    product: dict,
    token: str,
    currency: str = INVOICE_CURRENCY,
) -> Optional[int]:
    """Create one live invoice and freeze the terms it represents.

    The expiry cleanup, duplicate check and insert share a write transaction. Two
    callback tasks can therefore never both see an empty slot and issue two payable
    invoices for the same customer and product.
    """
    product_id = int(product["id"])
    product_slug = product["slug"]
    async with connection.transaction():
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_slug = ? "
            f"AND status = ? AND {_EXPIRED_PAYMENT_WINDOW}",
            (CANCELLED, telegram_id, product_slug, PENDING),
        )
        # A personal offer replaces the public product with the same slug. Its terms are
        # a different entitlement, so an older invoice for the shadowed row is closed.
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_slug = ? "
            "AND product_id != ? AND status = ? AND precheckout_approved_at IS NULL",
            (CANCELLED, telegram_id, product_slug, product_id, PENDING),
        )
        existing = await connection.fetch_one(
            "SELECT id FROM invoices WHERE telegram_id = ? AND product_slug = ? "
            f"AND status = ? AND {_LIVE_PAYMENT_WINDOW}",
            (telegram_id, product_slug, PENDING),
        )
        if existing:
            return None
        return await connection.execute(
            "INSERT INTO invoices (telegram_id, product_id, product_slug, product_name, "
            "emoji, owner_user_id, amount_stars, duration_days, currency, token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_id,
                product_id,
                product["slug"],
                product["name"],
                product.get("emoji") or "📦",
                int(product.get("owner_user_id") or 0),
                int(product.get("price_stars") or 0),
                int(product.get("duration_days") or 0),
                currency,
                token,
            ),
        )


async def create_quote(
    telegram_id: int,
    product: dict,
    token: str,
    terms_hash: str,
    currency: str = INVOICE_CURRENCY,
) -> Optional[int]:
    """Store the terms screen as a one-time quote before creating a payable invoice."""
    if not terms_hash:
        raise ValueError("A quote requires a terms hash")
    product_id = int(product["id"])
    product_slug = product["slug"]
    async with connection.transaction():
        # A payable invoice wins over a new quote. Expired invoices are closed first so
        # the customer can review and buy again without waiting for a cleanup job.
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_slug = ? "
            f"AND status = ? AND {_EXPIRED_PAYMENT_WINDOW}",
            (CANCELLED, telegram_id, product_slug, PENDING),
        )
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_slug = ? "
            "AND product_id != ? AND status = ? AND precheckout_approved_at IS NULL",
            (CANCELLED, telegram_id, product_slug, product_id, PENDING),
        )
        existing = await connection.fetch_one(
            "SELECT id FROM invoices WHERE telegram_id = ? AND product_slug = ? "
            f"AND status = ? AND {_LIVE_PAYMENT_WINDOW}",
            (telegram_id, product_slug, PENDING),
        )
        if existing:
            return None

        # Only the newest screen may be confirmed. This also bounds the number of live
        # buttons when Telegram delivers the first tap more than once.
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_slug = ? "
            "AND status = ?",
            (CANCELLED, telegram_id, product_slug, QUOTE),
        )
        return await connection.execute(
            "INSERT INTO invoices (telegram_id, product_id, product_slug, product_name, "
            "emoji, owner_user_id, amount_stars, duration_days, currency, terms_hash, "
            "token, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telegram_id,
                product_id,
                product["slug"],
                product["name"],
                product.get("emoji") or "📦",
                int(product.get("owner_user_id") or 0),
                int(product.get("price_stars") or 0),
                int(product.get("duration_days") or 0),
                currency,
                terms_hash,
                token,
                QUOTE,
            ),
        )


async def get(token: str) -> Optional[dict]:
    return await connection.fetch_one("SELECT * FROM invoices WHERE token = ?", (token,))


async def activate_quote(token: str, telegram_id: int, terms_hash: str) -> Optional[int]:
    """Turn one matching, unexpired quote into the only live invoice for its product."""
    async with connection.transaction():
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE token = ? AND status = ? "
            "AND datetime(created_at, '+' || ? || ' minutes') <= datetime('now')",
            (CANCELLED, token, QUOTE, _TTL_MINUTES),
        )
        quote = await get(token)
        if (
            not quote
            or quote["status"] != QUOTE
            or int(quote["telegram_id"]) != int(telegram_id)
            or quote.get("terms_hash") != terms_hash
        ):
            return None

        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_slug = ? "
            f"AND status = ? AND {_EXPIRED_PAYMENT_WINDOW}",
            (CANCELLED, telegram_id, quote["product_slug"], PENDING),
        )
        existing = await connection.fetch_one(
            "SELECT id FROM invoices WHERE telegram_id = ? AND product_slug = ? "
            f"AND status = ? AND {_LIVE_PAYMENT_WINDOW}",
            (telegram_id, quote["product_slug"], PENDING),
        )
        if existing:
            return None

        changed = await connection.execute_change(
            "UPDATE invoices SET status = ?, created_at = datetime('now') "
            "WHERE id = ? AND status = ? AND terms_hash = ? "
            "AND datetime(created_at, '+' || ? || ' minutes') > datetime('now')",
            (PENDING, quote["id"], QUOTE, terms_hash, _TTL_MINUTES),
        )
        return quote["id"] if changed == 1 else None


async def claim_for_payment(token: str) -> bool:
    # Compare and swap: only the first delivery of a payment wins the invoice. A
    # cancelled or already paid invoice is never resurrected.
    changed = await connection.execute_change(
        "UPDATE invoices SET status = ? WHERE token = ? AND status = ? "
        f"AND {_LIVE_PAYMENT_WINDOW}",
        (PAID, token, PENDING),
    )
    return changed == 1


async def approve_precheckout(token: str) -> bool:
    """Record approval while the invoice is still pending and inside its TTL."""
    changed = await connection.execute_change(
        "UPDATE invoices SET precheckout_approved_at = "
        "COALESCE(precheckout_approved_at, datetime('now')) "
        f"WHERE token = ? AND status = ? AND {_LIVE_PAYMENT_WINDOW}",
        (token, PENDING),
    )
    return changed == 1


async def cancel(token: str) -> bool:
    changed = await connection.execute_change(
        "UPDATE invoices SET status = ? WHERE token = ? AND status = ?",
        (CANCELLED, token, PENDING),
    )
    return changed == 1


async def cancel_quote(token: str) -> bool:
    changed = await connection.execute_change(
        "UPDATE invoices SET status = ? WHERE token = ? AND status = ?",
        (CANCELLED, token, QUOTE),
    )
    return changed == 1


async def cancel_for_user(telegram_id: int, product_id: int = None) -> None:
    if product_id:
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND product_id = ? AND status = ?",
            (CANCELLED, telegram_id, product_id, PENDING),
        )
    else:
        await connection.execute(
            "UPDATE invoices SET status = ? WHERE telegram_id = ? AND status = ?",
            (CANCELLED, telegram_id, PENDING),
        )


async def has_pending_for_product(telegram_id: int, product_id: int) -> bool:
    product_slug = await connection.fetch_value(
        "SELECT slug FROM products WHERE id = ?", (product_id,)
    )
    if not product_slug:
        return False
    row = await connection.fetch_one(
        "SELECT id FROM invoices WHERE telegram_id = ? AND product_slug = ? AND status = ? "
        f"AND {_LIVE_PAYMENT_WINDOW}",
        (telegram_id, product_slug, PENDING),
    )
    return row is not None


async def has_pending(telegram_id: int) -> bool:
    count = await connection.fetch_value(
        "SELECT COUNT(*) FROM invoices WHERE telegram_id = ? AND status = ? "
        f"AND {_LIVE_PAYMENT_WINDOW}",
        (telegram_id, PENDING),
        0,
    )
    return count > 0
