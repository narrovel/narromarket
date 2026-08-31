# Outgoing messages to customers and staff.

import asyncio
import logging

from telethon.errors import FloodWaitError

from db import users as users_db
from utils import texts

logger = logging.getLogger(__name__)

# Waiting longer than this is not worth blocking a broadcast for.
MAX_FLOOD_WAIT = 60


async def to_user(
    client,
    telegram_id: int,
    text: str,
    buttons=None,
    *,
    expected_user_id: int = None,
    lifecycle_held: bool = False,
) -> bool:
    # Last stop before Telegram: an oversized message would be dropped whole.
    body = texts.clamp(text)
    if lifecycle_held:
        return await _deliver(client, telegram_id, body, buttons)
    live = await users_db.get(int(telegram_id or 0))
    if not live or (expected_user_id is not None and live["id"] != int(expected_user_id)):
        logger.info("Message to removed or unknown user %s suppressed", telegram_id)
        return False
    async with users_db.lifecycle_lock(live["id"]):
        current = await users_db.get_by_id(live["id"])
        if not current or current["telegram_id"] != telegram_id:
            logger.info("Message to removed user %s suppressed", telegram_id)
            return False
        return await _deliver(client, telegram_id, body, buttons)


async def _deliver(client, telegram_id: int, body: str, buttons=None) -> bool:
    for attempt in (1, 2):
        try:
            await client.send_message(telegram_id, body, buttons=buttons, parse_mode="html")
            return True
        except FloodWaitError as error:
            # Report rate limiting separately from permanent delivery failures.
            if attempt == 2 or error.seconds > MAX_FLOOD_WAIT:
                logger.warning(
                    "Rate limited for %ss, message to %s not delivered",
                    error.seconds,
                    telegram_id,
                )
                return False
            logger.info("Rate limited for %ss, waiting", error.seconds)
            await asyncio.sleep(error.seconds + 1)
        except Exception as error:
            logger.warning("Message to %s not delivered: %s", telegram_id, error)
            return False
    return False


async def to_staff(client, text: str, buttons=None) -> int:
    delivered = 0
    for recipient in await users_db.staff_recipients():
        recipient_id = recipient["id"]
        async with users_db.lifecycle_lock(recipient_id):
            delivered += await _to_current_staff(client, recipient, text, buttons)
    return delivered


async def _to_current_staff(client, recipient: dict, text: str, buttons=None) -> bool:
    current = await users_db.get_by_id(recipient["id"])
    if (
        not current
        or current["telegram_id"] != recipient["telegram_id"]
        or current["is_blocked"]
        or current["role"] not in ("manager", "admin", "owner")
    ):
        return False
    return await to_user(
        client,
        current["telegram_id"],
        text,
        buttons,
        lifecycle_held=True,
    )
