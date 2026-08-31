# Bank details used for manual transfer payments.

from typing import Optional

from db import connection

KIND_TITLES = {"sbp": "📱 Fast payments", "card": "💳 Card"}
EDITABLE_FIELDS = ("title", "kind", "details", "bank", "holder", "is_active")


async def get(method_id: int) -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM payment_methods WHERE id = ?", (method_id,)
    )


async def list_all(active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM payment_methods WHERE is_deleted = 0"
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY is_default DESC, id"
    return await connection.fetch_all(sql)


async def get_default() -> Optional[dict]:
    return await connection.fetch_one(
        "SELECT * FROM payment_methods "
        "WHERE is_default = 1 AND is_active = 1 AND is_deleted = 0 LIMIT 1"
    )


async def create(title: str, kind: str, details: str, bank: str = "", holder: str = "") -> int:
    async with connection.transaction():
        method_id = await connection.execute(
            "INSERT INTO payment_methods (title, kind, details, bank, holder) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, kind, details, bank, holder),
        )
        default_exists = await connection.fetch_one(
            "SELECT 1 FROM payment_methods "
            "WHERE is_default = 1 AND is_active = 1 AND is_deleted = 0 LIMIT 1"
        )
        if not default_exists:
            await connection.execute(
                "UPDATE payment_methods SET is_default = 1 WHERE id = ?", (method_id,)
            )
    return method_id


async def update(method_id: int, **fields) -> None:
    data = {key: value for key, value in fields.items() if key in EDITABLE_FIELDS}
    if not data:
        return
    async with connection.transaction():
        await connection.update_row("payment_methods", method_id, **data)
        if data.get("is_active") == 0:
            await _promote_default_if_needed(method_id)


async def set_default(method_id: int) -> bool:
    async with connection.transaction():
        method = await get(method_id)
        if not method or not method["is_active"] or method["is_deleted"]:
            return False
        await connection.execute("UPDATE payment_methods SET is_default = 0")
        await connection.execute(
            "UPDATE payment_methods SET is_default = 1 WHERE id = ?", (method_id,)
        )
    return True


async def _promote_default_if_needed(method_id: int) -> None:
    current = await get(method_id)
    if not current or not current["is_default"]:
        return
    await connection.execute(
        "UPDATE payment_methods SET is_default = 0 WHERE id = ?", (method_id,)
    )
    replacement = await connection.fetch_one(
        "SELECT id FROM payment_methods WHERE is_active = 1 AND is_deleted = 0 "
        "AND id != ? ORDER BY id LIMIT 1",
        (method_id,),
    )
    if replacement:
        await connection.execute(
            "UPDATE payment_methods SET is_default = 1 WHERE id = ?", (replacement["id"],)
        )


async def delete(method_id: int) -> None:
    async with connection.transaction():
        await _promote_default_if_needed(method_id)
        await connection.execute(
            "UPDATE users SET payment_method_id = NULL WHERE payment_method_id = ?",
            (method_id,),
        )
        await connection.execute(
            "UPDATE payment_methods SET is_deleted = 1, is_default = 0, is_active = 0 "
            "WHERE id = ?",
            (method_id,),
        )


async def for_user(user: dict) -> Optional[dict]:
    # Client specific details if assigned, otherwise the default ones.
    if user and user.get("payment_method_id"):
        method = await get(user["payment_method_id"])
        if method and method["is_active"] and not method["is_deleted"]:
            return method
    return await get_default()


def describe(method: dict) -> str:
    # Staff typed values go into an html message, so they are escaped like any other.
    from utils import texts

    kind = KIND_TITLES.get(method["kind"], method["kind"])
    parts = [f"{kind} <code>{texts.escape(method['details'])}</code>"]
    if method.get("bank"):
        parts.append(texts.escape(method["bank"]))
    if method.get("holder"):
        parts.append(texts.escape(method["holder"]))
    return ", ".join(parts)
