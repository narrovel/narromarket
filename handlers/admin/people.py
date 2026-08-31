# Users: roles, blocks, per client payment details and direct messages.

import logging

from telethon import Button, TelegramClient, events

from config import RECEIPTS_DIR
from db import journal
from db import orders as orders_db
from db import requisites as requisites_db
from db import subscriptions as subs_db
from db import users as users_db
from handlers.admin import base, cards
from handlers.admin.input import on_state
from services import access, notify
from utils import states, texts

logger = logging.getLogger(__name__)


async def show_user(event, user_id: int, actor: dict) -> bool:
    # Returns False when there is nothing to show; the caller then stops.
    user = await users_db.get_by_id(user_id)
    if not user:
        await event.answer("User not found", alert=True)
        return False
    subscriptions = await subs_db.list_for_user_with_user(user["id"])
    orders = await orders_db.list_for_user(user["id"], limit=5)
    method = await requisites_db.for_user(user)
    await base.show(
        event,
        cards.user_card(user, subscriptions, orders, method),
        cards.user_buttons(
            user,
            can_manage=access.can(actor, "roles") and access.can_manage_user(actor, user),
            can_requisites=access.can(actor, "requisites"),
            can_erase=actor["role"] == "owner"
            and user["role"] == "user"
            and user["telegram_id"] > 0
            and actor["id"] != user["id"],
        ),
    )
    return True


