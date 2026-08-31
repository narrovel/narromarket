import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import (
    EVENT_RETENTION_DAYS,
    RECEIPT_RETENTION_DAYS,
    RECEIPT_TTL_HOURS,
    RECEIPTS_DIR,
    REVIEW_STALE_HOURS,
)
from db import connection, journal, settings
from db import orders as orders_db
from db import stats as stats_db
from db import subscriptions as subs_db
from db import users as users_db
from services import billing, notify
from utils import dates, keyboards, states, texts

logger = logging.getLogger(__name__)

_client = None
_scheduler: AsyncIOScheduler = None
_checks_lock = asyncio.Lock()
_reminders_lock = asyncio.Lock()
_active_jobs: set[asyncio.Task] = set()

# A run that starts late must still run: the default grace period is one second, and a
# skipped reminder is never sent at all.
MISFIRE_GRACE = 3600
STOP_GRACE_SECONDS = 20

# A wrong server clock makes every subscription expire in one pass, and closing one
# wipes its access details for good. Past this share of the active base the sweep stops
# and asks a human instead.
MAX_EXPIRY_SHARE = 0.25
MIN_EXPIRY_BATCH = 20


def set_client(client) -> None:
    global _client
    _client = client


def _cron_settings() -> dict:
    return {
        "check_hour": settings.get_int("check_hour", 10),
        "check_minute": settings.get_int("check_minute", 0),
        "monthly_report_day": settings.get_int("monthly_report_day", 1),
    }


async def notify_expiring() -> None:
    async with _reminders_lock:
        await _notify_expiring()


async def _notify_expiring() -> None:
    if not _client:
        return

    days_before = max(1, settings.get_int("notify_days_before", 3))
    # The one day pass runs first and also closes the earlier reminder, so a subscription
    # picked up after a missed day gets one message, not two.
    schedule = [(1, "notified_1d", ("notified_1d", "notified_3d"))]
    if days_before > 1:
        schedule.append((days_before, "notified_3d", ("notified_3d",)))

    attempted = set()
    for days, flag, flags_to_set in schedule:
        for subscription in await subs_db.expiring_in(days, flag):
            if subscription["id"] in attempted:
                continue
            # Claim first: the flag is what stops a second send, so it has to be won
            # before the message goes out, not after.
            if not await subs_db.claim_notification(
                subscription["id"], flag, subscription["expires_at"]
            ):
                continue
            attempted.add(subscription["id"])
            claimed_flags = [flag]
            for name in flags_to_set:
                if name != flag and await subs_db.claim_notification(
                    subscription["id"], name, subscription["expires_at"]
                ):
                    claimed_flags.append(name)
            left = dates.days_left(subscription["expires_at"])
            delivered = False
            try:
                delivered = await notify.to_user(
                    _client,
                    subscription["telegram_id"],
                    texts.expiring_soon(subscription, left if left is not None else days),
                    keyboards.renew_button(subscription.get("product_id")),
                    expected_user_id=subscription["user_id"],
                )
            finally:
                if not delivered:
                    # The flags are a delivery record, not merely an attempt record. A
                    # Telegram error or forced shutdown leaves this eligible for retry.
                    await subs_db.release_notifications(
                        subscription["id"],
                        tuple(claimed_flags),
                        subscription["expires_at"],
                    )
            logger.info(
                "Reminder %s days before for subscription %s: %s",
                left,
                subscription["id"],
                "delivered" if delivered else "not delivered",
            )


