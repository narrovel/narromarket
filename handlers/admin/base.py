# Permission checks and shared pieces of the admin panel.

import logging
from typing import Optional

from telethon import Button

from handlers import common
from services import access
from utils import keyboards, states, texts

logger = logging.getLogger(__name__)

DENIED = "Not enough rights"


async def actor(event, section: str) -> Optional[dict]:
    # Staff member allowed into a section, or None after showing the refusal.
    if not getattr(event, "is_private", False):
        if hasattr(event, "answer"):
            await event.answer("Open the admin panel in a private chat", alert=True)
        else:
            await event.respond("Open the admin panel in a private chat.")
        return None

    user = await access.actor(event.sender_id)
    if user:
        event._narromarket_user_id = user["id"]
    if not access.can(user, section):
        if hasattr(event, "answer"):
            await event.answer(DENIED, alert=True)
        else:
            await event.respond(DENIED)
        return None
    return user


async def show(event, text: str, buttons=None) -> None:
    await common.edit_or_reply(event, texts.clamp(text), buttons)


async def respond(event, text: str, buttons=None, parse_mode: str = None) -> bool:
    """Reply only while the exact staff profile that opened the action is live."""
    del parse_mode  # Admin messages consistently use the shared HTML-safe sender.
    return await common.reply(event, text, buttons)


def home_row() -> list:
    return [Button.inline("◀️ Admin panel", "a:home")]


# Limit product lists to Telegram's message and keyboard constraints.
PAGE_SIZE = 12


def clamp_page(items: list, page: int) -> int:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    return max(0, min(page, total_pages - 1))


def page_slice(items: list, page: int) -> list:
    # Clamped exactly like the keyboard, otherwise a stale page number renders a header
    # with the total count, an empty body and buttons from page one.
    start = clamp_page(items, page) * PAGE_SIZE
    return items[start : start + PAGE_SIZE]


def paged_rows(items: list, page: int, page_prefix: str, label, data) -> list:
    rows, _ = keyboards.paginate(items, page, PAGE_SIZE, page_prefix, label, data)
    return rows


def page_from(event, index: int) -> int:
    parts = event.data.decode().split(":")
    if len(parts) > index and parts[index].isdigit():
        return int(parts[index])
    return 0


async def ask(event, state: str, section: str, prompt: str, **data) -> None:
    # Start an input step and ask for the value. The section is stored with the state so
    # the rights can be checked again when the value finally arrives.
    if getattr(event, "is_private", True) is False:
        # Typed answers are only read in private chats, so a flow opened from a group
        # button would never be finishable.
        await event.answer("Open the panel in a private chat to edit this", alert=True)
        return
    states.set_for(event, state, section, **data)
    if hasattr(event, "answer"):
        try:
            await event.answer()
        except Exception as error:
            logger.debug("Callback answer failed: %s", error)
    delivered = await respond(
        event,
        f"{prompt}\n\n<i>Send the value as a message. The prompt expires in "
        f"{states.TTL_SECONDS // 60} minutes.</i>",
        buttons=[[Button.inline("❌ Cancel", "a:home")]],
    )
    if not delivered:
        states.clear_for(event)
