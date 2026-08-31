# Cards for orders, subscriptions, users and products shown to staff.

from telethon import Button

from db import orders as orders_db
from db import users as users_db
from utils import dates, texts


def order_card(order: dict) -> str:
    amount = []
    if order["amount_stars"]:
        amount.append(f"{order['amount_stars']}⭐")
    if order["amount_rub"]:
        amount.append(f"{int(order['amount_rub'])}₽")
    kind = "🔄 renewal" if order["is_renewal"] else "🆕 new subscription"
    personal = " 🎁" if order["is_personal"] else ""
    method = "⭐ Stars" if order["payment_method"] == "stars" else "💳 Transfer"

    lines = [
        f"📋 <b>Order #{order['id']}</b>{personal}",
        f"{orders_db.status_label(order['status'])} | {kind}",
        "",
        f"👤 {users_db.display_name(order)}",
        f"🆔 <code>{order['telegram_id']}</code>",
        f"📦 {texts.escape(order['emoji'])} {texts.escape(order['product_name'])}",
        f"💰 {' / '.join(amount) if amount else '-'} ({method})",
        f"🕐 {dates.fmt_datetime(order['created_at'])}",
    ]
    if order.get("subscription_id"):
        lines.append(f"🔗 Subscription #{order['subscription_id']}")
    if order.get("processed_by_telegram_id"):
        lines.append(f"🛠 Handled by <code>{order['processed_by_telegram_id']}</code>")
    return "\n".join(lines)


def order_buttons(order: dict) -> list:
    status = order["status"]
    paid = bool(order.get("paid_at"))
    stars_refundable = order.get("payment_method") != "stars" or (
        bool(order.get("payment_charge_id"))
        and int(order.get("payment_recipient_id") or order.get("telegram_id") or 0) > 0
    )
    rows = []
    if status == orders_db.PENDING_REVIEW and not paid:
        rows.append([Button.inline("✅ Confirm payment", f"a:confirm:{order['id']}")])
        rows.append([Button.inline("❌ Reject", f"a:reject:{order['id']}")])
    elif status == orders_db.PAID and paid:
        if order.get("subscription_id"):
            rows.append([Button.inline("📩 Send access", f"a:send:{order['id']}")])
        else:
            rows.append([Button.inline("🔁 Retry fulfilment", f"a:confirm:{order['id']}")])
        # No Cancel here: the money has arrived, so the only honest way out is a refund.
        if stars_refundable:
            rows.append([Button.inline("💸 Refund", f"a:refundask:{order['id']}")])
    elif status == orders_db.DELIVERED and paid:
        rows.append([Button.inline("✅ Complete", f"a:done:{order['id']}")])
        rows.append([Button.inline("📩 Update access", f"a:send:{order['id']}")])
        if stars_refundable:
            rows.append([Button.inline("💸 Refund", f"a:refundask:{order['id']}")])
    elif status == orders_db.PROBLEM:
        if not order.get("paid_at"):
            # Parked here by the stale-review sweep: the payment was never confirmed.
            rows.append([Button.inline("✅ Confirm payment", f"a:confirm:{order['id']}")])
            rows.append([Button.inline("❌ Reject", f"a:reject:{order['id']}")])
        elif not order.get("subscription_id"):
            rows.append([Button.inline("🔁 Retry fulfilment", f"a:confirm:{order['id']}")])
            if stars_refundable:
                rows.append([Button.inline("💸 Refund", f"a:refundask:{order['id']}")])
        else:
            rows.append([Button.inline("📩 Send access", f"a:send:{order['id']}")])
            buttons = [Button.inline("✅ Complete", f"a:done:{order['id']}")]
            if stars_refundable:
                buttons.insert(0, Button.inline("💸 Refund", f"a:refundask:{order['id']}"))
            rows.append(buttons)
    elif status == orders_db.COMPLETED and paid:
        if stars_refundable:
            rows.append([Button.inline("💸 Refund", f"a:refundask:{order['id']}")])
    elif status == orders_db.REFUND_PENDING:
        rows.append([Button.inline("💸 Open refund queue", "a:refunds")])
    rows.append(
        [
            Button.inline("👤 Profile", f"a:user:{order['user_id']}"),
            Button.inline("💬 Message", f"a:writeu:{order['user_id']}"),
        ]
    )
    return rows


def subscription_card(subscription: dict) -> str:
    left = dates.days_left(subscription["expires_at"])
    left_text = f"{left} days" if left is not None else "-"
    personal = " 🎁" if subscription["is_personal"] else ""
    lines = [
        f"📦 <b>Subscription #{subscription['id']}</b>{personal}",
        f"{texts.escape(subscription['emoji'])} {texts.escape(subscription['product_name'])}",
        "",
        f"👤 {users_db.display_name(subscription)}",
        f"🆔 <code>{subscription['telegram_id']}</code>",
        f"📅 Until {dates.fmt_date(subscription['expires_at'])} ({left_text} left)",
        f"🚦 Status: {subscription['status']}",
    ]
    if subscription.get("credentials"):
        lines += [
            "",
            "🔑 <b>Access details</b>",
            f"<code>{texts.escape(subscription['credentials'])}</code>",
        ]
    return "\n".join(lines)


def subscription_buttons(subscription: dict) -> list:
    return [
        [
            Button.inline("🎁 Add days", f"a:subdays:{subscription['id']}"),
            Button.inline("🔑 Access", f"a:subcreds:{subscription['id']}"),
        ],
        [
            Button.inline("💬 Message", f"a:writeu:{subscription['user_id']}"),
            Button.inline("⛔️ Close", f"a:subcancelask:{subscription['id']}"),
        ],
        [Button.inline("◀️ Subscriptions", "a:subs")],
    ]