async def close_expired() -> None:
    if not _client:
        return

    due_count = await connection.fetch_value(
        "SELECT COUNT(*) FROM subscriptions WHERE status = ? AND expires_at <= ?",
        (subs_db.ACTIVE, dates.to_sql(dates.utcnow())),
        0,
    )
    active = await subs_db.count_active()
    if due_count >= MIN_EXPIRY_BATCH and due_count > active * MAX_EXPIRY_SHARE:
        # Almost certainly a clock jump rather than a real wave of expiries.
        logger.error(
            "Refusing to expire %s of %s active subscriptions in one pass", due_count, active
        )
        await notify.to_staff(
            _client,
            f"⚠️ {due_count} of {active} subscriptions look expired at once. The sweep was "
            "stopped: check the server clock before anything is closed.",
        )
        return

    due = await subs_db.expired()
    for subscription in due:
        staff_message = None
        async with users_db.lifecycle_lock(subscription["user_id"]):
            current_user = await users_db.get_by_id(subscription["user_id"])
            current = await subs_db.get(subscription["id"])
            if (
                not current_user
                or current_user["telegram_id"] <= 0
                or not current
                or current["telegram_id"] != current_user["telegram_id"]
            ):
                continue
            # Status changes even when the customer blocked the bot, otherwise the same
            # subscription would be reprocessed on every run. Keep the lifecycle lock
            # through the customer notice so erasure cannot overtake its final send.
            if not await billing.claim_expired(current["id"], current["expires_at"]):
                continue
            await notify.to_user(
                _client,
                current["telegram_id"],
                texts.expired(current),
                keyboards.renew_button(current.get("product_id")),
                lifecycle_held=True,
            )
            # Keep customer profile fields out of the later broadcast: the profile may
            # be erased as soon as this lock is released.
            staff_message = (
                f"⌛ Subscription #{current['id']} ended\n"
                f"{texts.escape(current['emoji'])} "
                f"{texts.escape(current['product_name'])}"
            )
        # Never acquire staff lifecycle locks while holding the subject's lock: the
        # subject may itself be staff, and two simultaneous expiries could otherwise
        # wait on each other forever.
        if staff_message:
            await notify.to_staff(_client, staff_message)
            logger.info("Subscription %s marked as expired", subscription["id"])


async def close_stale_orders() -> None:
    if not _client:
        return

    for order in await orders_db.stale_receipt_orders(RECEIPT_TTL_HOURS * 60):
        if not await orders_db.claim_status(
            order["id"], orders_db.PAYMENT_EXPIRED, (orders_db.PENDING_RECEIPT,)
        ):
            continue
        await notify.to_user(
            _client,
            order["telegram_id"],
            f"⏰ Order #{order['id']} was closed: the receipt never arrived. "
            "Start a new order whenever you are ready to pay.",
            expected_user_id=order["user_id"],
        )
        logger.info("Order %s closed by payment timeout", order["id"])


async def flag_stale_reviews() -> None:
    # A receipt nobody looked at blocks the customer from buying that product again, and
    # there is no other way out of pending_review.
    if not _client:
        return

    for order in await orders_db.stale_review_orders(REVIEW_STALE_HOURS):
        if not await orders_db.claim_status(
            order["id"], orders_db.PROBLEM, (orders_db.PENDING_REVIEW,)
        ):
            continue
        await notify.to_staff(
            _client,
            f"🆘 Order #{order['id']} has been waiting for review for "
            f"{REVIEW_STALE_HOURS} hours.\n"
            f"{texts.escape(order['emoji'])} {texts.escape(order['product_name'])}",
        )
        logger.warning("Order %s left pending_review for too long", order["id"])


def _receipt_path(name: str):
    # The name comes out of the database and is about to be deleted, so it is checked
    # the same way a product image name is before it is read.
    if not name or "/" in name or "\\" in name or name.startswith("."):
        logger.error("Refusing to touch a receipt named %r", name)
        return None
    return RECEIPTS_DIR / name


async def prune_receipts() -> None:
    # Customers' bank documents, on a box where nothing else ever shrinks. Removed only
    # when a closed order says they may go: a file is never deleted just because the
    # database does not know about it, because that is exactly what a restored backup
    # looks like.
    removed = 0
    for order in await orders_db.receipts_to_forget(RECEIPT_RETENTION_DAYS):
        path = _receipt_path(order["receipt_file"])
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
            await orders_db.forget_receipt(order["id"], order["receipt_file"])
        except asyncio.CancelledError:
            # Cancellation can arrive at the database await immediately after unlink.
            # Finish the idempotent pointer cleanup before the connection is closed.
            await orders_db.forget_receipt(order["id"], order["receipt_file"])
            raise
        removed += 1
    if removed:
        logger.info("Removed %s receipts past their retention window", removed)


