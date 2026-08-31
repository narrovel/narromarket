# Payment details used for bank transfers.

import logging

from telethon import Button, TelegramClient, events

from db import journal
from db import requisites as requisites_db
from handlers.admin import base
from handlers.admin.input import on_state
from utils import states, texts

logger = logging.getLogger(__name__)

FIELD_PROMPTS = {
    "title": "Internal title, staff only",
    "details": "Phone number or card number",
    "bank": "Bank",
    "holder": "Recipient",
}

NEW_STEPS = ("title", "details", "bank", "holder")
NEW_PROMPTS = {
    "details": "Phone number for fast payments or a card number",
    "bank": "Bank",
    "holder": "Recipient, for example 'John D.'",
}


def method_card(method: dict) -> str:
    lines = [
        f"💳 <b>{texts.escape(method['title'])}</b>",
        "",
        f"Kind: {requisites_db.KIND_TITLES.get(method['kind'], method['kind'])}",
        f"Details: <code>{texts.escape(method['details'])}</code>",
        f"Bank: {texts.escape(method['bank'] or '-')}",
        f"Recipient: {texts.escape(method['holder'] or '-')}",
        "⭐ used by default" if method["is_default"] else "",
        "✅ enabled" if method["is_active"] else "⏸ disabled",
    ]
    return "\n".join(line for line in lines if line != "")


def method_buttons(method: dict) -> list:
    method_id = method["id"]
    return [
        [
            Button.inline("✏️ Title", f"a:re:{method_id}:title"),
            Button.inline("🔢 Details", f"a:re:{method_id}:details"),
        ],
        [
            Button.inline("🏦 Bank", f"a:re:{method_id}:bank"),
            Button.inline("👤 Recipient", f"a:re:{method_id}:holder"),
        ],
        [
            Button.inline("🔀 Card / fast payments", f"a:rkind:{method_id}"),
            Button.inline("⭐ Make default", f"a:rdef:{method_id}"),
        ],
        [
            Button.inline(
                "⏸ Disable" if method["is_active"] else "▶️ Enable", f"a:rtoggle:{method_id}"
            ),
            Button.inline("🗑 Delete", f"a:rdel:{method_id}"),
        ],
        [Button.inline("◀️ Payment details", "a:req")],
    ]


