# Bot settings and broadcasts.

import asyncio
import logging
import secrets
import time

from telethon import Button, TelegramClient, events

from db import journal, settings
from db import users as users_db
from handlers.admin import base
from handlers.admin.input import on_state
from services import notify, scheduler
from utils import states, texts

logger = logging.getLogger(__name__)

_pending_broadcast: dict[int, dict] = {}
BROADCAST_PREVIEW_TTL = 15 * 60

# Read by the scheduler when it builds its jobs, so changing one has to reschedule.
SCHEDULE_KEYS = ("check_hour", "check_minute", "monthly_report_day")

SHORT_KEYS = (
    "bot_name",
    "manager_username",
    "star_to_rub",
    "notify_days_before",
    "check_hour",
    "check_minute",
    "monthly_report_day",
    "catalog_per_page",
    "require_username",
    "transfer_for_all",
)


def register(client: TelegramClient) -> None:
    @client.on(events.CallbackQuery(pattern=rb"^a:set$"))
    async def settings_home(event):
        if not await base.actor(event, "settings"):
            return
        lines = ["⚙️ <b>Settings</b>", ""]
        rows = []
        for key in SHORT_KEYS:
            lines.append(
                f"{settings.TITLES.get(key, key)}: "
                f"<b>{texts.escape(settings.get(key) or '-')}</b>"
            )
            rows.append([Button.inline(settings.TITLES.get(key, key), f"a:sete:{key}")])
        rows.append(
            [
                Button.inline("📝 Welcome text", "a:sete:welcome_text"),
                Button.inline("❓ Help text", "a:sete:help_text"),
            ]
        )
        rows.append(
            [
                Button.inline("📄 Payment terms", "a:sete:terms_text"),
                Button.inline("🛟 Support text", "a:sete:support_text"),
            ]
        )
        rows.append(base.home_row())
        lines += ["", "<i>Changes apply immediately, the daily checks included.</i>"]
        await base.show(event, "\n".join(lines), rows)
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:sete:"))
    async def setting_edit(event):
        user = await base.actor(event, "settings")
        if not user:
            return
        key = event.data.decode().split(":")[2]
        if key not in settings.DEFAULTS:
            await event.answer("Unknown setting", alert=True)
            return
        current = settings.get(key)
        preview = current if len(current) < 300 else current[:300] + "..."
        await base.ask(
            event,
            "setting_value",
            "settings",
            f"⚙️ {settings.TITLES.get(key, key)}\n\nCurrent:\n"
            f"<code>{texts.escape(preview) or 'empty'}</code>",
            key=key,
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:cast$"))
    async def broadcast_start(event):
        user = await base.actor(event, "broadcast")
        if not user:
            return
        await base.ask(
            event,
            "broadcast",
            "broadcast",
            "📣 Broadcast text. HTML formatting is supported.",
        )

    @client.on(events.CallbackQuery(pattern=rb"^a:castgo:([A-Za-z0-9_-]+)$"))
    async def broadcast_send(event):
        user = await base.actor(event, "broadcast")
        if not user:
            return
        pending = _pending_broadcast.get(user["telegram_id"])
        nonce = event.data.decode().rsplit(":", 1)[1]
        if (
            not pending
            or pending["nonce"] != nonce
            or time.monotonic() - pending["created_at"] > BROADCAST_PREVIEW_TTL
        ):
            await event.answer("This preview has expired, start again", alert=True)
            return
        text = pending["text"]
        _pending_broadcast.pop(user["telegram_id"], None)

        recipients = await users_db.all_recipients()
        await base.show(event, f"📣 Sending to {len(recipients)} recipients...")
        sent = 0
        for recipient in recipients:
            if await notify.to_user(
                event.client,
                recipient["telegram_id"],
                text,
                expected_user_id=recipient["id"],
            ):
                sent += 1
            # Telegram tolerates about 30 messages per second for a bot.
            await asyncio.sleep(0.05)
        await journal.action(user["telegram_id"], "broadcast", details=f"sent:{sent}")
        await base.respond(
            event,
            f"📣 Broadcast finished: {sent} of {len(recipients)} delivered.",
            buttons=[base.home_row()],
        )
        await event.answer()

    @client.on(events.CallbackQuery(pattern=rb"^a:castcancel:([A-Za-z0-9_-]+)$"))
    async def broadcast_cancel(event):
        user = await base.actor(event, "broadcast")
        if not user:
            return
        pending = _pending_broadcast.get(user["telegram_id"])
        nonce = event.data.decode().rsplit(":", 1)[1]
        if pending and pending["nonce"] == nonce:
            _pending_broadcast.pop(user["telegram_id"], None)
        await event.answer("Cancelled")
        await base.show(event, "Broadcast cancelled.", [base.home_row()])


@on_state("setting_value")
async def save_setting(event, user, data, text):
    key = data["key"]
    # Range checked before it is stored. An out of range hour reached CronTrigger only
    # at the next start, and the bot then crash looped with no way to fix it from the
    # panel that lives inside the bot.
    value, error = settings.validate(key, text)
    if error:
        await base.respond(event, f"❌ {error} Try again or type 'cancel'.")
        return

    states.clear_for(event)
    await settings.set_value(key, value)
    await journal.action(user["telegram_id"], "setting", key, value[:100])

    note = ""
    if key in SCHEDULE_KEYS:
        note = " Schedule updated." if scheduler.reschedule() else " Applies after a restart."
    await base.respond(
        event, f"✅ {settings.TITLES.get(key, key)} updated.{note}", buttons=[base.home_row()]
    )


@on_state("broadcast")
async def prepare_broadcast(event, user, data, text):
    if len(text) > 3500:
        await base.respond(
            event,
            f"The text is {len(text)} characters, Telegram takes about 4000. "
            "Shorten it and send again.",
        )
        return

    states.clear_for(event)
    nonce = secrets.token_urlsafe(8)
    _pending_broadcast[user["telegram_id"]] = {
        "text": text,
        "nonce": nonce,
        "created_at": time.monotonic(),
    }
    recipients = await users_db.all_telegram_ids()
    await base.respond(
        event,
        f"📣 <b>Preview</b>\n\n{text}\n\nRecipients: {len(recipients)}",
        buttons=[
            [Button.inline("📤 Send", f"a:castgo:{nonce}")],
            [Button.inline("Cancel", f"a:castcancel:{nonce}")],
        ],
        parse_mode="html",
    )
