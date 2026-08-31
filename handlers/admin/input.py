# Single entry point for text input in admin step by step flows.

import logging
from typing import Awaitable, Callable

from telethon import TelegramClient, events

from handlers import common
from services import access
from utils import keyboards, states

logger = logging.getLogger(__name__)

STATE_HANDLERS: dict[str, Callable[..., Awaitable[None]]] = {}

CANCEL_WORDS = {"cancel", "stop", "/cancel"}
MENU_WORDS = {
    keyboards.BTN_CATALOG,
    keyboards.BTN_PERSONAL,
    keyboards.BTN_SUBS,
    keyboards.BTN_ORDERS,
    keyboards.BTN_HELP,
}


def on_state(name: str):
    def decorator(handler):
        STATE_HANDLERS[name] = handler
        return handler

    return decorator


def register(client: TelegramClient) -> None:
    # Private chats only, and only messages that came in. Registered without a filter
    # this catch all fired in every chat the bot can see, so a flow opened in a private
    # chat swallowed the same admin's next message in a group.
    @client.on(events.NewMessage(incoming=True, func=lambda event: event.is_private))
    async def text_input(event):
        text = (event.message.message or "").strip()
        if not text:
            return

        state = states.get_for(event)
        if not state:
            return

        handler = STATE_HANDLERS.get(state["name"])
        if handler is None:
            states.clear_for(event)
            return

        user = await access.actor(event.sender_id)
        if user:
            event._narromarket_user_id = user["id"]
        if not user or int(state.get("actor_user_id") or 0) != user["id"]:
            states.clear_for(event)
            if user:
                await common.reply(event, "❌ This input prompt is no longer active.")
            return

        if text.startswith("/") or text.lower() in CANCEL_WORDS or text in MENU_WORDS:
            states.clear_for(event)
            await common.reply(event, "Input cancelled.")
            return

        # The section the flow was opened from is checked again here. Checking only for
        # staff let a demoted or blocked admin finish a flow they no longer may run,
        # and the value they typed went straight into the shop settings.
        if not access.can(user, state.get("section") or "settings"):
            states.clear_for(event)
            await common.reply(event, "❌ You no longer have rights for this. Input cancelled.")
            return

        try:
            await handler(event, user, state["data"], text)
        except Exception as error:
            logger.exception("Flow %s failed: %s", state["name"], error)
            states.clear_for(event)
            await common.reply(event, "❌ Did not work out. The action was cancelled.")
