# Catalog management: products, prices, periods, descriptions.

import logging
import re

from telethon import Button, TelegramClient, events

from db import journal
from db import products as products_db
from db import users as users_db
from handlers.admin import base, cards
from handlers.admin.input import on_state
from services import billing
from utils import states, texts

logger = logging.getLogger(__name__)

FIELD_PROMPTS = {
    "name": "Product name",
    "emoji": "Product emoji",
    "price_stars": "Price in stars (0 disables paying with stars)",
    "price_rub": "Price in rubles (0 disables bank transfer)",
    "duration_days": "Subscription length in days",
    "sort_order": "Position in the catalog, lower goes first",
    "short_description": "Short line used on buttons and invoices",
    "description": "Full description, HTML tags allowed",
    "instruction": "Instruction sent together with the access details",
    "image": "Image file name from the images folder, '-' to remove",
}

# Rubles are whole numbers everywhere. Stored as a float, a price of 299.99 was sold
# for 299 and sums drifted.
INT_FIELDS = ("price_stars", "price_rub", "duration_days", "sort_order")
REQUIRED_TEXT_FIELDS = ("name", "emoji")

# Free text fields need a ceiling too: creating a product caps the name at 64, and
# without the same cap here an edit could grow it past what a message may hold, which
# broke the catalog list and the payment instructions after the order was created.
TEXT_LIMITS = {
    "name": 64,
    "emoji": 8,
    "slug": 60,
    "image": 120,
    "short_description": 255,
    "description": 3000,
    "instruction": 2000,
}

# Accepted ranges keep invoice amounts and expiry dates within operational bounds.
NUMBER_LIMITS = {
    "price_stars": (0, 1_000_000),
    "price_rub": (0, 1_000_000),
    "duration_days": (1, 3650),
    "sort_order": (0, 10_000),
}

# Cyrillic product names still have to produce readable slugs.
TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ы": "y",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ъ": "",
    "ь": "",
}


