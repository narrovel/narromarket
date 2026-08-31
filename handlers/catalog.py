# Catalog listing and product cards.

import logging
from typing import Optional

from telethon import TelegramClient, events

from db import journal
from db import orders as orders_db
from db import products as products_db
from db import subscriptions as subs_db
from handlers import common
from services import billing
from utils import dates, keyboards, texts

logger = logging.getLogger(__name__)


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^cat:"))
    async def catalog_page(event):
        user = await common.guard(event)
        if not user:
            return
        await show_catalog(event, user, common.callback_int(event), personal=False, edit=True)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^pcat:"))
    async def personal_page(event):
        user = await common.guard(event)
        if not user:
            return
        await show_catalog(event, user, common.callback_int(event), personal=True, edit=True)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^prod:"))
    async def product_view(event):
        user = await common.guard(event)
        if not user:
            return
        await show_product(event, user, common.callback_int(event))
        await event.answer()


async def show_catalog(
    event, user: dict, page: int = 0, personal: bool = False, edit: bool = False
):
    if personal:
        items = await products_db.list_personal(user["id"])
        header = "🎁 <b>Your personal offers</b>"
    else:
        items = await products_db.public_catalog_for(user["id"])
        header = "🛒 <b>Catalog</b>"

    await journal.event("view_personal" if personal else "view_catalog", user["telegram_id"])

    if not items:
        if not personal and await products_db.list_personal(user["id"]):
            await common.edit_or_reply(
                event,
                "🎁 There are personal offers prepared for you.",
                [[keyboards.Button.inline("🎁 Open", "menu:personal")], *keyboards.back_home()],
            )
            return
        await common.edit_or_reply(event, texts.CATALOG_EMPTY, keyboards.back_home())
        return

    # Show the transfer price in the list only if this customer can actually use it.
    can_transfer = bool(await billing.transfer_method(user, {"price_rub": 1}))
    buttons = keyboards.catalog(
        items,
        page,
        personal,
        lambda item: billing.price_label(item, with_transfer=can_transfer),
    )
    text = f"{header}\n\nPick a plan."
    if edit:
        await common.edit_or_reply(event, text, buttons)
    else:
        await common.reply(event, text, buttons)


async def resolve_visible_product(user: dict, product_id: int) -> Optional[dict]:
    # Product this user is allowed to see, with personal offers taking priority.
    product = await products_db.get(product_id)
    if not product or not product["is_active"]:
        return None
    if product["owner_user_id"] not in (products_db.PUBLIC, user["id"]):
        return None
    if product["owner_user_id"] == products_db.PUBLIC:
        personal = await products_db.get_by_slug(product["slug"], user["id"])
        if personal and personal["is_active"]:
            return personal
    return product


async def show_product(event, user: dict, product_id: int):
    product = await resolve_visible_product(user, product_id)
    if not product:
        await common.edit_or_reply(event, "Product not found.", keyboards.back_home())
        return

    await journal.event("view_product", user["telegram_id"], product["slug"])

    personal = product["owner_user_id"] != products_db.PUBLIC
    method = await billing.transfer_method(user, product)
    transfer_rub = billing.rub_price(product) if method else 0
    price_line = billing.price_label(product, with_transfer=bool(method))

    text = texts.product_card(product, price_line)

    subscription = await subs_db.active_for_slug(user["id"], product["slug"])
    if subscription:
        new_expires = billing.next_expiry(subscription["expires_at"], product["duration_days"])
        text += (
            f"\n\n🔄 <b>Renewal</b>\n"
            f"Active until {dates.fmt_date(subscription['expires_at'])}.\n"
            f"After payment it runs until {dates.fmt_date(new_expires)}."
        )

    open_order = await orders_db.open_for_product(user["id"], product["slug"])
    if open_order:
        text += (
            f"\n\n⚠️ Order #{open_order['id']} for this product is still open "
            f"({orders_db.status_label(open_order['status'])}). Please wait until it is closed."
        )
        buttons = [
            [keyboards.Button.inline(keyboards.BTN_ORDERS, "menu:orders")],
            *keyboards.back_home(),
        ]
    else:
        buttons = keyboards.product_card(
            product, billing.can_pay_stars(product), transfer_rub, personal
        )

    image = common.image_path(product)
    if image:
        # With a file attached the text becomes a caption, and a caption may only be a
        # quarter as long as a message. The old message is deleted only after the card
        # actually went out: deleting first left the customer with nothing at all when
        # the send failed.
        try:
            sent = await common.reply(
                event,
                texts.clamp(text, texts.CAPTION_LIMIT),
                file=image,
                buttons=buttons,
            )
            if not sent:
                return
        except Exception as error:
            logger.warning("Product card with an image was not sent: %s", error)
            await common.edit_or_reply(event, text, buttons)
            return
        try:
            await event.delete()
        except Exception as error:
            logger.debug("Could not delete the catalog message: %s", error)
        return

    await common.edit_or_reply(event, text, buttons)