async def report_orphan_receipts() -> None:
    # Files a crash left behind. Reported, never deleted automatically: after a restore
    # every receipt the older database has forgotten would look like an orphan.
    if not _client:
        return
    known = await orders_db.known_receipt_files()
    orphans = [
        path.name
        for path in RECEIPTS_DIR.glob("*")
        if path.is_file()
        and path.name not in known
        and (time.time() - path.stat().st_mtime) / 86400 > 7
    ]
    if not orphans:
        return
    logger.warning("%s receipt file(s) no order points at", len(orphans))
    await notify.to_staff(
        _client,
        f"🗂 {len(orphans)} receipt file(s) in data/receipts are not linked to any "
        "order. Nothing was deleted. Check whether a restore lost the links before "
        "removing them by hand.",
    )


async def prune_events() -> None:
    removed = await journal.prune_events(EVENT_RETENTION_DAYS)
    if removed:
        logger.info("Pruned %s old events", removed)


async def monthly_report_if_due() -> None:
    # The job store is rebuilt at every start, so a cron slot missed during a restart is
    # simply skipped and the report is lost for the whole month.
    month = dates.local_today().strftime("%Y-%m")
    if settings.get("last_monthly_report") == month:
        return
    if dates.local_today().day < settings.get_int("monthly_report_day", 1):
        return
    delivered = await monthly_report()
    if delivered:
        await settings.set_value("last_monthly_report", month)
    else:
        logger.warning("Monthly report was not delivered; it will be retried")


async def reconcile() -> None:
    # Nothing repairs a crash between two commits, so at least name what is broken.
    if not _client:
        return
    found = await orders_db.inconsistencies()
    lines = []
    for name, rows in found.items():
        if rows:
            ids = ", ".join(f"#{row['id']}" for row in rows)
            lines.append(f"• {name.replace('_', ' ')}: {ids}")
    if not lines:
        return
    logger.warning("Reconciliation found inconsistencies: %s", lines)
    await notify.to_staff(
        _client,
        "🧾 <b>These orders need a look</b>\n\n" + "\n".join(lines),
    )


async def _run_task(name: str, task) -> None:
    try:
        await task()
    except Exception:
        logger.exception("%s failed", name)


async def _run_scheduled(task) -> None:
    """Let an in-flight job finish when APScheduler cancels its executor future."""
    running = asyncio.create_task(task())
    _active_jobs.add(running)
    try:
        try:
            await asyncio.shield(running)
        except asyncio.CancelledError:
            # AsyncIOExecutor cancels coroutine jobs even when shutdown(wait=True) is
            # requested. Keep the actual work alive; stop() waits for this task before
            # the database is closed.
            await running
    finally:
        _active_jobs.discard(running)


async def run_checks() -> None:
    # The startup catch-up can coincide with the cron slot. Serialize the two runs, while
    # keeping failures isolated so one broken check does not suppress everything after it.
    async with _checks_lock:
        for name, task in (
            ("Expiry reminder check", notify_expiring),
            ("Expired subscription check", close_expired),
            ("Stale order check", close_stale_orders),
            ("Stale review check", flag_stale_reviews),
            ("Monthly report check", monthly_report_if_due),
        ):
            await _run_task(name, task)


async def run_maintenance() -> None:
    # Destructive housekeeping, on the daily cron only. run_checks also runs at startup,
    # and deleting customer documents on every restart is not something a restart should
    # ever do.
    for name, task in (
        ("Event cleanup", prune_events),
        ("Receipt cleanup", prune_receipts),
        ("Orphan receipt check", report_orphan_receipts),
    ):
        await _run_task(name, task)