async def show_method(event, method_id: int) -> bool:
    # Returns False when there is nothing to show; the caller then stops.
    method = await requisites_db.get(method_id)
    if not method:
        await event.answer("Payment details not found", alert=True)
        return False
    await base.show(event, method_card(method), method_buttons(method))
    return True


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^a:req$"))
    async def requisites_home(event):
        if not await base.actor(event, "requisites"):
            return
        methods = await requisites_db.list_all()
        lines = ["💳 <b>Payment details</b>", ""]
        rows = []
        for method in methods:
            mark = "⭐ " if method["is_default"] else ""
            if not method["is_active"]:
                mark = "⏸ "
            lines.append(
                f"{mark}{texts.escape(method['title'])}: {requisites_db.describe(method)}"
            )
            rows.append([Button.inline(f"{mark}{method['title'][:28]}", f"a:r:{method['id']}")])
        if not methods:
            lines.append("None yet. Bank transfers stay unavailable without them.")
        rows.append([Button.inline("➕ Add", "a:rnew")])
        rows.append(base.home_row())
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:r:(\d+)$"))
    async def method_view(event):
        if not await base.actor(event, "requisites"):
            return
        if await show_method(event, int(event.data.decode().split(":")[2])):
            await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:re:(\d+):"))
    async def method_edit(event):
        user = await base.actor(event, "requisites")
        if not user:
            return
        _, _, method_id, field = event.data.decode().split(":")
        if field not in FIELD_PROMPTS:
            await event.answer("This field is not editable", alert=True)
            return
        await base.ask(
            event,
            "req_field",
            "requisites",
            f"✏️ {FIELD_PROMPTS[field]}",
            method_id=int(method_id),
            field=field,
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:rkind:(\d+)$"))
    async def method_kind(event):
        user = await base.actor(event, "requisites")
        if not user:
            return
        method_id = int(event.data.decode().split(":")[2])
        method = await requisites_db.get(method_id)
        if not method:
            await event.answer("Not found", alert=True)
            return
        await requisites_db.update(method_id, kind="card" if method["kind"] == "sbp" else "sbp")
        await show_method(event, method_id)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:rtoggle:(\d+)$"))
    async def method_toggle(event):
        user = await base.actor(event, "requisites")
        if not user:
            return
        method_id = int(event.data.decode().split(":")[2])
        method = await requisites_db.get(method_id)
        if not method:
            await event.answer("Not found", alert=True)
            return
        await requisites_db.update(method_id, is_active=0 if method["is_active"] else 1)
        await show_method(event, method_id)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:rdef:(\d+)$"))
    async def method_default(event):
        user = await base.actor(event, "requisites")
        if not user:
            return
        method_id = int(event.data.decode().split(":")[2])
        if not await requisites_db.set_default(method_id):
            await event.answer("Only active payment details can be the default", alert=True)
            return
        await journal.action(user["telegram_id"], "requisite_default", f"method:{method_id}")
        await event.answer("Done")
        await show_method(event, method_id)

    @client.on(events.CallbackQuery(pattern=rb"^a:rdel:(\d+)$"))
    async def method_delete_confirm(event):
        if not await base.actor(event, "requisites"):
            return
        method_id = int(event.data.decode().split(":")[2])
        await base.show(
            event,
            "🗑 Delete these details? Clients assigned to them fall back to the default ones.",
            [
                [Button.inline("Yes, delete", f"a:rdelyes:{method_id}")],
                [Button.inline("Cancel", f"a:r:{method_id}")],
            ],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:rdelyes:(\d+)$"))
    async def method_delete(event):
        user = await base.actor(event, "requisites")
        if not user:
            return
        method_id = int(event.data.decode().split(":")[2])
        await requisites_db.delete(method_id)
        await journal.action(user["telegram_id"], "requisite_delete", f"method:{method_id}")
        await base.show(event, "🗑 Deleted.", [[Button.inline("◀️ Payment details", "a:req")]])
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:rnew$"))
    async def method_new(event):
        user = await base.actor(event, "requisites")
        if not user:
            return
        await base.ask(
            event,
            "req_new",
            "requisites",
            "➕ Internal title, for example 'Main card'",
            step=0,
            values={},
        )


@on_state("req_field")
async def save_method_field(event, user, data, text):
    states.clear_for(event)
    if len(text) > 200:
        await base.respond(event, "❌ Too long, keep it under 200 characters.")
        return
    method = await requisites_db.get(data["method_id"])
    if not method or method["is_deleted"]:
        await base.respond(event, "Payment details not found.")
        return
    await requisites_db.update(data["method_id"], **{data["field"]: text})
    await journal.action(
        user["telegram_id"], "requisite_edit", f"method:{data['method_id']}", data["field"]
    )
    method = await requisites_db.get(data["method_id"])
    await base.respond(
        event,
        "✅ Saved.\n\n" + method_card(method),
        buttons=method_buttons(method),
        parse_mode="html",
    )


@on_state("req_new")
async def create_method(event, user, data, text):
    values = data["values"]
    step = data["step"]
    values[NEW_STEPS[step]] = text

    if step + 1 < len(NEW_STEPS):
        states.set_for(event, "req_new", "requisites", step=step + 1, values=values)
        await base.respond(event, NEW_PROMPTS[NEW_STEPS[step + 1]])
        return

    states.clear_for(event)
    digits = "".join(char for char in values["details"] if char.isdigit())
    method_id = await requisites_db.create(
        title=values["title"],
        kind="card" if len(digits) >= 16 else "sbp",
        details=values["details"],
        bank=values.get("bank", ""),
        holder=values.get("holder", ""),
    )
    await journal.action(user["telegram_id"], "requisite_create", f"method:{method_id}")
    method = await requisites_db.get(method_id)
    await base.respond(
        event,
        "✅ Added. Double check the kind: card or fast payments.\n\n" + method_card(method),
        buttons=method_buttons(method),
        parse_mode="html",
    )
