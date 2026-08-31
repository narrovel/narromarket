# The /start command, reply keyboard and menu navigation.

import logging
import re

from telethon import TelegramClient, events

from db import journal
from handlers import account, catalog, common
from utils import keyboards, states, texts

logger = logging.getLogger(__name__)


def register(client: TelegramClient) -> None:
    @client.on(events.NewMessage(pattern=r"^/start$"))
    async def start_handler(event):
        # Same as /admin: leaving to the main menu ends whatever flow was open, so the
        # input handler does not answer with a stale "Input cancelled" afterwards.
        states.clear_for(event)
        user = await common.guard(event)
        if not user:
            return

        await journal.event("start", user["telegram_id"])
        buttons = keyboards.reply_menu(await common.has_personal(user))
        await common.reply(event, texts.welcome(), buttons=buttons)
        await common.show_main_menu(event, user, text="👇 Pick a section:")

    # Payment terms and support must remain reachable even when an account is blocked
    # or no longer has a username. Those are exactly the customers who may still need
    # help with a charge that already happened.
    @client.on(events.NewMessage(pattern=r"^/terms(?:@\w+)?$"))
    async def terms_handler(event):
        if event.is_private:
            states.clear_for(event)
            await event.respond(
                texts.terms_text(), buttons=keyboards.terms_menu(), parse_mode="html"
            )

    @client.on(events.NewMessage(pattern=r"^/(?:support|paysupport)(?:@\w+)?$"))
    async def support_handler(event):
        if event.is_private:
            states.clear_for(event)
            await event.respond(
                texts.support_text(), buttons=keyboards.support_menu(), parse_mode="html"
            )

    @client.on(events.NewMessage(pattern=rf"^{re.escape(keyboards.BTN_CATALOG)}$"))
    async def catalog_button(event):
        user = await common.guard(event)
        if user:
            await catalog.show_catalog(event, user, page=0, personal=False)

    @client.on(events.NewMessage(pattern=rf"^{re.escape(keyboards.BTN_PERSONAL)}$"))
    async def personal_button(event):
        user = await common.guard(event)
        if user:
            await catalog.show_catalog(event, user, page=0, personal=True)

    @client.on(events.NewMessage(pattern=rf"^{re.escape(keyboards.BTN_SUBS)}$"))
    async def subs_button(event):
        user = await common.guard(event)
        if user:
            await account.show_subscriptions(event, user)

    @client.on(events.NewMessage(pattern=rf"^{re.escape(keyboards.BTN_ORDERS)}$"))
    async def orders_button(event):
        user = await common.guard(event)
        if user:
            await account.show_orders(event, user)

    @client.on(events.NewMessage(pattern=rf"^{re.escape(keyboards.BTN_HELP)}$"))
    async def help_button(event):
        user = await common.guard(event)
        if user:
            await common.reply(event, texts.help_text(), buttons=keyboards.help_menu())

    @client.on(events.CallbackQuery(pattern=rb"^menu:"))
    async def menu_router(event):
        user = await common.guard(event)
        if not user:
            return
        section = common.callback_arg(event)

        if section == "main":
            await common.show_main_menu(event, user, edit=True)
        elif section == "catalog":
            await catalog.show_catalog(event, user, page=0, personal=False, edit=True)
        elif section == "personal":
            await catalog.show_catalog(event, user, page=0, personal=True, edit=True)
        elif section == "subs":
            await account.show_subscriptions(event, user, edit=True)
        elif section == "orders":
            await account.show_orders(event, user, edit=True)
        elif section == "help":
            await common.edit_or_reply(event, texts.help_text(), keyboards.help_menu())
        elif section == "terms":
            await common.edit_or_reply(event, texts.terms_text(), keyboards.terms_menu())
        elif section == "support":
            await common.edit_or_reply(event, texts.support_text(), keyboards.support_menu())

        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^noop$"))
    async def noop_handler(event):
        await event.answer()