async def run_hourly() -> None:
    await _run_task("Hourly reminder check", notify_expiring)
    await _run_task("Hourly stale order check", close_stale_orders)
    try:
        # Abandoned flow states expire lazily on read, so nothing frees the ones nobody
        # comes back to.
        dropped = states.sweep()
        if dropped:
            logger.info("Dropped %s expired flow states", dropped)
    except Exception:
        logger.exception("Hourly state cleanup failed")


async def monthly_report() -> int:
    if not _client:
        return 0

    total = await subs_db.count_active()
    money = await stats_db.revenue_for_month(-1)
    lines = [
        "📊 <b>Monthly report</b>",
        "",
        f"Collected: {money['stars']}⭐ and {money['rub']}₽ over {money['orders']} orders",
        (
            f"Reversed during the month: {money['reversed_stars']}⭐ and "
            f"{money['reversed_rub']}₽ over {money['reversed_orders']} orders"
        ),
        f"Net cash movement: {money['net_stars']}⭐ and {money['net_rub']}₽",
        "",
        f"Active subscriptions: {total}",
        "",
    ]
    if not total:
        lines.append("No active subscriptions.")
    return await notify.to_staff(_client, "\n".join(lines))


def _build_triggers(values: dict) -> dict:
    if not 1 <= values["monthly_report_day"] <= 28:
        raise ValueError("monthly_report_day must be between 1 and 28")
    return {
        "daily_checks": CronTrigger(
            hour=values["check_hour"],
            minute=values["check_minute"],
            timezone=dates.TZ,
        ),
        "hourly_checks": IntervalTrigger(hours=1, timezone=dates.TZ),
    }


def start() -> None:
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.warning("Scheduler is already running")
        return

    try:
        values = _cron_settings()
        triggers = _build_triggers(values)
    except (OverflowError, ValueError, TypeError) as error:
        # Fall back safely when a stored schedule value is invalid.
        logger.error("Bad schedule settings (%s), falling back to the defaults", error)
        values = {"check_hour": 10, "check_minute": 0, "monthly_report_day": 1}
        triggers = _build_triggers(values)

    _scheduler = AsyncIOScheduler(
        timezone=dates.TZ,
        job_defaults={
            "misfire_grace_time": MISFIRE_GRACE,
            "coalesce": True,
            "max_instances": 1,
        },
    )
    _scheduler.add_job(
        _run_scheduled,
        triggers["daily_checks"],
        args=(run_checks,),
        id="daily_checks",
        replace_existing=True,
    )
    _scheduler.add_job(
        _run_scheduled,
        triggers["daily_checks"],
        args=(run_maintenance,),
        id="daily_maintenance",
        replace_existing=True,
    )
    _scheduler.add_job(
        _run_scheduled,
        triggers["hourly_checks"],
        args=(run_hourly,),
        id="hourly_checks",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started, daily check at %s:%02d",
        values["check_hour"],
        values["check_minute"],
    )


def reschedule() -> bool:
    # Called after the schedule settings change, so the panel's promise that changes
    # apply immediately is actually true.
    if not _scheduler or not _scheduler.running:
        return False
    try:
        triggers = _build_triggers(_cron_settings())
    except (OverflowError, ValueError, TypeError) as error:
        logger.error("Refusing to reschedule on bad settings: %s", error)
        return False
    _scheduler.reschedule_job("daily_checks", trigger=triggers["daily_checks"])
    _scheduler.reschedule_job("daily_maintenance", trigger=triggers["daily_checks"])
    logger.info("Scheduler rescheduled from the settings panel")
    return True


async def stop() -> None:
    global _scheduler

    current = _scheduler
    if current and current.running:
        # AsyncIOScheduler schedules shutdown onto the event loop and returns at once.
        current.shutdown(wait=False)
        await asyncio.sleep(0)

    pending = list(_active_jobs)
    if pending:
        done, pending = await asyncio.wait(pending, timeout=STOP_GRACE_SECONDS)
        if pending:
            logger.error("Cancelling %s scheduler job(s) after shutdown grace", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
    _scheduler = None
