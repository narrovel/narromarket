# Bot copy. Welcome and help messages are editable from the admin panel.

import re
from html import escape as _escape

from config import RECEIPT_TTL_HOURS
from db import settings
from utils import dates

TELEGRAM_LIMIT = 4096

# A message that carries a file is a caption, and captions have a much smaller limit.
CAPTION_LIMIT = 1024


def escape(value) -> str:
    # Make text safe for parse_mode=html.
    #
    # Applied to anything a customer controls (names, usernames), to product names
    # typed by staff and to free form values like access details. Descriptions are
    # left alone: their HTML tags are intentional.
    return _escape(str(value or ""), quote=False)


# An ampersand that does not start an HTML entity. Telethon's parser feeds the text to
# HTMLParser and never closes it, so a bare "&" swallows everything after it: "R&D" ends
# up as an empty message and raises, and a trailing "?a=1&b=2" silently loses the tail.
_BARE_AMP = re.compile(r"&(?!#?\w+;)")


def html_safe(text: str) -> str:
    return _BARE_AMP.sub("&amp;", text or "")


def _utf16_len(text: str) -> int:
    # Telegram counts UTF-16 code units, so every emoji costs two.
    return len(text.encode("utf-16-le")) // 2


def clamp(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    # Telegram drops a message that is over the limit, so cut it instead of losing it.
    if not text:
        return text
    text = html_safe(text)
    if _utf16_len(text) <= limit:
        return text

    budget = max(1, limit - 24)
    used = 0
    end = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            break
        end = index + 1
    cut = text[:end]

    # Never end inside a tag or an entity: both would eat the rest of the message.
    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    if cut.rfind("&") > cut.rfind(";"):
        cut = cut[: cut.rfind("&")]
    return cut.rstrip() + "\n\n[...]"


def product_title(product: dict) -> str:
    return f"{escape(product.get('emoji') or '📦')} {escape(product.get('name') or '')}"


NO_USERNAME = (
    "⚠️ <b>A username is required</b>\n\n"
    "Set a username in Telegram settings, then send /start again."
)

BLOCKED = "🚫 Access to this bot is closed. Contact the manager if this looks wrong."

CATALOG_EMPTY = "📭 The catalog is empty for now. Come back a bit later."

NO_ORDERS = "📋 <b>My orders</b>\n\nNo orders yet. Take a look at the catalog 🛒"

NO_SUBSCRIPTIONS = (
    "📦 <b>My subscriptions</b>\n\nNothing active yet. Take a look at the catalog 🛒"
)


def welcome() -> str:
    return settings.get("welcome_text").replace("{bot_name}", settings.get("bot_name"))


def help_text() -> str:
    text = settings.get("help_text")
    if settings.get("manager_username"):
        text += f"\n\n📩 Contact: {settings.manager_mention()}"
    return text


def terms_text() -> str:
    text = settings.get("terms_text")
    if settings.get("manager_username"):
        text += f"\n\nQuestions before paying: {settings.manager_mention()}"
    return text


def support_text() -> str:
    text = settings.get("support_text")
    if settings.get("manager_username"):
        text += f"\n\n📩 Contact: {settings.manager_mention()}"
    else:
        text += "\n\nThe support contact has not been configured yet. Please try again later."
    return text


def stars_terms_prompt(product: dict) -> str:
    return (
        "⭐ <b>Confirm your purchase</b>\n\n"
        f"📦 Product: {product_title(product)}\n"
        f"💰 Price: <b>{int(product.get('price_stars') or 0)}⭐</b>\n"
        f"📅 Period: <b>{int(product.get('duration_days') or 0)} days</b>\n\n"
        f"{terms_text()}\n\n"
        "Continue only if the product, amount and period above are correct."
    )


def product_card(product: dict, price_line: str) -> str:
    parts = [f"{escape(product['emoji'])} <b>{escape(product['name'])}</b>", ""]
    if product.get("description"):
        parts.append(clamp(product["description"].strip(), 2000))
        parts.append("")
    parts.append(f"💰 <b>Price:</b> {price_line}")
    parts.append(f"📅 <b>Period:</b> {product['duration_days']} days")
    if product.get("instruction"):
        parts.append("")
        parts.append(clamp(product["instruction"].strip(), 1200))
    return "\n".join(parts)


def transfer_instructions(product: dict, amount_rub: int, method: dict, order_id: int) -> str:
    from db import requisites

    kind_title = requisites.KIND_TITLES.get(method["kind"], method["kind"])
    label = "Phone" if method["kind"] == "sbp" else "Card"
    lines = [
        f"{kind_title} <b>Bank transfer</b>",
        "",
        f"📦 <b>Product:</b> {product_title(product)}",
        f"💰 <b>Amount:</b> {amount_rub}₽",
        f"📋 <b>Order:</b> #{order_id}",
        "",
        "<b>Payment details</b>",
        f"🏦 Bank: {escape(method.get('bank') or '-')}",
        f"{'📱' if method['kind'] == 'sbp' else '💳'} {label}: "
        f"<code>{escape(method['details'])}</code>",
        f"👤 Recipient: {escape(method.get('holder') or '-')}",
        "",
        f"1. Transfer exactly {amount_rub}₽",
        "2. Save the receipt or a screenshot",
        "3. Send it here in a single message",
        "",
        "Use these details only for this order. Bank transfer is offered only where "
        "the operator has confirmed it is permitted for the transaction.",
        "",
        f"⏳ Waiting for the receipt, the order stays open for {RECEIPT_TTL_HOURS} hours.",
    ]
    return "\n".join(lines)


def payment_done(order_id: int, product_name: str, expires_at, renewed: bool) -> str:
    if renewed:
        return (
            "🔄 <b>Subscription renewed</b>\n\n"
            f"📋 Order: #{order_id}\n"
            f"📦 Product: {product_name}\n"
            f"📅 Valid until: <b>{dates.fmt_date(expires_at)}</b>\n\n"
            "Nothing was lost: the new period was added to the previous expiry date."
        )
    return (
        "✅ <b>Payment received</b>\n\n"
        f"📋 Order: #{order_id}\n"
        f"📦 Product: {product_name}\n"
        f"📅 Valid until: <b>{dates.fmt_date(expires_at)}</b>\n\n"
        "Access details usually arrive within 24 hours."
    )


def credentials_message(order_id: int, credentials: str, instruction: str) -> str:
    parts = [
        f"🔑 <b>Access details for order #{order_id}</b>",
        "",
        f"<code>{clamp(escape(credentials), 2500)}</code>",
    ]
    if instruction:
        parts += ["", clamp(instruction.strip(), 1200)]
    parts += ["", "Check the access and press 'Confirm' if everything works."]
    return "\n".join(parts)


def subscription_line(subscription: dict) -> str:
    left = dates.days_left(subscription["expires_at"])
    if left is None:
        status = ""
    elif left < 0:
        status = "⌛ expired"
    elif left == 0:
        status = "⚠️ expires today"
    elif left == 1:
        status = "⚠️ expires tomorrow"
    elif left <= 3:
        status = f"⏰ {left} days left"
    else:
        status = "✅ active"
    personal = " 🎁" if subscription.get("is_personal") else ""
    name = escape(subscription["product_name"])
    return (
        f"{escape(subscription['emoji'])} <b>{name}</b>{personal}\n"
        f"   📅 until {dates.fmt_date(subscription['expires_at'])} | {status}"
    )


def order_line(order: dict) -> str:
    from db import orders as orders_db

    amount = (
        f"{order['amount_stars']}⭐"
        if order["payment_method"] == "stars"
        else f"{int(order['amount_rub'])}₽"
    )
    return (
        f"{escape(order['emoji'])} <b>{escape(order['product_name'])}</b>\n"
        f"   #{order['id']} | {dates.fmt_date(order['created_at'])} | {amount}\n"
        f"   {orders_db.status_label(order['status'])}"
    )


def expiring_soon(subscription: dict, days: int) -> str:
    if days <= 0:
        when = "today"
    elif days == 1:
        when = "tomorrow"
    else:
        when = f"in {days} days"
    return (
        f"⏰ <b>Your subscription ends {when}</b>\n\n"
        f"{escape(subscription['emoji'])} <b>{escape(subscription['product_name'])}</b>\n"
        f"📅 Until {dates.fmt_date(subscription['expires_at'])}\n\n"
        "You can renew right now: paid days never burn, the new period is added "
        "to the current expiry date."
    )


def expired(subscription: dict) -> str:
    return (
        "⌛ <b>Subscription ended</b>\n\n"
        f"{escape(subscription['emoji'])} <b>{escape(subscription['product_name'])}</b>\n\n"
        "Want to renew?"
    )