def _erase_refusal(actor: dict, target: dict) -> str:
    # Destroying a customer's data is irreversible, so it is an owner action, and never
    # one an admin can point at a colleague or at themselves to cover their tracks.
    if not target or target.get("telegram_id", 0) <= 0:
        return "User not found"
    if actor["role"] != "owner":
        return "Only an owner can erase personal data"
    if target["id"] == actor["id"]:
        return "You cannot erase yourself"
    if target["role"] != "user":
        return "Staff cannot be erased, change the role first"
    return ""


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^a:users$"))
    async def users_home(event):
        if not await base.actor(event, "users"):
            return
        recent = await users_db.recent(limit=10)
        lines = ["👥 <b>Users</b>", "", "Latest:"]
        rows = []
        for user in recent:
            mark = "🚫 " if user["is_blocked"] else ""
            lines.append(f"{mark}{users_db.display_name(user)} - {user['role']}")
            rows.append(
                [
                    Button.inline(
                        f"{mark}{users_db.display_name(user)[:28]}", f"a:user:{user['id']}"
                    )
                ]
            )
        rows.append(
            [Button.inline("🔍 Search", "a:ufind"), Button.inline("🧑‍💼 Staff", "a:staff")]
        )
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:ufind$"))
    async def users_find(event):
        user = await base.actor(event, "users")
        if not user:
            return
        await base.ask(
            event,
            "user_search",
            "users",
            "🔍 Send a @username, a name or a Telegram ID.",
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:staff$"))
    async def staff_list(event):
        actor = await base.actor(event, "users")
        if not actor:
            return
        team = await users_db.staff()
        lines = ["🧑‍💼 <b>Staff</b>", ""]
        rows = []
        for member in team:
            lines.append(
                f"{users_db.ROLE_TITLES.get(member['role'], member['role'])} "
                f"{users_db.display_name(member)}"
            )
            rows.append(
                [Button.inline(users_db.display_name(member)[:30], f"a:user:{member['id']}")]
            )
        rows.append([Button.inline("◀️ Users", "a:users")])
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:user:(\d+)$"))
    async def user_card(event):
        actor = await base.actor(event, "users")
        if not actor:
            return
        if await show_user(event, int(event.data.decode().split(":")[2]), actor):
            await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:roles:(\d+)$"))
    async def roles_menu(event):
        actor = await base.actor(event, "roles")
        if not actor:
            return
        user = await users_db.get_by_id(int(event.data.decode().split(":")[2]))
        if not user or user["telegram_id"] <= 0:
            await event.answer("User not found", alert=True)
            return
        if user["role"] == "owner":
            await event.answer("The owner role is managed through .env", alert=True)
            return
        if not access.can_manage_user(actor, user):
            await event.answer("You cannot manage this user", alert=True)
            return

        rows = []
        for role in ("user", "manager", "admin"):
            if not access.can_assign_role(actor, user, role):
                continue
            mark = "✅ " if user["role"] == role else ""
            rows.append(
                [
                    Button.inline(
                        f"{mark}{users_db.ROLE_TITLES[role]}", f"a:role:{user['id']}:{role}"
                    )
                ]
            )
        rows.append([Button.inline("◀️ Back", f"a:user:{user['id']}")])
        await base.show(
            event,
            f"🎚 <b>Role for {users_db.display_name(user)}</b>\n\n"
            "A manager works with orders and subscriptions. An admin also manages the "
            "catalog, prices, payment details and settings.",
            rows,
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:role:(\d+):(user|manager|admin)$"))
    async def set_role(event):
        actor = await base.actor(event, "roles")
        if not actor:
            return
        _, _, user_id, role = event.data.decode().split(":")
        user = await users_db.get_by_id(int(user_id))
        if not user or user["telegram_id"] <= 0:
            await event.answer("User not found", alert=True)
            return
        if not access.can_assign_role(actor, user, role):
            await event.answer("You cannot grant this role", alert=True)
            return

        await users_db.set_role(user["id"], role)
        await journal.action(actor["telegram_id"], "role_set", f"user:{user['id']}", role)
        await notify.to_user(
            event.client,
            user["telegram_id"],
            f"🎚 Your role in the bot is now: {users_db.ROLE_TITLES.get(role, role)}",
            expected_user_id=user["id"],
        )
        await event.answer("Role updated")
        await show_user(event, user["id"], actor)

    @client.on(events.CallbackQuery(pattern=rb"^a:block:(\d+):(0|1)$"))
    async def toggle_block(event):
        actor = await base.actor(event, "roles")
        if not actor:
            return
        _, _, user_id, desired_raw = event.data.decode().split(":")
        user = await users_db.get_by_id(int(user_id))
        if not user:
            await event.answer("User not found", alert=True)
            return
        if not access.can_manage_user(actor, user):
            await event.answer("You cannot block or unblock this user", alert=True)
            return

        desired = bool(int(desired_raw))
        await users_db.set_blocked(user["id"], desired)
        await journal.action(
            actor["telegram_id"],
            "user_block" if desired else "user_unblock",
            f"user:{user['id']}",
        )
        await show_user(event, user["id"], actor)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:block:(\d+)$"))
    async def stale_block_button(event):
        if await base.actor(event, "roles"):
            await event.answer("This button is outdated. Reopen the user profile.", alert=True)

    @client.on(events.CallbackQuery(pattern=rb"^a:erase:(\d+)$"))
    async def erase_confirm(event):
        actor = await base.actor(event, "roles")
        if not actor:
            return
        target = await users_db.get_by_id(int(event.data.decode().split(":")[2]))
        problem = _erase_refusal(actor, target)
        if problem:
            await event.answer(problem, alert=True)
            return
        await event.answer()
        await base.show(
            event,
            f"🧹 <b>Erase the personal data of {users_db.display_name(target)}?</b>\n\n"
            "Name, username, note, profile Telegram id, access details, uploaded receipts "
            "and activity history are removed. Order amounts and dates stay so the books "
            "still add up. A paid Stars order also keeps its original payment recipient id "
            "for later refunds and financial reconciliation. This cannot be undone.",
            [
                [Button.inline("🧹 Erase", f"a:erasego:{target['id']}")],
                [Button.inline("◀️ Back", f"a:user:{target['id']}")],
            ],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:erasego:(\d+)$"))
    async def erase_go(event):
        actor = await base.actor(event, "roles")
        if not actor:
            return
        target = await users_db.get_by_id(int(event.data.decode().split(":")[2]))
        problem = _erase_refusal(actor, target)
        if problem:
            await event.answer(problem, alert=True)
            return
        try:
            result = await users_db.erase(target["id"])
        except users_db.EraseBlockedError as error:
            await event.answer(str(error), alert=True)
            return
        if result is None:
            await event.answer("User not found", alert=True)
            return
        removed = 0
        failed = 0
        for receipt in result["receipts"]:
            order_id = receipt["order_id"]
            name = receipt["receipt_file"]
            if not name or "/" in name or "\\" in name or name.startswith("."):
                failed += 1
                logger.warning("Receipt %r was not removed during erasure: invalid name", name)
                continue
            try:
                path = RECEIPTS_DIR / name
                existed = path.exists()
                path.unlink(missing_ok=True)
                if not await orders_db.forget_receipt(order_id, name):
                    failed += 1
                    logger.warning(
                        "Receipt %s was removed but order %s no longer points at it",
                        name,
                        order_id,
                    )
                    continue
                removed += 1 if existed else 0
            except Exception as error:
                failed += 1
                logger.warning("Receipt %s was not removed during erasure: %s", name, error)
        await journal.action(
            actor["telegram_id"],
            "user_erase",
            f"user:{target['id']}",
            f"receipts removed:{removed} failed:{failed}",
        )
        await event.answer("Erased")
        await base.show(
            event,
            f"🧹 Personal data of user #{target['id']} erased, "
            f"{removed} receipt file(s) removed."
            + (f" {failed} file(s) need manual cleanup." if failed else ""),
            [base.home_row()],
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:pm:(\d+)$"))
    async def payment_method_menu(event):
        actor = await base.actor(event, "requisites")
        if not actor:
            return
        user = await users_db.get_by_id(int(event.data.decode().split(":")[2]))
        if not user or user["telegram_id"] <= 0:
            await event.answer("User not found", alert=True)
            return

        methods = await requisites_db.list_all(active_only=True)
        rows = []
        for method in methods:
            mark = "✅ " if user["payment_method_id"] == method["id"] else ""
            rows.append(
                [
                    Button.inline(
                        f"{mark}{method['title']}", f"a:pmset:{user['id']}:{method['id']}"
                    )
                ]
            )
        rows.append([Button.inline("🚫 No transfers", f"a:pmset:{user['id']}:0")])
        rows.append([Button.inline("◀️ Back", f"a:user:{user['id']}")])
        await base.show(
            event,
            f"💳 <b>Payment details for {users_db.display_name(user)}</b>\n\n"
            "A client sees the transfer option only when payment details are assigned, "
            "or when transfers are enabled for everyone in settings.",
            rows,
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:pmset:(\d+):(\d+)$"))
    async def payment_method_set(event):
        actor = await base.actor(event, "requisites")
        if not actor:
            return
        _, _, user_id, method_id = event.data.decode().split(":")
        user = await users_db.get_by_id(int(user_id))
        if not user or user["telegram_id"] <= 0:
            await event.answer("User not found", alert=True)
            return
        selected_method_id = int(method_id)
        if selected_method_id:
            method = await requisites_db.get(selected_method_id)
            if not method or method["is_deleted"] or not method["is_active"]:
                await event.answer("Payment details are no longer available", alert=True)
                return
        await users_db.set_payment_method(user["id"], selected_method_id or None)
        await journal.action(
            actor["telegram_id"], "user_payment_method", f"user:{user['id']}", method_id
        )
        await event.answer("Saved")
        await show_user(event, user["id"], actor)

    @client.on(events.CallbackQuery(pattern=rb"^a:writeu:(\d+)$"))
    @client.on(events.CallbackQuery(pattern=rb"^a:write:(\d+)$"))
    async def write_user(event):
        actor = await base.actor(event, "users")
        if not actor:
            return
        _, action, raw_target = event.data.decode().split(":")
        target = (
            await users_db.get_by_id(int(raw_target))
            if action == "writeu"
            else await users_db.get(int(raw_target))
        )
        if not target or target["telegram_id"] <= 0:
            await event.answer("User not found", alert=True)
            return
        await base.ask(
            event,
            "write_user",
            "users",
            f"✏️ Message for client {users_db.display_name(target)}",
            target_user_id=target["id"],
        )


@on_state("user_search")
async def user_search(event, user, data, text):
    found = await users_db.search(text)
    if not found:
        await base.respond(event, "Nobody found. Try another query.")
        return

    states.clear_for(event)
    rows = [
        [Button.inline(users_db.display_name(item)[:30], f"a:user:{item['id']}")]
        for item in found[:10]
    ]
    await base.respond(event, f"Found: {len(found)}", buttons=rows)


@on_state("write_user")
async def write_user_message(event, user, data, text):
    states.clear_for(event)
    target = await users_db.get_by_id(data["target_user_id"])
    if not target or target["telegram_id"] <= 0:
        await base.respond(event, "User not found.")
        return
    delivered = await notify.to_user(
        event.client,
        target["telegram_id"],
        f"📩 <b>Message from the manager</b>\n\n{texts.escape(text)}",
        expected_user_id=target["id"],
    )
    await journal.action(user["telegram_id"], "write_user", f"user:{target['id']}")
    await base.respond(
        event, "✅ Sent." if delivered else "❌ Not delivered, the user is unreachable."
    )