def slugify(name: str) -> str:
    text = "".join(TRANSLIT.get(char, char) for char in name.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "product"


async def unique_slug(name: str, owner_user_id: int) -> str:
    base_slug = slugify(name)[:40]
    slug = base_slug
    index = 2
    while await products_db.slug_taken(slug, owner_user_id):
        slug = f"{base_slug}-{index}"
        index += 1
    return slug


async def product_list_view(event, owner_user_id: int = products_db.PUBLIC, page: int = 0):
    if owner_user_id:
        owner = await users_db.get_by_id(owner_user_id)
        items = await products_db.list_personal(owner_user_id, active_only=False)
        header = f"🎁 <b>Offers for {users_db.display_name(owner)}</b>"
        add_button = Button.inline("➕ New offer", f"a:onew:{owner_user_id}")
        extra = [Button.inline("📋 Copy from catalog", f"a:oclone:{owner_user_id}")]
        back = [Button.inline("◀️ Clients", "a:offers")]
        page_prefix = f"a:uoffers:{owner_user_id}"
    else:
        items = await products_db.list_public(active_only=False)
        header = "🛒 <b>Catalog</b>"
        add_button = Button.inline("➕ New product", "a:cnew")
        extra = []
        back = base.home_row()
        page_prefix = "a:cat"

    def mark(item):
        return "" if item["is_active"] else "⏸ "

    lines = [f"{header} - {len(items)}", ""]
    for item in base.page_slice(items, page):
        lines.append(
            f"{mark(item)}{texts.escape(item['emoji'])} {texts.escape(item['name'])} "
            f"- {billing.price_label(item, True)}"
        )
    if not items:
        lines.append("Nothing here yet.")

    rows = base.paged_rows(
        items,
        page,
        page_prefix,
        lambda item: f"{mark(item)}{item['emoji']} {item['name'][:24]}",
        lambda item: f"a:p:{item['id']}",
    )
    rows.append([add_button])
    if extra:
        rows.append(extra)
    rows.append(back)
    await base.show(event, "\n".join(lines), rows)


async def show_product_card(event, product: dict):
    owner = (
        await users_db.get_by_id(product["owner_user_id"]) if product["owner_user_id"] else None
    )
    await base.show(event, cards.product_card(product, owner), cards.product_buttons(product))


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^a:cat(:\d+)?$"))
    async def catalog_list(event):
        if not await base.actor(event, "catalog"):
            return
        await product_list_view(event, page=base.page_from(event, 2))
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:p:(\d+)$"))
    async def product_card(event):
        if not await base.actor(event, "catalog"):
            return
        product = await products_db.get(int(event.data.decode().split(":")[2]))
        if not product:
            await event.answer("Product not found", alert=True)
            return
        await show_product_card(event, product)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:pe:(\d+):([a-z_]+)$"))
    async def product_edit(event):
        user = await base.actor(event, "catalog")
        if not user:
            return
        _, _, product_id, field = event.data.decode().split(":")
        if field not in FIELD_PROMPTS:
            await event.answer("This field is not editable", alert=True)
            return
        product = await products_db.get(int(product_id))
        if not product:
            await event.answer("Product not found", alert=True)
            return
        current = product.get(field)
        await base.ask(
            event,
            "product_field",
            "catalog",
            f"✏️ {FIELD_PROMPTS[field]}\n\n"
            "Current: <code>"
            f"{texts.escape(current) if current not in (None, '') else 'empty'}</code>",
            product_id=product["id"],
            field=field,
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:ptoggle:(\d+):(0|1)$"))
    async def product_toggle(event):
        user = await base.actor(event, "catalog")
        if not user:
            return
        _, _, product_id, desired_raw = event.data.decode().split(":")
        product = await products_db.get(int(product_id))
        if not product:
            await event.answer("Product not found", alert=True)
            return
        await products_db.update(product["id"], is_active=int(desired_raw))
        await journal.action(user["telegram_id"], "product_toggle", f"product:{product['id']}")
        await event.answer("Done")
        await show_product_card(event, await products_db.get(product["id"]))

    @client.on(events.CallbackQuery(pattern=rb"^a:ptoggle:(\d+)$"))
    async def stale_product_toggle(event):
        if await base.actor(event, "catalog"):
            await event.answer("This button is outdated. Reopen the product.", alert=True)

    @client.on(events.CallbackQuery(pattern=rb"^a:pdel:(\d+)$"))
    async def product_delete_confirm(event):
        if not await base.actor(event, "catalog"):
            return
        product_id = int(event.data.decode().split(":")[2])
        product = await products_db.get(product_id)
        if not product:
            await event.answer("Product not found", alert=True)
            return
        await base.show(
            event,
            f"🗑 Delete <b>{texts.escape(product['emoji'])} "
            f"{texts.escape(product['name'])}</b>?\n\n"
            "Existing subscriptions stay, but they can no longer be renewed from the catalog.",
            [
                [Button.inline("Yes, delete", f"a:pdelyes:{product_id}")],
                [Button.inline("Cancel", f"a:p:{product_id}")],
            ],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:pdelyes:(\d+)$"))
    async def product_delete(event):
        user = await base.actor(event, "catalog")
        if not user:
            return
        product_id = int(event.data.decode().split(":")[2])
        product = await products_db.get(product_id)
        if not product:
            await event.answer("Product not found", alert=True)
            return
        owner_id = product["owner_user_id"]
        await products_db.delete(product_id)
        await journal.action(user["telegram_id"], "product_delete", f"product:{product_id}")
        await event.answer("Deleted")
        await product_list_view(event, owner_id)

    @client.on(events.CallbackQuery(pattern=rb"^a:cnew$"))
    async def product_new(event):
        user = await base.actor(event, "catalog")
        if not user:
            return
        await base.ask(
            event,
            "product_new",
            "catalog",
            "➕ Name of the new product",
            owner_user_id=products_db.PUBLIC,
        )


@on_state("product_new")
async def create_product(event, user, data, text):
    if text == "-":
        await base.respond(event, "Product name cannot be empty.")
        return
    states.clear_for(event)
    owner_user_id = data.get("owner_user_id", products_db.PUBLIC)
    product_id = await products_db.create(
        slug=await unique_slug(text, owner_user_id),
        owner_user_id=owner_user_id,
        name=text[:64],
        emoji="📦",
        is_active=0,
    )
    await journal.action(user["telegram_id"], "product_create", f"product:{product_id}")
    product = await products_db.get(product_id)
    owner = await users_db.get_by_id(owner_user_id) if owner_user_id else None
    await base.respond(
        event,
        "✅ Product created and disabled for now. Set the price and description, then enable it.\n\n"
        + cards.product_card(product, owner),
        buttons=cards.product_buttons(product),
        parse_mode="html",
    )


@on_state("product_field")
async def save_product_field(event, user, data, text):
    field = data["field"]
    product = await products_db.get(data["product_id"])
    if not product:
        states.clear_for(event)
        await base.respond(event, "Product not found.")
        return

    if field in INT_FIELDS:
        try:
            value = int(round(float(text.replace(",", "."))))
        except ValueError:
            await base.respond(event, "A whole number is expected. Try again or type 'cancel'.")
            return
    else:
        if field in REQUIRED_TEXT_FIELDS and text == "-":
            await base.respond(event, f"{FIELD_PROMPTS[field]} cannot be empty.")
            return
        value = "" if text == "-" else text
        limit = TEXT_LIMITS.get(field)
        if limit and len(value) > limit:
            await base.respond(event, f"Too long: keep {field} under {limit} characters.")
            return

    if field in NUMBER_LIMITS:
        low, high = NUMBER_LIMITS[field]
        if not low <= value <= high:
            await base.respond(event, f"Enter a number between {low} and {high}.")
            return

    states.clear_for(event)
    await products_db.update(product["id"], **{field: value})
    await journal.action(
        user["telegram_id"], "product_edit", f"product:{product['id']}", f"{field}={value}"
    )
    product = await products_db.get(product["id"])
    owner = (
        await users_db.get_by_id(product["owner_user_id"]) if product["owner_user_id"] else None
    )
    await base.respond(
        event,
        "✅ Saved.\n\n" + cards.product_card(product, owner),
        buttons=cards.product_buttons(product),
        parse_mode="html",
    )
