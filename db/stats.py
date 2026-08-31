# Numbers for the admin dashboard.

from db import connection, orders, refunds, subscriptions
from utils import dates


async def dashboard(since: str = None, until: str = None) -> dict:
    money_where = orders.PAID_CONDITION
    money_params = list(orders.REVERSED_STATUSES)
    if since:
        money_where += " AND paid_at >= ?"
        money_params.append(since)
    if until:
        money_where += " AND paid_at < ?"
        money_params.append(until)

    totals = await connection.fetch_one(
        f"""
        SELECT
            COALESCE(SUM(amount_stars), 0) AS stars,
            COALESCE(SUM(amount_rub), 0) AS rub,
            COUNT(*) AS paid_orders
        FROM orders WHERE {money_where}
        """,
        money_params,
    )

    # date('now') is a UTC day, while everything the shop shows is in the configured
    # timezone. Comparing against precomputed bounds also keeps the query indexable.
    today_start, today_end = dates.day_bounds_utc(0)

    needs_attention = await orders.count_needs_attention()
    # Order-linked refunds already contribute through REFUND_PENDING. Automatic
    # refused payments have no order, so count only those separately.
    needs_attention += await connection.fetch_value(
        "SELECT COUNT(*) FROM refunds WHERE order_id IS NULL AND status != ?",
        (refunds.COMPLETED,),
        0,
    )

    return {
        "users": await connection.fetch_value("SELECT COUNT(*) FROM users", (), 0),
        "users_today": await connection.fetch_value(
            "SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?",
            (today_start, today_end),
            0,
        ),
        "active_subscriptions": await connection.fetch_value(
            "SELECT COUNT(*) FROM subscriptions WHERE status = ?",
            (subscriptions.ACTIVE,),
            0,
        ),
        "orders_today": await connection.fetch_value(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND created_at < ?",
            (today_start, today_end),
            0,
        ),
        "revenue_stars": totals["stars"],
        "revenue_rub": totals["rub"],
        "paid_orders": totals["paid_orders"],
        "needs_attention": needs_attention,
        "products": await connection.fetch_value(
            "SELECT COUNT(*) FROM products WHERE owner_user_id = 0", (), 0
        ),
        "personal_offers": await connection.fetch_value(
            "SELECT COUNT(*) FROM products WHERE owner_user_id != 0", (), 0
        ),
    }


async def by_status() -> list[dict]:
    return await connection.fetch_all(
        "SELECT status, COUNT(*) AS count FROM orders GROUP BY status ORDER BY count DESC"
    )


async def top_products(limit: int = 5) -> list[dict]:
    # Grouped by slug alone: the name and emoji are per order snapshots, so grouping by
    # them too would split one product into a row per rename.
    return await connection.fetch_all(
        f"""
        SELECT MAX(emoji) AS emoji, MAX(product_name) AS product_name, COUNT(*) AS count
        FROM orders WHERE {orders.PAID_CONDITION}
        GROUP BY product_slug
        ORDER BY count DESC, product_name LIMIT ?
        """,
        (*orders.REVERSED_STATUSES, limit),
    )


async def expected_renewal_income() -> dict:
    row = await connection.fetch_one(
        """
        SELECT
            COALESCE(SUM(p.price_stars), 0) AS stars,
            COALESCE(SUM(p.price_rub), 0) AS rub
        FROM subscriptions s
        JOIN products p ON p.id = s.product_id
        WHERE s.status = ?
        """,
        (subscriptions.ACTIVE,),
    )
    return {"stars": row["stars"], "rub": row["rub"]}


async def revenue_for_month(offset_months: int = 0) -> dict:
    """Return payments and reversals recorded during one local calendar month."""
    start, end = dates.month_bounds_utc(offset_months)
    paid = await connection.fetch_one(
        """
        SELECT
            COALESCE(SUM(amount_stars), 0) AS stars,
            COALESCE(SUM(amount_rub), 0) AS rub,
            COUNT(*) AS orders
        FROM orders
        WHERE paid_at IS NOT NULL AND paid_at >= ? AND paid_at < ?
        """,
        (start, end),
    )
    reversed_row = await connection.fetch_one(
        """
        SELECT
            COALESCE(SUM(amount_stars), 0) AS reversed_stars,
            COALESCE(SUM(amount_rub), 0) AS reversed_rub,
            COUNT(*) AS reversed_orders
        FROM orders
        WHERE paid_at IS NOT NULL AND reversed_at IS NOT NULL
          AND reversed_at >= ? AND reversed_at < ?
        """,
        (start, end),
    )
    return {
        **paid,
        **reversed_row,
        "net_stars": paid["stars"] - reversed_row["reversed_stars"],
        "net_rub": paid["rub"] - reversed_row["reversed_rub"],
    }
