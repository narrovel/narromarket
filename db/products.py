# Catalog products and per-client personal offers.
#
# owner_user_id = 0 means the product is visible to everyone.
# owner_user_id = users.id means it is a personal offer for that client only.

from typing import Optional

from db import connection

PUBLIC = 0

EDITABLE_FIELDS = (
    "slug",
    "name",
    "emoji",
    "price_stars",
    "price_rub",
    "duration_days",
    "short_description",
    "description",
    "instruction",
    "image",
    "is_active",
    "sort_order",
    "owner_user_id",
)


async def get(product_id: int) -> Optional[dict]:
    return await connection.fetch_one("SELECT * FROM products WHERE id = ?", (product_id,))


async def get_by_slug(slug: str, owner_user_id: int = PUBLIC) -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM products WHERE slug = ? AND owner_user_id = ?",
        (slug, owner_user_id),
    )


async def list_public(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM products WHERE owner_user_id = 0"
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY sort_order, name, id"
    return await connection.fetch_all(sql)


async def list_personal(user_id: int, active_only: bool = True) -> list[dict]:
    if not user_id:
        return []
    sql = "SELECT * FROM products WHERE owner_user_id = ?"
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY sort_order, name, id"
    return await connection.fetch_all(sql, (user_id,))


async def public_catalog_for(user_id: int) -> list[dict]:
    # Public catalog without products shadowed by a personal offer.
    personal_slugs = {item["slug"] for item in await list_personal(user_id)}
    return [item for item in await list_public() if item["slug"] not in personal_slugs]


async def create(**fields) -> int:
    data = {key: value for key, value in fields.items() if key in EDITABLE_FIELDS}
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    return await connection.execute(
        f"INSERT INTO products ({columns}) VALUES ({placeholders})",
        list(data.values()),
    )


async def update(product_id: int, **fields) -> None:
    data = {key: value for key, value in fields.items() if key in EDITABLE_FIELDS}
    if data:
        await connection.update_row("products", product_id, **data)


async def delete(product_id: int) -> None:
    # All three steps or none: an interrupted delete would detach live subscriptions
    # from a product that still exists, and nothing would repair that.
    async with connection.transaction():
        await connection.execute(
            "UPDATE subscriptions SET product_id = NULL WHERE product_id = ?", (product_id,)
        )
        await connection.execute(
            "UPDATE orders SET product_id = NULL WHERE product_id = ?", (product_id,)
        )
        await connection.execute("DELETE FROM products WHERE id = ?", (product_id,))


async def slug_taken(slug: str, owner_user_id: int, exclude_id: int = 0) -> bool:
    row = await connection.fetch_one(
        "SELECT id FROM products WHERE slug = ? AND owner_user_id = ? AND id != ?",
        (slug, owner_user_id, exclude_id),
    )
    return row is not None


async def owners() -> list[dict]:
    # Clients that have at least one personal offer.
    return await connection.fetch_all(
        """
        SELECT u.*, COUNT(p.id) AS offers
        FROM products p
        JOIN users u ON u.id = p.owner_user_id
        WHERE p.owner_user_id != 0
        GROUP BY p.owner_user_id
        ORDER BY u.id
        """
    )