def user_card(
    user: dict, subscriptions: list[dict], orders: list[dict], method: dict = None
) -> str:
    lines = [
        f"👤 <b>{users_db.display_name(user)}</b>",
        f"🆔 <code>{user['telegram_id']}</code>",
        f"🎚 Role: {users_db.ROLE_TITLES.get(user['role'], user['role'])}",
        f"🚦 {'🚫 blocked' if user['is_blocked'] else '✅ active'}",
        f"📅 Joined {dates.fmt_date(user['created_at'])}",
        f"💳 Payment details: {texts.escape(method['title']) if method else 'shop default'}",
    ]
    if user.get("note"):
        lines.append(f"📝 {texts.escape(user['note'])}")

    lines.append("")
    if subscriptions:
        lines.append(f"<b>Subscriptions ({len(subscriptions)})</b>")
        for item in subscriptions[:8]:
            lines.append(
                f"• #{item['id']} {texts.escape(item['emoji'])} {texts.escape(item['product_name'])} "
                f"until {dates.fmt_date(item['expires_at'])} ({item['status']})"
            )
    else:
        lines.append("No subscriptions")

    if orders:
        lines.append("")
        lines.append("<b>Recent orders</b>")
        for item in orders[:5]:
            lines.append(
                f"• #{item['id']} {texts.escape(item['product_name'])} - {orders_db.status_label(item['status'])}"
            )
    return "\n".join(lines)


def user_buttons(
    user: dict, can_manage: bool, can_requisites: bool = False, can_erase: bool = False
) -> list:
    # An erased row carries a negative placeholder id that matches no chat, so the
    # message button would be a callback nothing answers.
    first = [Button.inline("🎁 Offers", f"a:uoffers:{user['id']}")]
    if user["telegram_id"] > 0:
        first.insert(0, Button.inline("💬 Message", f"a:writeu:{user['id']}"))
    access_row = [Button.inline("➕ Grant access", f"a:grant:{user['id']}")]
    if can_requisites:
        access_row.insert(0, Button.inline("💳 Payment details", f"a:pm:{user['id']}"))
    rows = [first, access_row]
    if can_manage:
        rows.append([Button.inline("🎚 Role", f"a:roles:{user['id']}")])
        rows.append(
            [
                Button.inline(
                    "✅ Unblock" if user["is_blocked"] else "🚫 Block",
                    f"a:block:{user['id']}:{0 if user['is_blocked'] else 1}",
                )
            ]
        )
    if can_erase:
        rows.append([Button.inline("🧹 Erase personal data", f"a:erase:{user['id']}")])
    rows.append([Button.inline("◀️ Users", "a:users")])
    return rows


def product_card(product: dict, owner: dict = None) -> str:
    scope = "🎁 personal offer" if product["owner_user_id"] else "🌍 public catalog"
    lines = [
        f"{texts.escape(product['emoji'])} <b>{texts.escape(product['name'])}</b>",
        f"<code>{product['slug']}</code> | {scope}",
    ]
    if owner:
        lines.append(f"👤 Client: {users_db.display_name(owner)}")
    lines += [
        "",
        f"⭐ Stars price: {product['price_stars'] or 'not set'}",
        f"💳 Transfer price: {int(product['price_rub']) if product['price_rub'] else 'not set'}",
        f"📅 Period: {product['duration_days']} days",
        f"🚦 {'✅ enabled' if product['is_active'] else '⏸ disabled'}",
        f"🔢 Sort order: {product['sort_order']}",
        f"🖼 Image: {product['image'] or 'none'}",
    ]
    if product.get("short_description"):
        lines += ["", f"<i>{texts.escape(product['short_description'])}</i>"]
    if product.get("description"):
        lines += ["", texts.clamp(product["description"], 400)]
    return "\n".join(lines)


def product_buttons(product: dict) -> list:
    product_id = product["id"]
    return [
        [
            Button.inline("✏️ Name", f"a:pe:{product_id}:name"),
            Button.inline("😀 Emoji", f"a:pe:{product_id}:emoji"),
        ],
        [
            Button.inline("⭐ Stars price", f"a:pe:{product_id}:price_stars"),
            Button.inline("💳 Transfer price", f"a:pe:{product_id}:price_rub"),
        ],
        [
            Button.inline("📅 Period", f"a:pe:{product_id}:duration_days"),
            Button.inline("🔢 Sort order", f"a:pe:{product_id}:sort_order"),
        ],
        [
            Button.inline("📝 Description", f"a:pe:{product_id}:description"),
            Button.inline("📋 Instruction", f"a:pe:{product_id}:instruction"),
        ],
        [
            Button.inline("💬 Short text", f"a:pe:{product_id}:short_description"),
            Button.inline("🖼 Image", f"a:pe:{product_id}:image"),
        ],
        [
            Button.inline(
                "⏸ Disable" if product["is_active"] else "▶️ Enable",
                f"a:ptoggle:{product_id}:{0 if product['is_active'] else 1}",
            ),
            Button.inline("🗑 Delete", f"a:pdel:{product_id}"),
        ],
        [
            Button.inline(
                "◀️ Back",
                f"a:uoffers:{product['owner_user_id']}"
                if product["owner_user_id"]
                else "a:cat",
            )
        ],
    ]
