# Customer view: subscriptions, orders and order feedback.

import logging

from telethon import TelegramClient, events

from db import orders as orders_db
from db import subscriptions as subs_db
from db import users as users_db
from handlers import common
from handlers.admin import cards
from services import notify
from utils import dates, keyboards, texts

logger = logging.getLogger(__name__)


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^subdata:(\d+)$"))
    async def subscription_data(event):
        user = await common.guard(event)
        if not user:
            return
        async with users_db.lifecycle_lock(user["id"]):
            current_user = await users_db.get_by_id(user["id"])
            if not current_user or current_user["telegram_id"] != user["telegram_id"]:
                await event.answer("This account is no longer active", alert=True)
                return
            subscription = await subs_db.get(common.callback_int(event))
            if not subscription or subscription["user_id"] != user["id"]:
                await event.answer("Subscription not found", alert=True)
                return
            # The button that opens this stays tappable in the chat forever, so the
            # current state has to be checked again while account erasure is excluded.
            if (
                subscription["status"] != subs_db.ACTIVE
                or dates.parse(subscription["expires_at"]) <= dates.utcnow()
            ):
                await event.answer("This subscription is no longer active", alert=True)
                return
            if not subscription["credentials"]:
                await event.answer("Access details are not issued yet", alert=True)
                return
            text = (
                f"🔑 <b>Access details</b>\n\n"
                f"{texts.escape(subscription['emoji'])} "
                f"<b>{texts.escape(subscription['product_name'])}</b>\n"
                f"📅 Until {dates.fmt_date(subscription['expires_at'])}\n\n"
                f"<code>{texts.escape(subscription['credentials'])}</code>\n\n"
                "Do not share these details with anyone."
            )
            await common.edit_or_reply(
                event,
                text,
                [[keyboards.Button.inline("◀️ Back", "menu:subs")]],
                lifecycle_held=True,
            )
            await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^ok:(\d+)$"))
    async def confirm_order(event):
        user = await common.guard(event)
        if not user:
            return
        async with users_db.lifecycle_lock(user["id"]):
            current_user = await users_db.get_by_id(user["id"])
            if not current_user or current_user["telegram_id"] != user["telegram_id"]:
                await event.answer("This account is no longer active", alert=True)
                return
            order = await orders_db.get(common.callback_int(event))
            if not order or order["user_id"] != user["id"]:
                await event.answer("Order not found", alert=True)
                return
            if not order.get("paid_at") or not order.get("subscription_id"):
                await event.answer("This order has not been fulfilled", alert=True)
                return
            if not await orders_db.claim_status(
                order["id"],
                orders_db.COMPLETED,
                (orders_db.DELIVERED, orders_db.PROBLEM),
            ):
                await event.answer("This order is already closed", alert=True)
                return

            await common.edit_or_reply(
                event,
                "✅ Thanks, enjoy!\n\nYou can renew any time from 'My subscriptions'.",
                keyboards.after_payment(),
                lifecycle_held=True,
            )
            await event.answer()

        # Broadcast only after releasing the customer's lifecycle lock. A staff member
        # can also be a customer; holding one staff lock while acquiring every other one
        # lets two confirmations deadlock each other. The notice intentionally carries
        # no customer profile fields, so erasure after the release cannot leak stale PII.
        await notify.to_staff(event.client, f"✅ Order #{order['id']} confirmed")

    @client.on(events.CallbackQuery(pattern=rb"^problem:(\d+)$"))
    async def report_problem(event):
        user = await common.guard(event)
        if not user:
            return
        async with users_db.lifecycle_lock(user["id"]):
            current_user = await users_db.get_by_id(user["id"])
            if not current_user or current_user["telegram_id"] != user["telegram_id"]:
                await event.answer("This account is no longer active", alert=True)
                return
            order = await orders_db.get(common.callback_int(event))
            if not order or order["user_id"] != user["id"]:
                await event.answer("Order not found", alert=True)
                return
            if not order.get("paid_at") or not order.get("subscription_id"):
                await event.answer("This order has not been fulfilled", alert=True)
                return
            if not await orders_db.claim_status(
                order["id"],
                orders_db.PROBLEM,
                (
                    orders_db.DELIVERED,
                    orders_db.COMPLETED,
                ),
            ):
                await event.answer(
                    "This order is closed, please contact the manager", alert=True
                )
                return

        await common.edit_or_reply(
            event,
            "🆘 The manager has been notified and will get back to you shortly.",
            keyboards.help_menu(),
        )
        fresh = await orders_db.get(order["id"])
        await notify.to_staff(
            event.client,
            "🆘 <b>Customer reported a problem</b>\n\n" + cards.order_card(fresh),
            cards.order_buttons(fresh),
        )
        await event.answer()


async def show_subscriptions(event, user: dict, edit: bool = False) -> None:
    items = await subs_db.active_for_user(user["id"])
    if not items:
        await _send(event, texts.NO_SUBSCRIPTIONS, keyboards.back_home(), edit)
        return

    lines = ["📦 <b>Active subscriptions</b>", ""]
    lines += [texts.subscription_line(item) for item in items]
    lines += [
        "",
        "<i>Renewing early keeps your date: days are added to the current period.</i>",
    ]
    await _send(event, "\n".join(lines), keyboards.subscriptions_list(items), edit)


async def show_orders(event, user: dict, edit: bool = False) -> None:
    items = await orders_db.list_for_user(user["id"], limit=10)
    if not items:
        await _send(event, texts.NO_ORDERS, keyboards.back_home(), edit)
        return

    lines = ["📋 <b>My orders</b>", ""]
    lines += [texts.order_line(item) for item in items]
    await _send(event, "\n".join(lines), keyboards.orders_list(items), edit)


async def _send(event, text: str, buttons, edit: bool) -> None:
    if edit:
        await common.edit_or_reply(event, text, buttons)
    else:
        await common.reply(event, text, buttons)
