# User events and the staff audit trail.

import json
from typing import Any

from db import connection


async def event(event_type: str, telegram_id: int = None, data: Any = None) -> None:
    payload = None
    if data is not None:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    async with connection.transaction():
        # Erasure deletes this history. Validate and insert under the same write
        # transaction so a late handler cannot put the old Telegram id back afterwards.
        if telegram_id is not None:
            live = await connection.fetch_one(
                "SELECT 1 FROM users WHERE telegram_id = ? AND telegram_id > 0",
                (telegram_id,),
            )
            if live is None:
                telegram_id = None
        await connection.execute(
            "INSERT INTO events (telegram_id, type, data) VALUES (?, ?, ?)",
            (telegram_id, event_type, payload),
        )


async def action(admin_id: int, name: str, target: str = None, details: str = None) -> None:
    async with connection.transaction():
        live = await connection.fetch_one(
            "SELECT 1 FROM users WHERE telegram_id = ? AND telegram_id > 0",
            (admin_id,),
        )
        await connection.execute(
            "INSERT INTO audit_log (admin_id, action, target, details) VALUES (?, ?, ?, ?)",
            (admin_id if live else 0, name, target, details),
        )


async def recent_actions(limit: int = 20) -> list[dict]:
    return await connection.fetch_all(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    )


async def prune_events(days: int = 90) -> int:
    # Screen views are only useful while they are fresh; nothing reads them after that.
    return await connection.execute_change(
        "DELETE FROM events WHERE created_at < datetime('now', '-' || ? || ' days')",
        (days,),
    )
