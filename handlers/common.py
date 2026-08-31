# Shared helpers for handlers.

import logging
from typing import Optional

from telethon.errors import MessageNotModifiedError

from config import IMAGES_DIR
from db import products as products_db
from db import settings
from db import users as users_db
from services import access
from utils import keyboards, texts

logger = logging.getLogger(__name__)


async def resolve_user(event) -> tuple[Optional[dict], object]:
    sender = await event.get_sender()
    if sender is None:
        return None, None
    user = await users_db.get_or_create(
        telegram_id=sender.id,
        username=getattr(sender, "username", None),
        first_name=getattr(sender, "first_name", None),
        last_name=getattr(sender, "last_name", None),
    )
    return user, sender


async def guard(event) -> Optional[dict]:
    # Return the user we may serve, or None after explaining why we may not.
    if not getattr(event, "is_private", False):
        if hasattr(event, "answer"):
            await event.answer("Open the bot in a private chat", alert=True)
        else:
            await event.respond("Open the bot in a private chat.")
        return None

    user, sender = await resolve_user(event)
    if user is None:
        return None
    # Bind every later response to this exact internal profile. If erasure wins and the
    # same Telegram account starts again, an old handler must not send retained history
    # from the erased row to the newly created row.
    event._narromarket_user_id = user["id"]

    if user["is_blocked"]:
        await reply(event, texts.BLOCKED)
        return None

    if settings.get_bool("require_username") and not getattr(sender, "username", None):
        await reply(event, texts.NO_USERNAME)
        return None

    return user


async def _for_live_sender(event, operation, lifecycle_held: bool):
    if lifecycle_held:
        return await operation()
    telegram_id = int(getattr(event, "sender_id", 0) or 0)
    if telegram_id <= 0:
        return False
    live = await users_db.get(telegram_id)
    if not live:
        return False
    expected_user_id = int(getattr(event, "_narromarket_user_id", 0) or 0)
    if expected_user_id <= 0 or live["id"] != expected_user_id:
        return False
    async with users_db.lifecycle_lock(live["id"]):
        current = await users_db.get_by_id(live["id"])
        if not current or current["telegram_id"] != telegram_id:
            return False
        return await operation()


async def reply(
    event, text: str, buttons=None, *, file=None, lifecycle_held: bool = False
) -> bool:
    async def deliver():
        if hasattr(event, "answer"):
            try:
                await event.answer()
            except Exception as error:
                logger.debug("Callback answer failed: %s", error)
        await event.respond(texts.clamp(text), buttons=buttons, parse_mode="html", file=file)
        return True

    return await _for_live_sender(event, deliver, lifecycle_held)


async def edit_or_reply(
    event, text: str, buttons=None, *, lifecycle_held: bool = False
) -> bool:
    async def deliver():
        body = texts.clamp(text)
        try:
            await event.edit(body, buttons=buttons, parse_mode="html")
            return True
        except MessageNotModifiedError:
            # The screen is already exactly this. Sending it again would just duplicate it.
            return True
        except Exception as error:
            logger.debug("Could not edit message, sending a new one: %s", error)
        try:
            await event.respond(body, buttons=buttons, parse_mode="html")
            return True
        except Exception as error:
            logger.warning("Could not render message: %s", error)
            return False

    return await _for_live_sender(event, deliver, lifecycle_held)


async def has_personal(user: Optional[dict]) -> bool:
    if not user:
        return False
    return bool(await products_db.list_personal(user["id"]))


async def show_main_menu(event, user: dict, edit: bool = False, text: str = None) -> None:
    buttons = keyboards.main_menu(await has_personal(user), access.is_staff(user))
    body = text or texts.welcome()
    if edit:
        await edit_or_reply(event, body, buttons)
    else:
        await reply(event, body, buttons)


def image_path(product: dict) -> Optional[str]:
    # A bare file name inside the images folder, and nothing else. Without the
    # containment check "../.env" or an absolute path would be read straight off the
    # server and sent to whoever opens the product card.
    name = (product.get("image") or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    try:
        base = IMAGES_DIR.resolve()
        path = (base / name).resolve()
    except OSError:
        return None
    if path.parent != base or not path.is_file():
        return None
    return str(path)


def callback_arg(event, index: int = 1) -> str:
    return event.data.decode().split(":")[index]


def callback_int(event, index: int = 1) -> int:
    try:
        return int(callback_arg(event, index))
    except (ValueError, IndexError):
        return 0
