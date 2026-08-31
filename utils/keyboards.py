# Keyboards for the customer facing part of the bot.

from typing import Callable, Optional

from telethon import Button

from db import settings

BTN_CATALOG = "🛒 Catalog"
BTN_PERSONAL = "🎁 Personal"
BTN_SUBS = "📦 My subscriptions"
BTN_ORDERS = "📋 My orders"
BTN_HELP = "❓ Help"


def reply_menu(has_personal: bool) -> list[list[Button]]:
    rows = [
        [Button.text(BTN_CATALOG, resize=True), Button.text(BTN_SUBS, resize=True)],
        [Button.text(BTN_ORDERS, resize=True), Button.text(BTN_HELP, resize=True)],
    ]
    if has_personal:
        rows.insert(1, [Button.text(BTN_PERSONAL, resize=True)])
    return rows


def main_menu(has_personal: bool, is_staff: bool) -> list[list[Button]]:
    rows = [[Button.inline(BTN_CATALOG, "menu:catalog")]]
    if has_personal:
        rows.append([Button.inline("🎁 Personal offers", "menu:personal")])
    rows += [
        [Button.inline(BTN_SUBS, "menu:subs")],
        [Button.inline(BTN_ORDERS, "menu:orders")],
        [Button.inline(BTN_HELP, "menu:help")],
    ]
    if is_staff:
        rows.append([Button.inline("🛠 Admin panel", "a:home")])
    return rows


def _label(title: str, suffix: str = "") -> str:
    room = MAX_LABEL - (len(suffix) + 3 if suffix else 0)
    if len(title) > room:
        title = title[: max(1, room - 1)].rstrip() + "…"
    return f"{title} - {suffix}" if suffix else title


def back_home() -> list[list[Button]]:
    return [[Button.inline("🏠 Menu", "menu:main")]]


def paginate(
    items: list,
    page: int,
    per_page: int,
    page_prefix: str,
    label: Callable[[dict], str],
    data: Callable[[dict], str],
) -> tuple[list[list[Button]], int]:
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per_page : page * per_page + per_page]

    rows = [[Button.inline(label(item), data(item))] for item in chunk]
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️", f"{page_prefix}:{page - 1}"))
        nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(Button.inline("▶️", f"{page_prefix}:{page + 1}"))
        rows.append(nav)
    return rows, total_pages


# One row per item, so an oversized page makes Telegram reject the whole markup and the
# catalog button stops doing anything at all.
MAX_ROWS = 20
MAX_LABEL = 48


def catalog(items: list[dict], page: int, personal: bool, price_label: Callable[[dict], str]):
    per_page = min(MAX_ROWS, max(2, settings.get_int("catalog_per_page", 6)))
    prefix = "pcat" if personal else "cat"
    rows, _ = paginate(
        items,
        page,
        per_page,
        prefix,
        lambda item: _label(f"{item['emoji']} {item['name']}", price_label(item)),
        lambda item: f"prod:{item['id']}",
    )
    manager = settings.manager_link()
    if manager and not personal:
        rows.append([Button.url("🔍 Looking for something else?", manager)])
    rows.append([Button.inline("🏠 Menu", "menu:main")])
    return rows


def product_card(product: dict, can_stars: bool, transfer_rub: int, personal: bool):
    rows = []
    if can_stars:
        rows.append(
            [Button.inline(f"⭐ Pay {product['price_stars']}⭐", f"pay_stars:{product['id']}")]
        )
    if transfer_rub:
        rows.append([Button.inline(f"💳 Transfer {transfer_rub}₽", f"pay_tr:{product['id']}")])
    if not rows:
        rows.append([Button.inline("💬 Contact the manager", "menu:help")])
    rows.append([Button.inline("◀️ Back", "menu:personal" if personal else "menu:catalog")])
    return rows


def stars_terms(product: dict, quote_token: str):
    amount = int(product.get("price_stars") or 0)
    return [
        [
            Button.inline(
                _label("✅ I agree and continue", f"{amount}⭐"),
                f"pay_stars_confirm:{quote_token}",
            )
        ],
        [Button.inline("❌ Cancel", f"prod:{product['id']}")],
    ]


def waiting_receipt(order_id: int):
    return [[Button.inline("❌ Cancel order", f"cancel_order:{order_id}")]]


def order_response(order_id: int):
    return [
        [Button.inline("✅ Confirm", f"ok:{order_id}")],
        [Button.inline("🆘 Something is wrong", f"problem:{order_id}")],
    ]


def after_payment():
    return [
        [Button.inline(BTN_SUBS, "menu:subs")],
        [Button.inline("🏠 Menu", "menu:main")],
    ]


def refund_retry(refund_id: int):
    return [[Button.inline("🔁 Retry refund", f"a:refundretry:{refund_id}")]]


def subscriptions_list(items: list[dict]):
    # Truncated the same way the text is, see account.show_subscriptions.
    rows = []
    for subscription in items[:MAX_ROWS]:
        row = []
        if subscription.get("credentials"):
            row.append(Button.inline("🔑 Access", f"subdata:{subscription['id']}"))
        if subscription.get("product_id"):
            row.append(Button.inline("🔄 Renew", f"prod:{subscription['product_id']}"))
        if row:
            rows.append(row)
    rows.append([Button.inline(BTN_CATALOG, "menu:catalog")])
    rows.append([Button.inline("🏠 Menu", "menu:main")])
    return rows


def orders_list(items: list[dict]):
    rows = []
    for order in items[:MAX_ROWS]:
        if order["status"] == "pending_receipt":
            rows.append(
                [Button.inline(f"❌ Cancel #{order['id']}", f"cancel_order:{order['id']}")]
            )
    rows.append([Button.inline(BTN_CATALOG, "menu:catalog")])
    rows.append([Button.inline("🏠 Menu", "menu:main")])
    return rows


def help_menu():
    rows = [
        [Button.inline("📄 Payment terms", "menu:terms")],
        [Button.inline("🛟 Payment support", "menu:support")],
    ]
    manager = settings.manager_link()
    if manager:
        rows.append([Button.url("📩 Message the manager", manager)])
    rows.append([Button.inline("🏠 Menu", "menu:main")])
    return rows


def terms_menu():
    return [
        [Button.inline("🛟 Payment support", "menu:support")],
        [Button.inline("🏠 Menu", "menu:main")],
    ]


def support_menu():
    rows = []
    manager = settings.manager_link()
    if manager:
        rows.append([Button.url("📩 Message the manager", manager)])
    rows += [
        [Button.inline("📄 Payment terms", "menu:terms")],
        [Button.inline("🏠 Menu", "menu:main")],
    ]
    return rows


def renew_button(product_id: Optional[int]):
    if not product_id:
        return None
    return [[Button.inline("🔄 Renew", f"prod:{product_id}")]]
