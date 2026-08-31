# Admin panel home screen and statistics.

from telethon import Button, TelegramClient, events

from db import journal, settings
from db import stats as stats_db
from handlers.admin import base
from services import access
from utils import dates, states, texts


def panel_buttons(user: dict, pending: int) -> list:
    rows = [[Button.inline(f"📋 Orders ({pending})", "a:orders")]]
    rows.append(
        [
            Button.inline("📦 Subscriptions", "a:subs"),
            Button.inline("📊 Stats", "a:stats"),
        ]
    )
    if access.can(user, "catalog"):
        rows.append(
            [
                Button.inline("🛒 Catalog", "a:cat"),
                Button.inline("🎁 Personal offers", "a:offers"),
            ]
        )
    if access.can(user, "users"):
        rows.append([Button.inline("👥 Users", "a:users")])
    if access.can(user, "requisites"):
        rows.append(
            [
                Button.inline("💳 Payment details", "a:req"),
                Button.inline("⚙️ Settings", "a:set"),
            ]
        )
    if access.can(user, "broadcast"):
        rows.append([Button.inline("📣 Broadcast", "a:cast")])
    if access.can(user, "settings"):
        rows.append([Button.inline("📜 Audit log", "a:log")])
    rows.append([Button.inline("🏠 Bot menu", "menu:main")])
    return rows


async def panel_text(data: dict = None) -> str:
    # The caller already has the numbers on the home screen: reading them twice meant
    # scanning the orders and users tables twice for one tap.
    data = data or await stats_db.dashboard()
    return (
        f"🛠 <b>{texts.escape(settings.get('bot_name'))} admin</b>\n\n"
        f"👥 Users: {data['users']} (+{data['users_today']} today)\n"
        f"📦 Active subscriptions: {data['active_subscriptions']}\n"
        f"📋 Orders today: {data['orders_today']}\n"
        f"⚠️ Waiting for staff: {data['needs_attention']}\n"
        f"🛒 Products: {data['products']} plus {data['personal_offers']} personal"
    )


def register(client: TelegramClient) -> None:
    @client.on(events.NewMessage(pattern=r"^/admin$"))
    async def admin_command(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        states.clear_for(event)
        data = await stats_db.dashboard()
        await base.respond(
            event,
            await panel_text(data),
            buttons=panel_buttons(user, data["needs_attention"]),
            parse_mode="html",
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:home$"))
    async def admin_home(event):
        user = await base.actor(event, "orders")
        if not user:
            return
        states.clear_for(event)
        data = await stats_db.dashboard()
        await base.show(
            event, await panel_text(data), panel_buttons(user, data["needs_attention"])
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:log(:\d+)?$"))
    async def audit_log(event):
        # The trail was written from more than twenty places and read from none, so after
        # an incident nobody could see who refunded what without opening sqlite by hand.
        if not await base.actor(event, "settings"):
            return
        page = base.page_from(event, 2)
        entries = await journal.recent_actions(limit=base.PAGE_SIZE * 10)
        if not entries:
            await base.show(event, "📜 Nothing recorded yet.", [base.home_row()])
            await event.answer()
            return

        lines = ["📜 <b>Staff actions</b>", ""]
        for entry in base.page_slice(entries, page):
            details = f" - {texts.escape(entry['details'])}" if entry["details"] else ""
            lines.append(
                f"{dates.fmt_datetime(entry['created_at'])} | "
                f"<code>{entry['admin_id']}</code> | "
                f"{texts.escape(entry['action'])} "
                f"{texts.escape(entry['target'] or '')}{details}"
            )
        rows = []
        total_pages = max(1, (len(entries) + base.PAGE_SIZE - 1) // base.PAGE_SIZE)
        page = base.clamp_page(entries, page)
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️", f"a:log:{page - 1}"))
        nav.append(Button.inline(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(Button.inline("▶️", f"a:log:{page + 1}"))
        if len(nav) > 1:
            rows.append(nav)
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:stats$"))
    async def admin_stats(event):
        user = await base.actor(event, "stats")
        if not user:
            return

        data = await stats_db.dashboard()
        expected = await stats_db.expected_renewal_income()
        lines = [
            "📊 <b>Statistics</b>",
            "",
            f"👥 Users: {data['users']} (+{data['users_today']} today)",
            f"📦 Active subscriptions: {data['active_subscriptions']}",
            f"💰 Collected: {data['revenue_stars']}⭐ and {int(data['revenue_rub'])}₽",
            f"🧾 Paid orders: {data['paid_orders']}",
            "",
            f"🔮 If everyone renews: {expected['stars']}⭐ and {int(expected['rub'])}₽",
            "",
            "<b>Orders by status</b>",
        ]
        for row in await stats_db.by_status():
            lines.append(f"• {row['status']}: {row['count']}")

        top = await stats_db.top_products()
        if top:
            lines += ["", "<b>Top products</b>"]
            for row in top:
                lines.append(f"• {row['emoji']} {row['product_name']}: {row['count']}")

        await base.show(event, "\n".join(lines), [base.home_row()])
        await event.answer()
