# Personal offers: a private product or a private price for one client.

import logging

from telethon import Button, TelegramClient, events

from db import journal
from db import products as products_db
from db import users as users_db
from handlers.admin import base, cards
from handlers.admin.catalog import product_list_view
from handlers.admin.input import on_state
from services import billing
from utils import states

logger = logging.getLogger(__name__)

CLONED_FIELDS = (
    "name",
    "emoji",
    "price_stars",
    "price_rub",
    "duration_days",
    "short_description",
    "description",
    "instruction",
    "image",
    "sort_order",
)


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^a:offers(:\d+)?$"))
    async def offers_home(event):
        if not await base.actor(event, "offers"):
            return
        page = base.page_from(event, 2)
        owners = await products_db.owners()

        lines = [f"🎁 <b>Personal offers</b> - {len(owners)} clients", ""]
        for owner in base.page_slice(owners, page):
            lines.append(f"{users_db.display_name(owner)} - {owner['offers']}")
        if not owners:
            lines.append("Nobody has personal offers yet.")

        rows = base.paged_rows(
            owners,
            page,
            "a:offers",
            lambda owner: f"{users_db.display_name(owner)[:26]} ({owner['offers']})",
            lambda owner: f"a:uoffers:{owner['id']}",
        )
        rows.append([Button.inline("🔍 Find a client", "a:offind")])
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:offind$"))
    async def offers_find(event):
        user = await base.actor(event, "offers")
        if not user:
            return
        await base.ask(
            event,
            "offer_search",
            "offers",
            "🔍 Who is the offer for? Send a @username, a name or a Telegram ID.",
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:uoffers:(\d+)(:\d+)?$"))
    async def user_offers(event):
        if not await base.actor(event, "offers"):
            return
        owner_id = int(event.data.decode().split(":")[2])
        if not await users_db.get_by_id(owner_id):
            await event.answer("Client not found", alert=True)
            return
        await product_list_view(event, owner_id, page=base.page_from(event, 3))
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:onew:(\d+)$"))
    async def offer_new(event):
        user = await base.actor(event, "offers")
        if not user:
            return
        owner_id = int(event.data.decode().split(":")[2])
        owner = await users_db.get_by_id(owner_id)
        if not owner:
            await event.answer("Client not found", alert=True)
            return
        await base.ask(
            event,
            "product_new",
            "offers",
            f"➕ Offer name for {users_db.display_name(owner)}",
            owner_user_id=owner_id,
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:oclone:(\d+)(:\d+)?$"))
    async def offer_clone_list(event):
        if not await base.actor(event, "offers"):
            return
        owner_id = int(event.data.decode().split(":")[2])
        items = await products_db.list_public(active_only=False)
        if not items:
            await event.answer("The catalog is empty", alert=True)
            return
        rows = base.paged_rows(
            items,
            base.page_from(event, 3),
            f"a:oclone:{owner_id}",
            lambda item: (
                f"{item['emoji']} {item['name'][:24]} ({billing.price_label(item, True)})"
            ),
            lambda item: f"a:ocl:{owner_id}:{item['id']}",
        )
        rows.append([Button.inline("◀️ Back", f"a:uoffers:{owner_id}")])
        await base.show(
            event,
            "📋 Pick a product to copy for this client. The price can be changed afterwards.",
            rows,
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:ocl:(\d+):(\d+)$"))
    async def offer_clone(event):
        user = await base.actor(event, "offers")
        if not user:
            return
        _, _, owner_id, product_id = event.data.decode().split(":")
        owner_id, product_id = int(owner_id), int(product_id)
        source = await products_db.get(product_id)
        owner = await users_db.get_by_id(owner_id)
        if not source or not owner:
            await event.answer("Not found", alert=True)
            return

        existing = await products_db.get_by_slug(source["slug"], owner_id)
        if existing:
            await event.answer("This client already has that offer", alert=True)
            await base.show(
                event, cards.product_card(existing, owner), cards.product_buttons(existing)
            )
            return

        fields = {key: source[key] for key in CLONED_FIELDS}
        new_id = await products_db.create(
            slug=source["slug"], owner_user_id=owner_id, is_active=1, **fields
        )
        await journal.action(
            user["telegram_id"], "offer_create", f"product:{new_id}", f"user:{owner_id}"
        )
        product = await products_db.get(new_id)
        await event.answer("Copied")
        await base.show(
            event, cards.product_card(product, owner), cards.product_buttons(product)
        )


@on_state("offer_search")
async def offer_search(event, user, data, text):
    found = await users_db.search(text)
    if not found:
        await base.respond(event, "Nobody found. Send another @username or ID.")
        return

    states.clear_for(event)
    if len(found) == 1:
        owner = found[0]
        await base.respond(
            event,
            f"🎁 Offers for {users_db.display_name(owner)}",
            buttons=[[Button.inline("Open", f"a:uoffers:{owner['id']}")]],
            parse_mode="html",
        )
        return

    rows = [
        [Button.inline(users_db.display_name(item)[:30], f"a:uoffers:{item['id']}")]
        for item in found[:10]
    ]
    await base.respond(event, "Which one did you mean?", buttons=rows)
