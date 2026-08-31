# Edge cases: python -m tools.edgecases
#
# The happy paths live in tools.walkthrough. This one pushes on everything that
# should politely refuse: missing usernames, blocked users, other people's data,
# broken invoices, wrong files, role limits, and the scheduled jobs.

import asyncio
import sys
import time
from datetime import timedelta

from tools import _sandbox  # noqa: F401  imported first, it sets DATABASE_PATH
from tools import walkthrough as flow

from config import INVOICE_TTL_MINUTES
from db import connection, invoices as invoices_db, orders as orders_db
from db import products as products_db, requisites as requisites_db
from db import journal, refunds as refunds_db, settings, stats as stats_db
from db import subscriptions as subs_db, users as users_db
from handlers import account, checkout, common
from telethon.tl import types
from services import billing, notify, scheduler, stars
from tools.seed_demo import DEMO_PRODUCTS
from utils import dates

_sandbox.guard_live_database()

OWNER_ID, ALICE_ID, BOB_ID, NONAME_ID, BLOCKED_ID, MANAGER_ID, CAROL_ID = (
    111,
    222,
    333,
    444,
    555,
    666,
    777,
)

problems = []


def _inside_local_day(offset_days: int):
    start, _ = dates.day_bounds_utc(offset_days)
    return dates.parse(start) + timedelta(hours=12)


def check(name, condition, detail=""):
    if not condition:
        problems.append(f"{name}: {detail}")
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}"[:120], flush=True)


class BlockedByCustomer(flow.FakeClient):
    # Sending to this customer always fails, as if the bot was blocked.
    def __init__(self, telegram_id):
        super().__init__()
        self.telegram_id = telegram_id

    async def send_message(self, peer, text=None, buttons=None, parse_mode=None, file=None):
        if peer == self.telegram_id:
            raise RuntimeError("bot was blocked by the user")
        return await super().send_message(peer, text, buttons, parse_mode, file)


async def pay(
    product_id,
    token,
    charge_id,
    telegram_id=ALICE_ID,
    client=None,
    amount=None,
    currency="XTR",
    provider_charge_id=None,
):
    if amount is None:
        invoice = await invoices_db.get(token)
        amount = invoice["amount_stars"] if invoice else 0
    provider_charge_id = provider_charge_id or f"provider-{charge_id}"

    action = types.MessageActionPaymentSentMe(
        currency=currency,
        total_amount=amount,
        payload=f"buy:{product_id}:{token}".encode(),
        charge=types.PaymentCharge(id=charge_id, provider_charge_id=provider_charge_id),
    )
    message = types.MessageService(
        id=1, peer_id=types.PeerUser(user_id=telegram_id), date=None, action=action
    )
    if client is None:
        await flow.deliver_raw(
            types.UpdateNewMessage(message=message, pts=0, pts_count=1),
            label=f"payment {charge_id}",
        )
    else:
        # A few checks need a client that refuses to deliver, which the Raw path does
        # not let us swap out.
        await checkout.handle_payment(client, message, action)


async def start_stars_invoice(sender, product_id):
    await flow.click(sender, f"pay_stars:{product_id}")
    return await flow.click(sender, f"pay_stars_confirm:{await last_token()}")


async def last_token():
    row = await connection.fetch_one("SELECT token FROM invoices ORDER BY id DESC")
    return row["token"]


def refund_requests(since):
    return [
        request
        for request in flow.fake.requests[since:]
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]


def recent_traffic(since):
    return " ".join(str(item) for item in flow.fake.sent[since:])


async def refused_users(alice, blocked, noname):
    event = await flow.send_text(noname, "/start")
    check(
        "a user without a username is refused",
        "username is required" in flow.produced(event).lower(),
    )
    event = await flow.click(noname, "menu:catalog")
    check(
        "a user without a username cannot browse",
        "username is required" in flow.produced(event).lower(),
    )

    event = await flow.send_text(blocked, "/start")
    check(
        "a blocked user is refused",
        "access to this bot is closed" in flow.produced(event).lower(),
    )
    event = await flow.click(blocked, "prod:1")
    check("a blocked user cannot open a product", "access" in flow.produced(event).lower())


async def unknown_ids(owner, alice):
    for data in (
        "prod:9999",
        "pay_stars:9999",
        "pay_stars_confirm:9999:1:1",
        "pay_tr:9999",
        "subdata:9999",
        "ok:9999",
        "problem:9999",
        "cancel_order:9999",
        "a:order:9999",
        "a:sub:9999",
        "a:user:9999",
        "a:p:9999",
        "a:r:9999",
        "a:uoffers:9999",
        "a:roles:9999",
    ):
        actor = owner if data.startswith("a:") else alice
        event = await flow.click(actor, data)
        answer = ((event.answered or "") + flow.produced(event)).lower()
        check(f"{data} answers politely", "not found" in answer, answer[:50])


async def other_peoples_data(alice, bob):
    alice_row = await users_db.get(ALICE_ID)
    await start_stars_invoice(alice, 1)
    await pay(1, await last_token(), "charge-alice")

    subscription = await subs_db.active_for_slug(alice_row["id"], "music")
    await subs_db.set_credentials(subscription["id"], "alice-secret")
    order = await orders_db.get(1)

    event = await flow.click(bob, f"subdata:{subscription['id']}")
    check("another subscription stays private", "alice-secret" not in flow.produced(event))
    for action in ("ok", "problem", "cancel_order"):
        await flow.click(bob, f"{action}:{order['id']}")
        check(
            f"a stranger cannot use {action} on someone else's order",
            (await orders_db.get(order["id"]))["status"] == orders_db.PAID,
        )
    return subscription


async def precheckout(alice):
    # Exercise the pre-checkout gate before Telegram accepts payment.
    async def ask(product_id, token):
        before = len(flow.fake.requests)
        await flow.deliver_raw(
            types.UpdateBotPrecheckoutQuery(
                query_id=1,
                user_id=ALICE_ID,
                payload=f"buy:{product_id}:{token}".encode(),
                currency="XTR",
                total_amount=100,
            ),
            label="precheckout",
        )
        new = flow.fake.requests[before:]
        return new[-1] if new else None

    # Its own product: an open order or a pending invoice elsewhere would silently
    # refuse the click and leave this testing somebody else's token.
    pid = await products_db.create(
        slug="precheckout", name="Gate", price_stars=100, duration_days=30, is_active=1
    )
    await start_stars_invoice(alice, pid)
    good = await ask(pid, await last_token())
    check(
        "a good invoice is answered",
        good is not None and type(good).__name__ == "SetBotPrecheckoutResultsRequest",
    )
    check("and approved", bool(good and good.success), str(good and good.success))

    stale = await last_token()
    await invoices_db.cancel_for_user(ALICE_ID, pid)
    bad = await ask(pid, stale)
    check("a cancelled invoice is refused", bool(bad) and not bad.success)
    check(
        "and the customer is told why",
        bool(bad) and "cancelled" in (bad.error or "").lower(),
        str(bad and bad.error),
    )

    broken = await ask(pid, "no-such-token")
    check("an unknown token is refused", bool(broken) and not broken.success)

    expired_pid = await products_db.create(
        slug="precheckout-expired",
        name="Expired gate",
        price_stars=100,
        duration_days=30,
        is_active=1,
    )
    await start_stars_invoice(alice, expired_pid)
    expired_token = await last_token()
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now', '-2 hours') WHERE token = ?",
        (expired_token,),
    )
    expired = await ask(expired_pid, expired_token)
    check(
        "pre-checkout refuses an invoice outside its payment window",
        bool(expired)
        and not expired.success
        and (await invoices_db.get(expired_token))["precheckout_approved_at"] is None,
        str(expired and expired.error),
    )

    boundary_pid = await products_db.create(
        slug="precheckout-boundary",
        name="Boundary gate",
        price_stars=100,
        duration_days=30,
        is_active=1,
    )
    await start_stars_invoice(alice, boundary_pid)
    boundary_token = await last_token()
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now', ?, '+5 seconds') WHERE token = ?",
        (f"-{INVOICE_TTL_MINUTES} minutes", boundary_token),
    )
    boundary_approval = await ask(boundary_pid, boundary_token)
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now', ?, '-5 seconds') WHERE token = ?",
        (f"-{INVOICE_TTL_MINUTES} minutes", boundary_token),
    )
    requests_before = len(flow.fake.requests)
    await pay(boundary_pid, boundary_token, "charge-after-precheckout-boundary")
    boundary_order = await orders_db.by_charge_id("charge-after-precheckout-boundary")
    check(
        "an approved charge crossing the TTL boundary is fulfilled, not refunded",
        bool(boundary_approval)
        and boundary_approval.success
        and boundary_order is not None
        and (await invoices_db.get(boundary_token))["status"] == invoices_db.PAID
        and not refund_requests(requests_before),
        str(boundary_order),
    )

    # Validation and approval share one write transaction. A catalog edit that arrives
    # at the last boundary must wait, and a charge Telegram accepted under the frozen
    # terms remains fulfilable after that edit commits.
    race_pid = await products_db.create(
        slug="precheckout-atomic",
        name="Atomic approval",
        price_stars=100,
        duration_days=19,
        is_active=1,
    )
    await start_stars_invoice(alice, race_pid)
    race_token = await last_token()
    original_approve = invoices_db.approve_precheckout
    approval_entered = asyncio.Event()
    resume_approval = asyncio.Event()
    approval_task = None
    edit_task = None

    async def paused_approval(token):
        if token == race_token:
            approval_entered.set()
            await resume_approval.wait()
        return await original_approve(token)

    invoices_db.approve_precheckout = paused_approval
    try:
        approval_task = asyncio.create_task(ask(race_pid, race_token))
        await approval_entered.wait()
        edit_task = asyncio.create_task(products_db.update(race_pid, is_active=0))
        await asyncio.sleep(0)
        edit_waited = not edit_task.done()
        resume_approval.set()
        approval, _ = await asyncio.gather(approval_task, edit_task)
    finally:
        resume_approval.set()
        invoices_db.approve_precheckout = original_approve
        pending = [
            task for task in (approval_task, edit_task) if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    requests_before = len(flow.fake.requests)
    await pay(race_pid, race_token, "charge-precheckout-atomic", amount=100)
    atomic_order = await orders_db.by_charge_id("charge-precheckout-atomic")
    check(
        "pre-checkout validation and approval are one authorization decision",
        edit_waited
        and approval.success
        and atomic_order is not None
        and atomic_order["product_name"] == "Atomic approval"
        and not refund_requests(requests_before),
        f"waited={edit_waited} approval={approval and approval.success} order={atomic_order}",
    )


async def broken_invoices(alice, bob):
    token = await last_token()
    since = len(flow.fake.sent)
    orders_before = await connection.fetch_value("SELECT COUNT(*) FROM orders", (), 0)
    # Telegram redelivers unacknowledged updates after a reconnect. The same charge must
    # be recognised as a duplicate, not refunded as fraud on an already paid invoice.
    await pay(1, token, "charge-alice")
    traffic = recent_traffic(since).lower()
    check(
        "a redelivered payment is ignored, not refunded",
        "refund" not in traffic and "already paid" not in traffic,
        traffic[:80],
    )
    check(
        "a redelivered payment creates no second order",
        await connection.fetch_value("SELECT COUNT(*) FROM orders", (), 0) == orders_before,
    )

    since = len(flow.fake.sent)
    await pay(1, token, "charge-repeat")
    check(
        "paying the same invoice twice is refunded",
        "already paid" in recent_traffic(since).lower(),
    )

    await start_stars_invoice(alice, 2)
    cancelled = await last_token()
    await invoices_db.cancel_for_user(ALICE_ID, 2)
    since = len(flow.fake.sent)
    requests_before = len(flow.fake.requests)
    await pay(2, cancelled, "charge-cancelled")
    check("a cancelled invoice is refunded", "cancelled" in recent_traffic(since).lower())
    check(
        "and the stars are actually asked back from Telegram",
        [r.charge_id for r in refund_requests(requests_before)] == ["charge-cancelled"],
        str([type(r).__name__ for r in flow.fake.requests[requests_before:]]),
    )

    stale = "stale-token"
    await invoices_db.create(ALICE_ID, await products_db.get(3), stale)
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now', '-2 hours') WHERE token = ?", (stale,)
    )
    since = len(flow.fake.sent)
    await pay(3, stale, "charge-stale")
    check("an expired invoice is refunded", "payment window" in recent_traffic(since).lower())

    # A Stars invoice belongs to the account it was issued to, forwarding it does not
    # make it payable by somebody else.
    await start_stars_invoice(alice, 2)
    forwarded = await last_token()
    since = len(flow.fake.sent)
    await pay(2, forwarded, "charge-forwarded", telegram_id=BOB_ID)
    check(
        "a forwarded invoice is refused",
        "another account" in recent_traffic(since).lower(),
        recent_traffic(since)[:80],
    )
    # The other direction too: a comparison that only rejects one way is not a guard.
    await start_stars_invoice(bob, 2)
    bobs_token = await last_token()
    since = len(flow.fake.sent)
    await pay(2, bobs_token, "charge-back", telegram_id=ALICE_ID)
    check(
        "and refused the other way round as well",
        "another account" in recent_traffic(since).lower(),
        recent_traffic(since)[:80],
    )

    # Compare the current product price with Telegram's charged amount. Use another
    # owned product so the invoice cooldown does not reuse the previous token.
    cheap_id = await products_db.create(
        slug="underpaid", name="Cheap", price_stars=250, duration_days=30, is_active=1
    )
    await start_stars_invoice(alice, cheap_id)
    mismatched = await last_token()
    since = len(flow.fake.sent)
    await pay(cheap_id, mismatched, "charge-cheap", amount=1)
    check(
        "an underpaid invoice is refunded",
        "amount does not match" in recent_traffic(since).lower(),
        recent_traffic(since)[:80],
    )

    gone = await products_db.create(slug="gone", name="Gone", price_stars=10, is_active=1)
    gone_token = str(int(time.time() * 1000))
    await invoices_db.create(ALICE_ID, await products_db.get(gone), gone_token)
    await products_db.delete(gone)
    since = len(flow.fake.sent)
    await pay(gone, gone_token, "charge-gone")
    check(
        "paying for a deleted product is refunded",
        "no longer available" in recent_traffic(since).lower(),
    )

    # If the refusal journal fails, its surrounding transaction rolls back the first
    # refund row too. The direct fallback must create a fresh durable obligation instead
    # of passing the stale, rolled-back id to the refund worker.
    rollback_product_id = await products_db.create(
        slug="refund-journal-failure",
        name="Refund journal failure",
        price_stars=44,
        duration_days=14,
        is_active=1,
    )
    rollback_product = await products_db.get(rollback_product_id)
    rollback_token = f"refund-journal-failure-{time.time_ns()}"
    await invoices_db.create(ALICE_ID, rollback_product, rollback_token)
    rollback_client = flow.FakeClient()
    original_event = journal.event

    async def fail_refusal_event(event_type, *args, **kwargs):
        if event_type == "payment_refused":
            raise RuntimeError("journal unavailable")
        return await original_event(event_type, *args, **kwargs)

    journal.event = fail_refusal_event
    try:
        await pay(
            rollback_product_id,
            rollback_token,
            "refund-journal-failure",
            client=rollback_client,
            amount=1,
            provider_charge_id="provider-refund-journal-failure",
        )
    finally:
        journal.event = original_event
    rollback_refund = await refunds_db.by_charge(
        "refund-journal-failure", "provider-refund-journal-failure"
    )
    rollback_calls = [
        request
        for request in rollback_client.requests
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    check(
        "a rolled-back refund record falls back to a real durable refund",
        rollback_refund is not None
        and rollback_refund["status"] == refunds_db.COMPLETED
        and (await invoices_db.get(rollback_token))["status"] == invoices_db.CANCELLED
        and [request.charge_id for request in rollback_calls] == ["refund-journal-failure"],
        str(rollback_refund),
    )


async def payment_options(alice):
    event = await flow.click(alice, "pay_tr:2")
    check(
        "transfer is refused without payment details",
        "not available" in (event.answered or "").lower(),
    )

    rub_only = await products_db.create(
        slug="rub-only", name="Transfer only", price_stars=0, price_rub=500, is_active=1
    )
    event = await flow.click(alice, f"pay_stars:{rub_only}")
    check(
        "stars are refused for a transfer only product",
        "transfer only" in (event.answered or "").lower(),
    )
    event = await flow.click(alice, f"prod:{rub_only}")
    check("a transfer only card hides the stars button", "⭐ Pay" not in flow.produced(event))
    return rub_only


async def receipts(alice, bob, rub_only):
    stray = flow.FakeEvent(bob, flow.fake)
    stray.photo = True
    await checkout.handle_receipt(stray)
    check(
        "a receipt without an order is refused clearly",
        "no open order" in flow.produced(stray).lower(),
    )

    method_id = await requisites_db.create("Main", "sbp", "+70000000000", "Bank", "Boss")
    await users_db.set_payment_method((await users_db.get(ALICE_ID))["id"], method_id)
    await flow.click(alice, f"pay_tr:{rub_only}")

    class Archive:
        mime_type = "application/zip"
        size = 100

    wrong = flow.FakeEvent(alice, flow.fake)
    wrong.document = Archive()
    await checkout.handle_receipt(wrong)
    check("a wrong file type is rejected", "photos and pdf" in flow.produced(wrong).lower())

    # The mime type is whatever the client claimed. The bytes decide.
    liar = flow.FakeEvent(alice, flow.fake)
    liar.document = type("Png", (), {"mime_type": "image/png", "size": 1000})()
    liar.media_bytes = b"PK\x03\x04 not an image at all"
    await checkout.handle_receipt(liar)
    check(
        "a file that only claims to be a png is rejected",
        "not a photo or a pdf" in flow.produced(liar).lower(),
        flow.produced(liar)[:60],
    )

    photo_liar = flow.FakeEvent(alice, flow.fake)
    photo_liar.photo = True
    photo_liar.media_bytes = b"GIF89a not a jpeg either"
    await checkout.handle_receipt(photo_liar)
    check(
        "a photo whose bytes are not an image is rejected too",
        "not a photo or a pdf" in flow.produced(photo_liar).lower(),
        flow.produced(photo_liar)[:60],
    )

    class Huge:
        mime_type = "image/jpeg"
        size = 9 * 1024 * 1024

    huge = flow.FakeEvent(alice, flow.fake)
    huge.document = Huge()
    await checkout.handle_receipt(huge)
    check("an oversized file is rejected", "5 mb" in flow.produced(huge).lower())


async def role_limits(owner, manager):
    event = await flow.click(manager, "a:orders")
    check("a manager sees the orders queue", "orders" in flow.produced(event).lower())
    for section in ("a:cat", "a:set", "a:req", "a:offers", "a:cast"):
        event = await flow.click(manager, section)
        check(f"a manager is kept out of {section}", event.answered == "Not enough rights")

    owner_row = await users_db.get(OWNER_ID)
    event = await flow.click(owner, f"a:roles:{owner_row['id']}")
    check("the owner role cannot be changed here", "env" in (event.answered or "").lower())
    event = await flow.click(owner, f"a:block:{owner_row['id']}:1")
    check("the owner cannot be blocked", "cannot block" in (event.answered or "").lower())


async def scheduled_jobs(subscription):
    scheduler.set_client(flow.fake)

    await connection.execute(
        "UPDATE subscriptions SET expires_at = ?, notified_3d = 0, notified_1d = 0 WHERE id = ?",
        (dates.to_sql(_inside_local_day(3)), subscription["id"]),
    )
    since = len(flow.fake.sent)
    await scheduler.notify_expiring()
    check("the early reminder goes out", "in 3 days" in recent_traffic(since).lower())

    await connection.execute(
        "UPDATE subscriptions SET expires_at = ?, notified_1d = 0 WHERE id = ?",
        (dates.to_sql(_inside_local_day(1)), subscription["id"]),
    )
    since = len(flow.fake.sent)
    await scheduler.notify_expiring()
    check("the last day reminder goes out", "tomorrow" in recent_traffic(since).lower())

    await connection.execute(
        "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
        (dates.to_sql(dates.utcnow() - timedelta(minutes=1)), subscription["id"]),
    )
    scheduler.set_client(BlockedByCustomer(ALICE_ID))
    await scheduler.close_expired()
    closed = await subs_db.get(subscription["id"])
    check(
        "a subscription closes even if the bot was blocked", closed["status"] == subs_db.EXPIRED
    )
    await scheduler.close_expired()
    check("a closed subscription is not processed again", not await subs_db.expired())

    scheduler.set_client(flow.fake)
    stale_user = await users_db.get_or_create(880019, "stale_transfer", "Stale transfer")
    stale = await orders_db.create(
        user_id=stale_user["id"],
        product=await products_db.get(1),
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.PENDING_RECEIPT,
    )
    await connection.execute(
        "UPDATE orders SET created_at = datetime('now', '-3 hours') WHERE id = ?", (stale,)
    )
    await scheduler.close_stale_orders()
    check(
        "a transfer order is not closed on the stars deadline",
        (await orders_db.get(stale))["status"] == orders_db.PENDING_RECEIPT,
    )
    await connection.execute(
        "UPDATE orders SET created_at = datetime('now', '-99 hours') WHERE id = ?", (stale,)
    )
    await scheduler.close_stale_orders()
    check(
        "an abandoned transfer order is closed",
        (await orders_db.get(stale))["status"] == orders_db.PAYMENT_EXPIRED,
    )

    late_telegram_id = 880015
    late_sender = flow.Sender(late_telegram_id, "late_receipt", "Late receipt")
    late_user = await users_db.get_or_create(late_telegram_id, "late_receipt", "Late receipt")
    late_order = await orders_db.create(
        user_id=late_user["id"],
        product=await products_db.get(1),
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.PAYMENT_EXPIRED,
    )
    late_event = flow.FakeEvent(late_sender, flow.fake)
    late_event.photo = True
    await checkout.handle_receipt(late_event)
    late_record = await orders_db.get(late_order)
    check(
        "a single recent transfer timeout accepts a late receipt",
        late_record["status"] == orders_db.PENDING_REVIEW
        and bool(late_record["receipt_file"])
        and "receipt received" in flow.produced(late_event).lower(),
        str(late_record),
    )

    await scheduler.monthly_report()
    check("the monthly report is produced", "monthly report" in str(flow.fake.sent[-1]).lower())


async def staff_customer_locks(owner, manager):
    """Staff accounts can buy and expire without acquiring each other's locks in a cycle."""

    owner_user = await users_db.get(OWNER_ID)
    manager_user = await users_db.get(MANAGER_ID)

    async def fulfilled_order(user, slug):
        product_id = await products_db.create(
            slug=slug,
            name=slug.replace("-", " ").title(),
            price_stars=25,
            duration_days=4,
            is_active=1,
        )
        product = await products_db.get(product_id)
        order_id = await orders_db.create(
            user_id=user["id"],
            product=product,
            amount_stars=25,
            payment_method="stars",
            status=orders_db.PAID,
            payment_charge_id=f"charge-{slug}",
            payment_recipient_id=user["telegram_id"],
        )
        result = await billing.apply_payment(await orders_db.get(order_id))
        await orders_db.set_status(order_id, orders_db.DELIVERED)
        return order_id, result["subscription"]["id"]

    owner_order, owner_subscription = await fulfilled_order(owner_user, "owner-staff-lock")
    manager_order, manager_subscription = await fulfilled_order(
        manager_user, "manager-staff-lock"
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(
                flow.click(owner, f"ok:{owner_order}"),
                flow.click(manager, f"ok:{manager_order}"),
            ),
            timeout=2,
        )
        confirmations_finished = True
    except asyncio.TimeoutError:
        confirmations_finished = False
    check(
        "two staff customers can confirm concurrently without a lifecycle deadlock",
        confirmations_finished
        and (await orders_db.get(owner_order))["status"] == orders_db.COMPLETED
        and (await orders_db.get(manager_order))["status"] == orders_db.COMPLETED,
    )

    await connection.execute(
        "UPDATE subscriptions SET expires_at = ? WHERE id IN (?, ?)",
        (
            dates.to_sql(dates.utcnow() - timedelta(minutes=1)),
            owner_subscription,
            manager_subscription,
        ),
    )
    scheduler.set_client(flow.fake)
    try:
        await asyncio.wait_for(scheduler.close_expired(), timeout=2)
        expiry_finished = True
    except asyncio.TimeoutError:
        expiry_finished = False
    check(
        "staff subscriptions expire without reacquiring their own lifecycle locks",
        expiry_finished
        and (await subs_db.get(owner_subscription))["status"] == subs_db.EXPIRED
        and (await subs_db.get(manager_subscription))["status"] == subs_db.EXPIRED,
    )


async def expiry_credentials_race():
    # A credential write pauses while it owns the billing lock. The expiry sweep must
    # try to enter that same lock instead of closing the subscription underneath it.
    scheduler.set_client(flow.fake)
    user = await users_db.get(ALICE_ID)
    product_id = await products_db.create(
        slug="expiry-lock-race",
        name="Expiry lock race",
        price_stars=10,
        duration_days=30,
        is_active=1,
    )
    product = await products_db.get(product_id)
    subscription_id = await subs_db.create(
        user["id"], product, dates.utcnow() - timedelta(minutes=1), False
    )

    class ProbeLock:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.attempts = 0
            self.second_attempt = asyncio.Event()

        async def __aenter__(self):
            self.attempts += 1
            if self.attempts == 2:
                self.second_attempt.set()
            await self.lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            self.lock.release()

    probe = ProbeLock()
    setter_entered = asyncio.Event()
    release_setter = asyncio.Event()
    original_lock = billing._subscription_lock
    original_set_credentials = subs_db.set_credentials
    credentials_task = None
    sweep_task = None
    second_attempt_task = None

    async def paused_set_credentials(current_id, credentials):
        setter_entered.set()
        await release_setter.wait()
        return await original_set_credentials(current_id, credentials)

    billing._subscription_lock = probe
    subs_db.set_credentials = paused_set_credentials
    try:
        credentials_task = asyncio.create_task(
            billing.set_credentials(subscription_id, "race-secret")
        )
        await setter_entered.wait()
        sweep_task = asyncio.create_task(scheduler.close_expired())
        second_attempt_task = asyncio.create_task(probe.second_attempt.wait())
        done, _ = await asyncio.wait(
            {sweep_task, second_attempt_task}, return_when=asyncio.FIRST_COMPLETED
        )
        check(
            "expiry and credential writes use the same billing lock",
            second_attempt_task in done and sweep_task not in done,
            f"lock attempts={probe.attempts} sweep done={sweep_task.done()}",
        )
        release_setter.set()
        await asyncio.gather(credentials_task, sweep_task)
    finally:
        release_setter.set()
        subs_db.set_credentials = original_set_credentials
        billing._subscription_lock = original_lock
        pending = [
            task
            for task in (credentials_task, sweep_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if second_attempt_task is not None and not second_attempt_task.done():
            second_attempt_task.cancel()
            await asyncio.gather(second_attempt_task, return_exceptions=True)

    final = await subs_db.get(subscription_id)
    check(
        "expiry wins without leaving credentials on closed access",
        final["status"] == subs_db.EXPIRED and final["credentials"] is None,
        f"status={final['status']} credentials={final['credentials']!r}",
    )


async def deleted_product_with_a_live_subscription(alice):
    product = await products_db.get_by_slug("cloud-storage")
    order_id = await orders_db.create(
        user_id=(await users_db.get(ALICE_ID))["id"],
        product=product,
        amount_stars=10,
        payment_method="stars",
        status=orders_db.PAID,
    )
    result = await billing.apply_payment(await orders_db.get(order_id))
    await products_db.delete(product["id"])

    event = await flow.click(alice, "menu:subs")
    check(
        "the subscription list survives a deleted product",
        "cloud storage" in flow.produced(event).lower(),
    )
    check(
        "the renew button disappears together with the product",
        f"prod:{product['id']}" not in str(event.replies),
    )
    await subs_db.set_status(result["subscription"]["id"], subs_db.CANCELLED)


async def concurrency_and_limits(owner):
    carol = await users_db.get_or_create(CAROL_ID, "carol", "Carol")
    product = await products_db.get_by_slug("secure-net")

    # Force both deliveries past invoice validation before either can enter the
    # serialized order decision. The loser must see the winner's charge under the
    # lock, not misclassify the winner's open order and refund the shared charge.
    alice = await users_db.get(ALICE_ID)
    race_product_id = await products_db.create(
        slug="same-charge-race",
        name="Same charge race",
        price_stars=73,
        duration_days=23,
        is_active=1,
    )
    race_product = await products_db.get(race_product_id)
    race_invoice_ref = "same-charge-race-token"
    await invoices_db.create(ALICE_ID, race_product, race_invoice_ref)
    original_validate = checkout._validate_invoice
    both_validated = asyncio.Event()
    validated = 0

    async def validate_together(*args, **kwargs):
        nonlocal validated
        result = await original_validate(*args, **kwargs)
        validated += 1
        if validated == 2:
            both_validated.set()
        await both_validated.wait()
        return result

    checkout._validate_invoice = validate_together
    requests_before = len(flow.fake.requests)
    try:
        await asyncio.gather(
            pay(
                race_product_id,
                race_invoice_ref,
                "same-charge-race",
                provider_charge_id="provider-same-charge-race",
            ),
            pay(
                race_product_id,
                race_invoice_ref,
                "same-charge-race",
                provider_charge_id="provider-same-charge-race",
            ),
        )
    finally:
        checkout._validate_invoice = original_validate
    race_orders = await connection.fetch_all(
        "SELECT * FROM orders WHERE payment_charge_id = ?", ("same-charge-race",)
    )
    race_subscriptions = await connection.fetch_all(
        "SELECT * FROM subscriptions WHERE user_id = ? AND product_slug = ?",
        (alice["id"], "same-charge-race"),
    )
    race_refunds = [
        request
        for request in refund_requests(requests_before)
        if request.charge_id == "same-charge-race"
    ]
    check(
        "concurrent redelivery of one charge creates one order",
        len(race_orders) == 1,
        f"orders={len(race_orders)}",
    )
    check(
        "concurrent redelivery grants one snapshot period",
        len(race_subscriptions) == 1
        and 22
        <= (dates.parse(race_subscriptions[0]["expires_at"]) - dates.utcnow()).days
        <= 23,
        f"subscriptions={len(race_subscriptions)}",
    )
    check(
        "concurrent redelivery never refunds the winning charge",
        not race_refunds,
        f"refunds={len(race_refunds)}",
    )

    # Invoice validation may finish just before an owner erases the customer. The
    # final database decision must notice that change, avoid creating an orphaned paid
    # order and refund the Telegram account from the incoming payment update.
    erase_telegram_id = 880004
    erase_user = await users_db.get_or_create(
        erase_telegram_id, "payment_erase_race", "Payment erase race"
    )
    erase_product_id = await products_db.create(
        slug="payment-erase-race",
        name="Payment erase race",
        price_stars=67,
        duration_days=17,
        is_active=1,
    )
    erase_product = await products_db.get(erase_product_id)
    erase_invoice_ref = "payment-erase-race-token"
    await invoices_db.create(erase_telegram_id, erase_product, erase_invoice_ref)
    validated_event = asyncio.Event()
    resume_payment = asyncio.Event()

    async def pause_after_validation(*args, **kwargs):
        result = await original_validate(*args, **kwargs)
        if args[1] == erase_invoice_ref:
            validated_event.set()
            await resume_payment.wait()
        return result

    erase_client = flow.FakeClient()
    checkout._validate_invoice = pause_after_validation
    payment_task = asyncio.create_task(
        pay(
            erase_product_id,
            erase_invoice_ref,
            "payment-erase-race",
            telegram_id=erase_telegram_id,
            client=erase_client,
            provider_charge_id="provider-payment-erase-race",
        )
    )
    try:
        await validated_event.wait()
        await users_db.erase(erase_user["id"])
        resume_payment.set()
        await payment_task
    finally:
        resume_payment.set()
        checkout._validate_invoice = original_validate
        if not payment_task.done():
            await payment_task

    erase_refund = await refunds_db.by_charge(
        "payment-erase-race", "provider-payment-erase-race"
    )
    erase_refund_calls = [
        request
        for request in erase_client.requests
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    check(
        "payment after account erasure creates no order or access",
        await orders_db.by_charge_id("payment-erase-race") is None
        and not await subs_db.active_for_slug(erase_user["id"], "payment-erase-race"),
    )
    check(
        "payment after account erasure refunds the original Telegram account",
        erase_refund is not None
        and erase_refund["telegram_id"] == 0
        and erase_refund["status"] == refunds_db.COMPLETED
        and [request.charge_id for request in erase_refund_calls] == ["payment-erase-race"]
        and erase_refund_calls[0].user_id == erase_telegram_id
        and not any(peer == erase_telegram_id for peer, _ in erase_client.sent),
        str(erase_refund),
    )

    # Two payments landing at the same moment must not split into two subscriptions.
    orders = [
        await orders_db.create(
            user_id=carol["id"],
            product=product,
            amount_stars=150,
            payment_method="stars",
            status=orders_db.PAID,
        )
        for _ in range(2)
    ]
    loaded = [await orders_db.get(order_id) for order_id in orders]
    await asyncio.gather(*(billing.apply_payment(item) for item in loaded))
    rows = await connection.fetch_all(
        "SELECT * FROM subscriptions WHERE user_id = ? AND product_slug = ?",
        (carol["id"], product["slug"]),
    )
    check(
        "two simultaneous payments make one subscription", len(rows) == 1, f"found {len(rows)}"
    )
    if len(rows) == 1:
        days = (dates.parse(rows[0]["expires_at"]) - dates.utcnow()).days
        check("both payments are counted", 59 <= days <= 60, f"days={days}")

    # The same order applied twice must add one period only.
    again = await orders_db.create(
        user_id=carol["id"],
        product=product,
        amount_stars=150,
        payment_method="stars",
        status=orders_db.PAID,
    )
    order = await orders_db.get(again)
    before = (await subs_db.active_for_slug(carol["id"], product["slug"]))["expires_at"]
    await asyncio.gather(billing.apply_payment(order), billing.apply_payment(order))
    after = (await subs_db.active_for_slug(carol["id"], product["slug"]))["expires_at"]
    check(
        "one order adds one period",
        (dates.parse(after) - dates.parse(before)).days == 30,
        f"{before} -> {after}",
    )

    # Two admins confirming the same transfer at the same moment.
    music = await products_db.get_by_slug("music")
    transfer = await orders_db.create(
        user_id=carol["id"],
        product=music,
        amount_rub=300,
        payment_method="transfer",
        status=orders_db.PENDING_REVIEW,
    )
    await asyncio.gather(
        flow.click(owner, f"a:confirm:{transfer}", label="first admin confirms"),
        flow.click(owner, f"a:confirm:{transfer}", label="second admin confirms"),
    )
    rows = await connection.fetch_all(
        "SELECT * FROM subscriptions WHERE user_id = ? AND product_slug = 'music'",
        (carol["id"],),
    )
    check("a double confirmation makes one subscription", len(rows) == 1, f"found {len(rows)}")

    # Numbers that would poison an invoice or an expiry date.
    for field, value in (
        ("price_stars", "-100"),
        ("price_stars", "99999999"),
        ("duration_days", "100000"),
    ):
        stored_before = (await products_db.get(music["id"]))[field]
        await flow.click(owner, f"a:pe:{music['id']}:{field}")
        await flow.send_text(owner, value, label=f"{field} = {value}")
        check(
            f"{field} rejects {value}",
            (await products_db.get(music["id"]))[field] == stored_before,
        )

    subscription = await subs_db.active_for_slug(carol["id"], "music")
    await flow.click(owner, f"a:subdays:{subscription['id']}")
    event = await flow.send_text(owner, "99999999", label="a century of extra days")
    check("an absurd gift of days is refused", "days added" not in flow.produced(event).lower())

    # A pending question is lost on restart; the answer must not land somewhere else.
    await flow.click(owner, f"a:pe:{music['id']}:price_stars")
    flow.clear_state(owner)
    price_before = (await products_db.get(music["id"]))["price_stars"]
    await flow.send_text(owner, "555", label="answer after a restart")
    check(
        "an answer without a pending question is ignored",
        (await products_db.get(music["id"]))["price_stars"] == price_before,
    )

    try:
        await connection.execute(
            "INSERT INTO subscriptions (user_id, product_slug, product_name, expires_at) "
            "VALUES (99999, 'x', 'X', datetime('now'))"
        )
        check("a subscription cannot point at a missing user", False, "the insert went through")
    except Exception as error:
        check("a subscription cannot point at a missing user", True, type(error).__name__)


async def refund_recovery(owner):
    # An automatic refusal is durable before its first Telegram attempt and can be
    # retried after a process restart without losing either charge reference.
    automatic_product_id = await products_db.create(
        slug="automatic-refund-recovery",
        name="Automatic refund recovery",
        price_stars=41,
        duration_days=11,
        is_active=1,
    )
    automatic_product = await products_db.get(automatic_product_id)
    automatic_invoice_ref = "automatic-refund-recovery-token"
    await invoices_db.create(ALICE_ID, automatic_product, automatic_invoice_ref)
    await invoices_db.cancel(automatic_invoice_ref)
    failed_client = flow.FakeClient()
    failed_client.fail_refunds = 1
    await pay(
        automatic_product_id,
        automatic_invoice_ref,
        "automatic-refund-recovery",
        client=failed_client,
        provider_charge_id="provider-automatic-refund-recovery",
    )
    automatic = await refunds_db.by_charge(
        "automatic-refund-recovery", "provider-automatic-refund-recovery"
    )
    check(
        "a failed automatic refund remains durable and retryable",
        automatic is not None
        and automatic["order_id"] is None
        and automatic["source"] == "automatic"
        and automatic["status"] == refunds_db.PENDING
        and automatic["attempts"] == 1
        and automatic["last_error"],
        str(automatic),
    )
    check(
        "the automatic refund preserves both charge ids",
        automatic["telegram_charge_id"] == "automatic-refund-recovery"
        and automatic["provider_charge_id"] == "provider-automatic-refund-recovery",
    )
    await connection.disconnect()
    await connection.connect()
    automatic = await refunds_db.get(automatic["id"])
    queue = await flow.click(owner, "a:refunds")
    check(
        "the refund queue survives a database restart",
        automatic is not None
        and automatic["status"] == refunds_db.PENDING
        and f"#{automatic['id']}" in flow.produced(queue),
        flow.produced(queue)[:100],
    )
    before_retry = len(failed_client.requests)
    await flow.click(
        owner,
        f"a:refundretry:{automatic['id']}",
        event_client=failed_client,
    )
    retried = await refunds_db.get(automatic["id"])
    retry_calls = [
        request
        for request in failed_client.requests[before_retry:]
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    check(
        "retry uses the Telegram charge and completes the automatic refund",
        retried["status"] == refunds_db.COMPLETED
        and retried["resolution"] == refunds_db.TELEGRAM
        and [request.charge_id for request in retry_calls] == ["automatic-refund-recovery"],
        str(retried),
    )
    before_stale = len(failed_client.requests)
    await flow.click(
        owner,
        f"a:refundretry:{automatic['id']}",
        event_client=failed_client,
    )
    check(
        "a stale automatic retry sends no second refund RPC",
        len(failed_client.requests) == before_stale,
    )

    # An admin refund stays financially pending after a failed RPC. One of two
    # concurrent retries performs the Telegram call and finalization is idempotent.
    admin_product_id = await products_db.create(
        slug="admin-refund-recovery",
        name="Admin refund recovery",
        price_stars=57,
        duration_days=19,
        is_active=1,
    )
    admin_product = await products_db.get(admin_product_id)
    admin_invoice_ref = "admin-refund-recovery-token"
    await invoices_db.create(ALICE_ID, admin_product, admin_invoice_ref)
    await pay(
        admin_product_id,
        admin_invoice_ref,
        "admin-refund-recovery",
        provider_charge_id="provider-admin-refund-recovery",
    )
    admin_order = await orders_db.by_charge_id("admin-refund-recovery")
    admin_subscription = await subs_db.get(admin_order["subscription_id"])
    admin_client = flow.FakeClient()
    admin_client.fail_refunds = 1
    order_before_old_button = await orders_db.get(admin_order["id"])
    requests_before_old_button = len(admin_client.requests)
    old_button = await flow.click(
        owner,
        f"a:refund:{admin_order['id']}",
        event_client=admin_client,
    )
    check(
        "a refund button from an old message opens the new confirmation",
        (await orders_db.get(admin_order["id"]))["status"] == order_before_old_button["status"]
        and await refunds_db.for_order(admin_order["id"]) is None
        and len(admin_client.requests) == requests_before_old_button
        and "refund order" in flow.produced(old_button).lower()
        and old_button.keyboards[-1][0][0].data.decode() == f"a:refundgo:{admin_order['id']}",
        flow.produced(old_button)[:100],
    )
    await flow.click(owner, f"a:refundask:{admin_order['id']}")
    await flow.click(
        owner,
        f"a:refundgo:{admin_order['id']}",
        event_client=admin_client,
    )
    pending_order = await orders_db.get(admin_order["id"])
    admin_refund = await refunds_db.for_order(admin_order["id"])
    check(
        "a failed admin refund keeps revenue and access pending",
        pending_order["status"] == orders_db.REFUND_PENDING
        and pending_order["reversed_at"] is None
        and (await subs_db.get(admin_subscription["id"]))["status"] == subs_db.ACTIVE
        and admin_refund["status"] == refunds_db.PENDING,
        f"order={pending_order['status']} refund={admin_refund['status']}",
    )
    before_admin_retry = len(admin_client.requests)
    await asyncio.gather(
        flow.click(
            owner,
            f"a:refundretry:{admin_refund['id']}",
            label="first refund retry",
            event_client=admin_client,
        ),
        flow.click(
            owner,
            f"a:refundretry:{admin_refund['id']}",
            label="second refund retry",
            event_client=admin_client,
        ),
    )
    admin_retry_calls = [
        request
        for request in admin_client.requests[before_admin_retry:]
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    final_order = await orders_db.get(admin_order["id"])
    final_refund = await refunds_db.get(admin_refund["id"])
    final_subscription = await subs_db.get(admin_subscription["id"])
    check(
        "concurrent admin retries issue one Telegram refund",
        [request.charge_id for request in admin_retry_calls] == ["admin-refund-recovery"],
        str([request.charge_id for request in admin_retry_calls]),
    )

    # Older orders do not have the provider reference. PaymentCharge.id alone is
    # sufficient for Telegram's refund request and must remain recoverable.
    legacy_client = flow.FakeClient()
    legacy = await refunds_db.create(
        telegram_id=ALICE_ID,
        source="automatic",
        reason="Legacy Telegram charge",
        telegram_charge_id="telegram-only-refund",
        amount_stars=29,
    )
    legacy_returned = await stars.process_refund(legacy_client, legacy["id"])
    legacy_calls = [
        request
        for request in legacy_client.requests
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    check(
        "a legacy Telegram-only charge remains refundable",
        legacy_returned
        and (await refunds_db.get(legacy["id"]))["status"] == refunds_db.COMPLETED
        and [request.charge_id for request in legacy_calls] == ["telegram-only-refund"],
    )
    check(
        "a confirmed admin refund finalizes order and access once",
        final_refund["status"] == refunds_db.COMPLETED
        and final_order["status"] == orders_db.REFUNDED
        and final_order["reversed_at"] is not None
        and final_subscription["status"] != subs_db.ACTIVE,
        f"order={final_order['status']} sub={final_subscription['status']}",
    )
    expiry_after = final_subscription["expires_at"]
    before_final_retry = len(admin_client.requests)
    await flow.click(
        owner,
        f"a:refundretry:{admin_refund['id']}",
        event_client=admin_client,
    )
    check(
        "a completed admin refund is harmless when retried again",
        len(admin_client.requests) == before_final_retry
        and (await subs_db.get(admin_subscription["id"]))["expires_at"] == expiry_after,
    )


async def closing_out_orders(owner, alice, rub_only):
    # The reject / refund / complete half of the panel was never clicked by any check.
    method = (await requisites_db.list_all(active_only=True))[0]
    user = await users_db.get(ALICE_ID)
    await users_db.set_payment_method(user["id"], method["id"])
    slug = (await products_db.get(rub_only))["slug"]

    async def transfer_with_receipt():
        await flow.click(alice, f"pay_tr:{rub_only}")
        order = await orders_db.oldest_awaiting_receipt(user["id"])
        receipt = flow.FakeEvent(alice, flow.fake)
        receipt.photo = True
        await checkout.handle_receipt(receipt)
        stored = await orders_db.get(order["id"])
        check(
            f"order #{order['id']} records the receipt it went under review with",
            stored["status"] == orders_db.PENDING_REVIEW and stored["receipt_file"],
            f"status={stored['status']} file={stored['receipt_file']}",
        )
        return order["id"]

    # Confirmed, then someone taps Reject on a stale card.
    first = await transfer_with_receipt()
    await flow.click(owner, f"a:confirm:{first}")
    granted = await subs_db.active_for_slug(user["id"], slug)
    check("confirming a transfer grants the subscription", granted is not None)
    await flow.click(owner, f"a:reject:{first}")
    check(
        "a stale Reject on a handled order is refused",
        (await orders_db.get(first))["status"] == orders_db.PAID,
        (await orders_db.get(first))["status"],
    )
    check(
        "the subscription survives the stale Reject",
        (await subs_db.get(granted["id"]))["status"] == subs_db.ACTIVE,
    )

    # Sending access details, then completing.
    await flow.click(owner, f"a:send:{first}")
    await flow.send_text(owner, "login:pass", label="issue access details")
    check(
        "sending access details delivers the order",
        (await orders_db.get(first))["status"] == orders_db.DELIVERED,
    )
    await flow.click(owner, f"a:done:{first}")
    check(
        "completing closes the order",
        (await orders_db.get(first))["status"] == orders_db.COMPLETED,
    )

    revenue_before = (await stats_db.dashboard())["revenue_rub"]
    await flow.click(owner, f"a:refundask:{first}")
    await flow.click(owner, f"a:refundgo:{first}")
    pending_refund_order = await orders_db.get(first)
    manual_refund = await refunds_db.for_order(first)
    check(
        "requesting a transfer refund leaves it pending",
        pending_refund_order["status"] == orders_db.REFUND_PENDING
        and manual_refund["status"] == refunds_db.PENDING,
    )
    check(
        "a pending manual refund keeps revenue and access intact",
        (await stats_db.dashboard())["revenue_rub"] == revenue_before
        and (await subs_db.get(granted["id"]))["status"] == subs_db.ACTIVE,
    )
    check(
        "the refund obligation is written down before completion",
        any(
            row["action"] == "order_refund" and row["details"].startswith("PENDING")
            for row in await journal.recent_actions(limit=20)
        ),
    )
    requests_before_done = len(flow.fake.requests)
    await flow.click(owner, f"a:refunddone:{manual_refund['id']}")
    refunded = await orders_db.get(first)
    check(
        "confirming the transfer refund closes the order",
        refunded["status"] == orders_db.REFUNDED,
    )
    check(
        "a completed refund takes the money back out of the books",
        (await stats_db.dashboard())["revenue_rub"] < revenue_before,
    )
    check(
        "refunding closes the subscription",
        (await subs_db.get(granted["id"]))["status"] != subs_db.ACTIVE,
    )
    expiry_after_done = (await subs_db.get(granted["id"]))["expires_at"]
    await flow.click(owner, f"a:refunddone:{manual_refund['id']}")
    check(
        "manual refund completion is idempotent and sends no Stars RPC",
        len(flow.fake.requests) == requests_before_done
        and (await subs_db.get(granted["id"]))["expires_at"] == expiry_after_done,
    )
    check(
        "a refunded order cannot be completed back into revenue",
        not await orders_db.claim_status(
            first, orders_db.COMPLETED, (orders_db.PAID, orders_db.DELIVERED, orders_db.PROBLEM)
        ),
    )

    # A paid problem cannot be disguised as a rejection: it requires a real refund.
    third = await transfer_with_receipt()
    await flow.click(owner, f"a:confirm:{third}")
    third_sub = await subs_db.active_for_slug(user["id"], slug)
    check("the problem order granted access first", third_sub is not None)
    await flow.click(owner, f"a:send:{third}")
    await flow.send_text(owner, "third-login:pass", label="issue third access details")
    await flow.click(alice, f"problem:{third}")
    await flow.click(owner, f"a:reject:{third}")
    check(
        "a paid problem cannot be rejected without a refund",
        (await orders_db.get(third))["status"] == orders_db.PROBLEM
        and (await subs_db.get(third_sub["id"]))["status"] == subs_db.ACTIVE,
        (await orders_db.get(third))["status"],
    )
    await flow.click(owner, f"a:refundask:{third}")
    await flow.click(owner, f"a:refundgo:{third}")
    third_refund = await refunds_db.for_order(third)
    await flow.click(owner, f"a:refunddone:{third_refund['id']}")

    # A genuine rejection of an unconfirmed receipt.
    second = await transfer_with_receipt()
    await flow.click(owner, f"a:reject:{second}")
    check(
        "an unreviewed receipt can be rejected",
        (await orders_db.get(second))["status"] == orders_db.REJECTED,
    )
    check(
        "a rejected order stops blocking the product",
        await orders_db.open_for_product(user["id"], slug) is None,
    )


async def stale_access(alice):
    user = await users_db.get(ALICE_ID)
    product = await products_db.get_by_slug("music")
    sub_id = await subs_db.create(
        user["id"], product, dates.utcnow() + timedelta(days=5), False
    )
    await subs_db.set_credentials(sub_id, "top-secret-login")
    event = await flow.click(alice, f"subdata:{sub_id}")
    check(
        "an active subscription shows its access details",
        "top-secret-login" in flow.produced(event),
    )

    # Status only: closing through close() would also blank the column, and then the
    # handler's own guard would never be exercised.
    await connection.execute(
        "UPDATE subscriptions SET status = ? WHERE id = ?", (subs_db.EXPIRED, sub_id)
    )
    event = await flow.click(alice, f"subdata:{sub_id}")
    check(
        "an expired subscription no longer shows them",
        "top-secret-login" not in flow.produced(event),
        flow.produced(event)[:60],
    )

    # Same again for a subscription that is still marked active but ran out.
    await connection.execute(
        "UPDATE subscriptions SET status = ?, expires_at = ? WHERE id = ?",
        (subs_db.ACTIVE, dates.to_sql(dates.utcnow() - timedelta(days=1)), sub_id),
    )
    event = await flow.click(alice, f"subdata:{sub_id}")
    check(
        "a subscription past its date no longer shows them either",
        "top-secret-login" not in flow.produced(event),
        flow.produced(event)[:60],
    )


async def blocked_uploads(blocked):
    user = await users_db.get_or_create(BLOCKED_ID, "blocked", "B")
    product = await products_db.get_by_slug("music")
    await orders_db.create(
        user_id=user["id"],
        product=product,
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.PENDING_RECEIPT,
    )
    await users_db.set_blocked(user["id"], True)
    event = flow.FakeEvent(blocked, flow.fake)
    event.photo = True
    await checkout.handle_receipt(event)
    check("a blocked customer cannot push a receipt to staff", not event.replies)


async def flow_state_rules(owner):
    from utils import states
    from handlers.admin import base as admin_base

    # Rights are rechecked when the value arrives, not only when the flow is opened.
    before = settings.get("check_hour")
    await flow.click(owner, "a:sete:check_hour")
    owner_row = await users_db.get(OWNER_ID)
    await connection.execute(
        "UPDATE users SET role = 'manager' WHERE id = ?", (owner_row["id"],)
    )
    event = await flow.send_text(owner, "7", label="answer after being demoted")
    check(
        "a demoted admin cannot finish an open flow",
        settings.get("check_hour") == before,
        f"{before} -> {settings.get('check_hour')}",
    )
    check("and is told why", "rights" in flow.produced(event).lower())
    await connection.execute("UPDATE users SET role = 'owner' WHERE id = ?", (owner_row["id"],))

    # A message the admin sends somewhere else entirely must not become the answer.
    await flow.click(owner, "a:sete:check_hour")
    group = flow.FakeEvent(owner, flow.fake, text="7")
    group.chat_id = -100500
    group.is_private = False
    for callback, builder in flow.MESSAGE_HANDLERS:
        try:
            allowed = builder.func(group) if builder.func else True
        except Exception:
            allowed = False
        if allowed and (builder.pattern is None or builder.pattern("7")):
            await callback(group)
    check(
        "a message in another chat does not answer a private prompt",
        settings.get("check_hour") == before and not group.replies,
        f"{before} -> {settings.get('check_hour')} replies={group.replies}",
    )
    await flow.send_text(owner, "cancel", label="close the prompt")

    # Out of range values never reach the scheduler.
    await flow.click(owner, "a:sete:check_hour")
    await flow.send_text(owner, "24", label="an hour that does not exist")
    check("an impossible hour is refused", settings.get("check_hour") == before)

    # An abandoned prompt expires instead of waiting forever.
    await flow.click(owner, "a:sete:check_hour")
    original_ttl = states.TTL_SECONDS
    states.TTL_SECONDS = 0
    try:
        await flow.send_text(owner, "7", label="answer to an expired prompt")
    finally:
        states.TTL_SECONDS = original_ttl
    check("an expired prompt ignores the answer", settings.get("check_hour") == before)

    # A Telegram id can register again after privacy erasure. The prompt must stay
    # attached to the old internal profile instead of becoming an admin action for the
    # new account that happens to have the same Telegram id.
    reused_telegram_id = 880021
    old_admin = await users_db.get_or_create(
        reused_telegram_id, "old_prompt_admin", "Old prompt admin"
    )
    await users_db.set_role(old_admin["id"], "owner")
    old_sender = flow.Sender(reused_telegram_id, "old_prompt_admin", "Old prompt admin")
    old_event = flow.FakeEvent(old_sender, flow.fake, data="a:sete:check_hour")
    assert await admin_base.actor(old_event, "settings")
    await admin_base.ask(
        old_event,
        "setting_value",
        "settings",
        "Choose an hour",
        key="check_hour",
    )
    await users_db.erase(old_admin["id"])
    new_admin = await users_db.get_or_create(
        reused_telegram_id, "new_prompt_admin", "New prompt admin"
    )
    await users_db.set_role(new_admin["id"], "owner")
    new_sender = flow.Sender(reused_telegram_id, "new_prompt_admin", "New prompt admin")
    event = await flow.send_text(new_sender, "7", label="answer to a previous profile's prompt")
    check(
        "an erased profile's prompt cannot act as a re-registered admin",
        settings.get("check_hour") == before,
        f"{before} -> {settings.get('check_hour')}",
    )
    check(
        "and the replacement profile sees a stale-prompt notice",
        "no longer active" in flow.produced(event).lower(),
    )

    # Free text fields are bounded, like the numeric ones.
    music = await products_db.get_by_slug("music")
    await flow.click(owner, f"a:pe:{music['id']}:name")
    await flow.send_text(owner, "N" * 5000, label="an absurd product name")
    check(
        "an absurd product name is refused",
        (await products_db.get(music["id"]))["name"] != "N" * 5000,
    )


async def clock_and_reminders(alice):
    # Its own client: inheriting one from an earlier section made every assertion here
    # pass vacuously when the sections were reordered.
    scheduler.set_client(flow.fake)
    user = await users_db.get(ALICE_ID)
    # A product of its own, so nothing earlier in the run interferes with it.
    product_id = await products_db.create(
        slug="reminder-probe", name="Probe", price_stars=10, duration_days=30, is_active=1
    )
    product = await products_db.get(product_id)
    sub_id = await subs_db.create(user["id"], product, _inside_local_day(1), False)
    await subs_db.set_credentials(sub_id, "keep-me")

    since = len(flow.fake.sent)
    await scheduler.notify_expiring()
    first_run = len(flow.fake.sent) - since
    await scheduler.notify_expiring()
    check(
        "a reminder is sent once, however often the job runs",
        len(flow.fake.sent) - since == first_run and first_run >= 1,
        f"first={first_run} total={len(flow.fake.sent) - since}",
    )

    retry_telegram_id = 880022
    retry_user = await users_db.get_or_create(
        retry_telegram_id, "reminder_retry", "Reminder retry"
    )
    retry_product_id = await products_db.create(
        slug="reminder-retry",
        name="Reminder retry",
        price_stars=10,
        duration_days=30,
        is_active=1,
    )
    retry_subscription_id = await subs_db.create(
        retry_user["id"],
        await products_db.get(retry_product_id),
        _inside_local_day(1),
        False,
    )
    scheduler.set_client(BlockedByCustomer(retry_telegram_id))
    await scheduler.notify_expiring()
    retry_subscription = await subs_db.get(retry_subscription_id)
    check(
        "a failed reminder remains eligible for retry",
        retry_subscription["notified_1d"] == 0,
        str(retry_subscription["notified_1d"]),
    )
    since = len(flow.fake.sent)
    scheduler.set_client(flow.fake)
    await scheduler.notify_expiring()
    retry_subscription = await subs_db.get(retry_subscription_id)
    check(
        "the reminder is recorded after a later successful delivery",
        retry_subscription["notified_1d"] == 1
        and any(peer == retry_telegram_id for peer, _ in flow.fake.sent[since:]),
    )

    class PausedReminder(flow.FakeClient):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def send_message(self, peer, text=None, buttons=None, parse_mode=None, file=None):
            if peer == retry_telegram_id:
                self.started.set()
                await asyncio.Event().wait()
            return await super().send_message(peer, text, buttons, parse_mode, file)

    await connection.execute(
        "UPDATE subscriptions SET notified_1d = 0, notified_3d = 0 WHERE id = ?",
        (retry_subscription_id,),
    )
    paused_client = PausedReminder()
    scheduler.set_client(paused_client)
    paused_reminder = asyncio.create_task(scheduler.notify_expiring())
    await paused_client.started.wait()
    paused_reminder.cancel()
    try:
        await paused_reminder
    except asyncio.CancelledError:
        pass
    retry_subscription = await subs_db.get(retry_subscription_id)
    check(
        "forced shutdown releases a reminder claim before cancellation completes",
        retry_subscription["notified_1d"] == 0,
        str(retry_subscription["notified_1d"]),
    )
    scheduler.set_client(flow.fake)

    # Everything looks expired at once: that is a wrong clock, not a wave of expiries.
    # Distinct slugs from the start: two active subscriptions on one slug are now
    # forbidden by a unique index, which is itself the point.
    for index in range(25):
        await connection.execute(
            "INSERT INTO subscriptions (user_id, product_slug, product_name, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (
                user["id"],
                f"probe{index}",
                "Probe",
                dates.to_sql(dates.utcnow() + timedelta(days=30)),
            ),
        )
    await connection.execute("UPDATE subscriptions SET expires_at = '2020-01-01 00:00:00'")
    since = len(flow.fake.sent)
    await scheduler.close_expired()
    check(
        "staff are warned about the clock instead",
        "clock" in recent_traffic(since).lower(),
        recent_traffic(since)[:70],
    )
    check(
        "a clock jump does not close every subscription at once",
        (await subs_db.get(sub_id))["credentials"] == "keep-me",
        (await subs_db.get(sub_id))["status"],
    )


async def guarded_actions(owner, manager, alice):
    # Things a check has to click, because reading the code is how the last three
    # regressions got past review.
    user = await users_db.get(ALICE_ID)

    # The invoice cooldown expires, it does not lock the product for good.
    product_id = await products_db.create(
        slug="cooldown", name="Cool", price_stars=50, duration_days=30, is_active=1
    )
    await start_stars_invoice(alice, product_id)
    again = await flow.click(alice, f"pay_stars:{product_id}")
    check(
        "a second invoice for the same product is refused while one is live",
        "already in the chat" in (again.answered or "").lower(),
        str(again.answered),
    )
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now','-2 hours') WHERE product_id = ?",
        (product_id,),
    )
    later = await start_stars_invoice(alice, product_id)
    check(
        "and allowed once the old one has expired",
        "invoice sent" in (later.answered or "").lower(),
        str(later.answered),
    )

    # The customer gets the full payment window after accepting the terms; time spent
    # reading the confirmation screen must not be subtracted from the invoice TTL.
    aged_product_id = await products_db.create(
        slug="aged-quote",
        name="Aged quote",
        price_stars=51,
        duration_days=15,
        is_active=1,
    )
    await flow.click(alice, f"pay_stars:{aged_product_id}")
    aged_token = await last_token()
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now', ?) WHERE token = ?",
        (f"-{max(1, INVOICE_TTL_MINUTES - 1)} minutes", aged_token),
    )
    aged_confirmation = await flow.click(alice, f"pay_stars_confirm:{aged_token}")
    aged_invoice = await invoices_db.get(aged_token)
    check(
        "activating an old quote starts a fresh invoice payment window",
        aged_invoice["status"] == invoices_db.PENDING
        and not checkout._invoice_expired(aged_invoice)
        and (dates.utcnow() - dates.parse(aged_invoice["created_at"])).total_seconds() < 10
        and "invoice sent" in (aged_confirmation.answered or "").lower(),
        str(aged_invoice),
    )

    # Public and personal products with one slug are alternatives, not two things the
    # customer may pay for at once. Adding the personal shadow closes the public invoice.
    shadow_public_id = await products_db.create(
        slug="invoice-shadow",
        name="Public shadow plan",
        price_stars=55,
        duration_days=20,
        is_active=1,
    )
    await start_stars_invoice(alice, shadow_public_id)
    public_shadow_token = await last_token()
    shadow_personal_id = await products_db.create(
        slug="invoice-shadow",
        name="Personal shadow plan",
        owner_user_id=user["id"],
        price_stars=45,
        duration_days=25,
        is_active=1,
    )
    shadow_terms = await flow.click(alice, f"pay_stars:{shadow_public_id}")
    personal_shadow_token = await last_token()
    await flow.click(alice, f"pay_stars_confirm:{personal_shadow_token}")
    shadow_live = await connection.fetch_all(
        "SELECT * FROM invoices WHERE telegram_id = ? AND product_slug = ? AND status = ?",
        (ALICE_ID, "invoice-shadow", invoices_db.PENDING),
    )
    check(
        "a personal shadow replaces the public invoice instead of doubling it",
        (await invoices_db.get(public_shadow_token))["status"] == invoices_db.CANCELLED
        and personal_shadow_token != public_shadow_token
        and "personal shadow plan" in flow.produced(shadow_terms).lower()
        and len(shadow_live) == 1
        and shadow_live[0]["product_id"] == shadow_personal_id,
        f"live={shadow_live}",
    )

    # Confirmation buttons are one-time quotes for the exact identity and terms shown.
    quote_product_id = await products_db.create(
        slug="quote-identity",
        name="Original entitlement",
        price_stars=64,
        duration_days=21,
        is_active=1,
    )
    await flow.click(alice, f"pay_stars:{quote_product_id}")
    identity_token = await last_token()
    await products_db.update(
        quote_product_id, slug="quote-identity-changed", name="Different entitlement"
    )
    stale_identity = await flow.click(alice, f"pay_stars_confirm:{identity_token}")
    refreshed_token = await last_token()
    check(
        "a renamed product invalidates an old confirmation at the same price",
        (await invoices_db.get(identity_token))["status"] == invoices_db.CANCELLED
        and refreshed_token != identity_token
        and (await invoices_db.get(refreshed_token))["status"] == invoices_db.QUOTE
        and not await invoices_db.has_pending_for_product(ALICE_ID, quote_product_id)
        and "different entitlement" in flow.produced(stale_identity).lower(),
    )

    old_terms = settings.get("terms_text")
    try:
        await settings.set_value("terms_text", old_terms + "\n\nUpdated checkout terms.")
        stale_terms = await flow.click(alice, f"pay_stars_confirm:{refreshed_token}")
        current_token = await last_token()
        check(
            "changed shop terms require a fresh confirmation",
            (await invoices_db.get(refreshed_token))["status"] == invoices_db.CANCELLED
            and current_token != refreshed_token
            and "updated checkout terms" in flow.produced(stale_terms).lower()
            and not await invoices_db.has_pending_for_product(ALICE_ID, quote_product_id),
        )
        confirmed = await flow.click(alice, f"pay_stars_confirm:{current_token}")
        invoice_count = await connection.fetch_value(
            "SELECT COUNT(*) FROM invoices WHERE product_id = ? AND status = ?",
            (quote_product_id, invoices_db.PENDING),
            0,
        )
        replay = await flow.click(alice, f"pay_stars_confirm:{current_token}")
        check(
            "a fresh quote creates one invoice and cannot be replayed",
            "invoice sent" in (confirmed.answered or "").lower()
            and invoice_count == 1
            and await connection.fetch_value(
                "SELECT COUNT(*) FROM invoices WHERE product_id = ? AND status = ?",
                (quote_product_id, invoices_db.PENDING),
                0,
            )
            == 1
            and "already used" in (replay.answered or "").lower(),
            str(replay.answered),
        )
    finally:
        await settings.set_value("terms_text", old_terms)

    legacy_product_id = await products_db.create(
        slug="legacy-confirmation",
        name="Legacy confirmation",
        price_stars=38,
        duration_days=14,
        is_active=1,
    )
    legacy_event = await flow.click(alice, f"pay_stars_confirm:{legacy_product_id}:38:14")
    legacy_quote = await connection.fetch_one(
        "SELECT * FROM invoices WHERE product_id = ? ORDER BY id DESC",
        (legacy_product_id,),
    )
    check(
        "an old-format Stars button only refreshes the terms",
        legacy_quote is not None
        and legacy_quote["status"] == invoices_db.QUOTE
        and "review the current terms" in (legacy_event.answered or "").lower(),
        str(legacy_event.answered),
    )

    # Erasing a customer is an owner action.
    target = await users_db.get(BOB_ID)
    event = await flow.click(manager, f"a:erase:{target['id']}")
    check(
        "a manager cannot even open the erase screen",
        "not enough rights" in (event.answered or "").lower(),
        str(event.answered),
    )
    await users_db.set_role((await users_db.get(MANAGER_ID))["id"], "admin")
    event = await flow.click(manager, f"a:erase:{target['id']}")
    check("nor can an admin", "owner" in (event.answered or "").lower(), str(event.answered))
    await users_db.set_role((await users_db.get(MANAGER_ID))["id"], "manager")
    event = await flow.click(owner, f"a:erase:{(await users_db.get(MANAGER_ID))['id']}")
    check(
        "and the owner cannot erase a colleague",
        "staff" in (event.answered or "").lower(),
        str(event.answered),
    )
    event = await flow.click(owner, f"a:erase:{target['id']}")
    check(
        "but can erase a customer, behind a confirmation",
        "erase the personal data" in flow.produced(event).lower(),
        flow.produced(event)[:60],
    )

    # Old Telegram messages retain their callback data after a deployment. They must
    # not bypass a confirmation screen introduced by a newer version.
    close_user = await users_db.get_or_create(880005, "old_close_button", "Old button")
    close_product_id = await products_db.create(
        slug="old-close-button",
        name="Old close button",
        price_stars=25,
        duration_days=30,
        is_active=1,
    )
    close_product = await products_db.get(close_product_id)
    close_subscription = await subs_db.create(
        close_user["id"], close_product, dates.utcnow() + timedelta(days=30), False
    )
    await subs_db.set_credentials(close_subscription, "keep-until-confirmed")
    old_close = await flow.click(owner, f"a:subcancel:{close_subscription}")
    still_open = await subs_db.get(close_subscription)
    check(
        "a close button from an old message opens the new confirmation",
        still_open["status"] == subs_db.ACTIVE
        and still_open["credentials"] == "keep-until-confirmed"
        and "close subscription" in flow.produced(old_close).lower()
        and old_close.keyboards[-1][0][0].data.decode()
        == f"a:subcancelgo:{close_subscription}",
        flow.produced(old_close)[:100],
    )
    await flow.click(owner, f"a:subcancelask:{close_subscription}")
    await flow.click(owner, f"a:subcancelgo:{close_subscription}")
    check(
        "the new close confirmation removes access",
        (await subs_db.get(close_subscription))["status"] == subs_db.CANCELLED
        and (await subs_db.get(close_subscription))["credentials"] is None,
    )

    # A receipt that is too big only after it is downloaded.
    method = (await requisites_db.list_all(active_only=True))[0]
    await users_db.set_payment_method(user["id"], method["id"])
    heavy_product = await products_db.create(
        slug="heavy", name="Heavy", price_rub=100, duration_days=30, is_active=1
    )
    await flow.click(alice, f"pay_tr:{heavy_product}")
    heavy = flow.FakeEvent(alice, flow.fake)
    heavy.photo = True
    heavy.media_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)
    await checkout.handle_receipt(heavy)
    check(
        "a photo that turns out to be too big is rejected",
        "5 mb" in flow.produced(heavy).lower(),
        flow.produced(heavy)[:60],
    )

    # And a good one reaches every member of staff.
    staff_ids = await users_db.staff_telegram_ids()
    since = len(flow.fake.sent)
    good = flow.FakeEvent(alice, flow.fake)
    good.photo = True
    await checkout.handle_receipt(good)
    delivered = {peer for peer, _ in flow.fake.sent[since:]}
    check(
        "the receipt reaches every staff member",
        set(staff_ids) <= delivered,
        f"{staff_ids} vs {sorted(delivered)}",
    )


async def erasure_integrity(owner):
    from config import RECEIPTS_DIR

    product_id = await products_db.create(
        slug="erase-integrity",
        name="Erase integrity",
        price_rub=100,
        duration_days=30,
        is_active=1,
    )
    product = await products_db.get(product_id)
    failures_before = len(flow.failures)

    # A reminder may have been selected just before erasure. If the same Telegram
    # account registers again, the stale snapshot remains bound to the old internal id.
    reminder_telegram_id = 880020
    reminder_user = await users_db.get_or_create(
        reminder_telegram_id, "old_reminder", "Old reminder"
    )
    reminder_subscription = await subs_db.create(
        reminder_user["id"], product, _inside_local_day(1), False
    )
    reminder_client = flow.FakeClient()
    scheduler.set_client(reminder_client)
    original_notify_user = notify.to_user
    reminder_entered = asyncio.Event()
    resume_reminder = asyncio.Event()
    reminder_task = None

    async def paused_reminder(client, telegram_id, *args, **kwargs):
        if telegram_id == reminder_telegram_id:
            reminder_entered.set()
            await resume_reminder.wait()
        return await original_notify_user(client, telegram_id, *args, **kwargs)

    notify.to_user = paused_reminder
    try:
        reminder_task = asyncio.create_task(scheduler.notify_expiring())
        await reminder_entered.wait()
        await users_db.erase(reminder_user["id"])
        new_reminder_user = await users_db.get_or_create(
            reminder_telegram_id, "new_reminder", "New reminder"
        )
        resume_reminder.set()
        await reminder_task
    finally:
        resume_reminder.set()
        notify.to_user = original_notify_user
        scheduler.set_client(flow.fake)
        if reminder_task is not None and not reminder_task.done():
            await asyncio.gather(reminder_task, return_exceptions=True)
    check(
        "a stale reminder is not delivered to a re-registered profile",
        new_reminder_user["id"] != reminder_user["id"]
        and (await subs_db.get(reminder_subscription))["status"] == subs_db.CANCELLED
        and not any(peer == reminder_telegram_id for peer, _ in reminder_client.sent),
    )

    # A late receipt for a timed-out transfer is allowed, but not after profile erasure
    # has won. The final attach statement revalidates both internal and Telegram ids.
    receipt_race_telegram_id = 880016
    receipt_race_user = await users_db.get_or_create(
        receipt_race_telegram_id, "receipt_erase_race", "Receipt erase race"
    )
    receipt_race_order = await orders_db.create(
        user_id=receipt_race_user["id"],
        product=product,
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.PAYMENT_EXPIRED,
    )
    original_execute_change = connection.execute_change
    receipt_attach_entered = asyncio.Event()
    resume_receipt_attach = asyncio.Event()
    receipt_attach_task = None

    async def paused_receipt_attach(sql, params=()):
        if sql.startswith("UPDATE orders SET status = ?, receipt_file = ?"):
            receipt_attach_entered.set()
            await resume_receipt_attach.wait()
        return await original_execute_change(sql, params)

    connection.execute_change = paused_receipt_attach
    try:
        receipt_attach_task = asyncio.create_task(
            orders_db.attach_receipt(
                receipt_race_order,
                "must-not-survive.jpg",
                receipt_race_user["id"],
                receipt_race_telegram_id,
            )
        )
        await receipt_attach_entered.wait()
        await users_db.erase(receipt_race_user["id"])
        resume_receipt_attach.set()
        receipt_attached = await receipt_attach_task
    finally:
        resume_receipt_attach.set()
        connection.execute_change = original_execute_change
        if receipt_attach_task is not None and not receipt_attach_task.done():
            await asyncio.gather(receipt_attach_task, return_exceptions=True)
    receipt_race_record = await orders_db.get(receipt_race_order)
    check(
        "a late receipt cannot reopen an order after profile erasure",
        not receipt_attached
        and receipt_race_record["status"] == orders_db.PAYMENT_EXPIRED
        and receipt_race_record["receipt_file"] is None
        and receipt_race_record["telegram_id"] < 0,
        str(receipt_race_record),
    )

    # Access details are read and sent under the same lifecycle lock. If erasure starts
    # after the response has been built, it must wait for the Telegram edit and then be
    # the final state that clears both the recipient id and the credentials.
    access_telegram_id = 880012
    access_sender = flow.Sender(access_telegram_id, "erase_access_race", "Access race")
    access_user = await users_db.get_or_create(
        access_telegram_id, "erase_access_race", "Access race"
    )
    access_subscription = await subs_db.create(
        access_user["id"], product, dates.utcnow() + timedelta(days=10), False
    )
    access_secret = "access-race-secret"
    await subs_db.set_credentials(access_subscription, access_secret)
    original_edit_or_reply = common.edit_or_reply
    access_send_entered = asyncio.Event()
    resume_access_send = asyncio.Event()
    recipient_was_live = []
    access_task = None
    access_erase_task = None

    async def paused_access_send(event, text, buttons=None, **kwargs):
        if access_secret not in text:
            return await original_edit_or_reply(event, text, buttons, **kwargs)
        access_send_entered.set()
        await resume_access_send.wait()
        current = await users_db.get_by_id(access_user["id"])
        recipient_was_live.append(
            bool(current and current["telegram_id"] == access_telegram_id)
        )
        return await original_edit_or_reply(event, text, buttons, **kwargs)

    common.edit_or_reply = paused_access_send
    try:
        access_task = asyncio.create_task(
            flow.click(access_sender, f"subdata:{access_subscription}")
        )
        await access_send_entered.wait()
        access_erase_task = asyncio.create_task(users_db.erase(access_user["id"]))
        await asyncio.sleep(0)
        check(
            "erasure waits while access details are being delivered",
            not access_erase_task.done(),
        )
        resume_access_send.set()
        access_event, _ = await asyncio.gather(access_task, access_erase_task)
    finally:
        resume_access_send.set()
        common.edit_or_reply = original_edit_or_reply
        pending = [
            task
            for task in (access_task, access_erase_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    erased_access_user = await users_db.get_by_id(access_user["id"])
    erased_access_subscription = await subs_db.get(access_subscription)
    check(
        "access is sent only while the recipient is live and erasure wins last",
        recipient_was_live == [True]
        and access_secret in flow.produced(access_event)
        and erased_access_user["telegram_id"] < 0
        and erased_access_subscription["credentials"] is None
        and erased_access_subscription["status"] == subs_db.CANCELLED,
        str(erased_access_subscription),
    )

    # An update may have loaded the old profile just before erasure. Resuming it must
    # not write Telegram names back onto the retained, anonymized financial row.
    profile_telegram_id = 880013
    profile_user = await users_db.get_or_create(
        profile_telegram_id, "profile_before_erase", "Before erase"
    )
    original_user_get = users_db.get
    stale_profile_loaded = asyncio.Event()
    resume_profile_update = asyncio.Event()
    profile_get_count = 0
    profile_task = None

    async def paused_profile_get(telegram_id):
        nonlocal profile_get_count
        result = await original_user_get(telegram_id)
        if telegram_id == profile_telegram_id and profile_get_count == 0:
            profile_get_count += 1
            stale_profile_loaded.set()
            await resume_profile_update.wait()
        return result

    users_db.get = paused_profile_get
    try:
        profile_task = asyncio.create_task(
            users_db.get_or_create(profile_telegram_id, "profile_after_erase", "After erase")
        )
        await stale_profile_loaded.wait()
        await users_db.erase(profile_user["id"])
        resume_profile_update.set()
        profile_result = await profile_task
    finally:
        resume_profile_update.set()
        users_db.get = original_user_get
        if profile_task is not None and not profile_task.done():
            await asyncio.gather(profile_task, return_exceptions=True)
    erased_profile = await users_db.get_by_id(profile_user["id"])
    check(
        "a stale profile refresh cannot restore erased names",
        profile_result is None
        and erased_profile["telegram_id"] < 0
        and erased_profile["username"] is None
        and erased_profile["first_name"] == "erased"
        and erased_profile["last_name"] is None,
        str(erased_profile),
    )

    # If the same Telegram account starts again while an old history screen is paused,
    # the old screen remains bound to its original internal row and is suppressed.
    history_telegram_id = 880014
    history_sender = flow.Sender(history_telegram_id, "old_history", "Old history")
    history_user = await users_db.get_or_create(
        history_telegram_id, "old_history", "Old history"
    )
    await orders_db.create(
        user_id=history_user["id"],
        product=product,
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.COMPLETED,
    )
    history_event = flow.FakeEvent(history_sender, flow.fake)
    history_event._narromarket_user_id = history_user["id"]
    original_history_list = orders_db.list_for_user
    history_loaded = asyncio.Event()
    resume_history = asyncio.Event()
    history_task = None

    async def paused_history_list(user_id, limit=10):
        result = await original_history_list(user_id, limit)
        if user_id == history_user["id"]:
            history_loaded.set()
            await resume_history.wait()
        return result

    orders_db.list_for_user = paused_history_list
    try:
        history_task = asyncio.create_task(account.show_orders(history_event, history_user))
        await history_loaded.wait()
        await users_db.erase(history_user["id"])
        new_history_user = await users_db.get_or_create(
            history_telegram_id, "new_history", "New history"
        )
        resume_history.set()
        await history_task
    finally:
        resume_history.set()
        orders_db.list_for_user = original_history_list
        if history_task is not None and not history_task.done():
            await asyncio.gather(history_task, return_exceptions=True)
    check(
        "an old history response is not sent to a re-registered account",
        new_history_user["id"] != history_user["id"] and not history_event.replies,
        str(history_event.replies),
    )

    # Orderless automatic refunds also retain the original internal profile identity.
    # Telegram ids alone are insufficient because the same account can register a new
    # profile after erasure while an old refund notice is still in flight.
    orphan_refund_telegram_id = 880017
    orphan_refund_user = await users_db.get_or_create(
        orphan_refund_telegram_id, "orphan_refund", "Orphan refund"
    )
    orphan_refund = await refunds_db.create(
        telegram_id=orphan_refund_telegram_id,
        user_id=orphan_refund_user["id"],
        source="automatic",
        reason="identity binding probe",
        telegram_charge_id="orphan-refund-identity",
        amount_stars=12,
    )
    orphan_lease = await refunds_db.claim(orphan_refund["id"])
    await refunds_db.mark_completed(orphan_refund["id"], orphan_lease)
    await users_db.erase(orphan_refund_user["id"])
    new_orphan_user = await users_db.get_or_create(
        orphan_refund_telegram_id, "orphan_refund_new", "New refund profile"
    )
    orphan_client = flow.FakeClient()
    await stars.refund(
        orphan_client,
        orphan_refund_telegram_id,
        "orphan-refund-identity",
        "Old refund notice",
        refund_id=orphan_refund["id"],
        user_id=orphan_refund_user["id"],
    )
    stored_orphan_refund = await refunds_db.get(orphan_refund["id"])
    check(
        "an old orderless refund notice is not sent to a new profile",
        stored_orphan_refund["user_id"] == orphan_refund_user["id"]
        and stored_orphan_refund["telegram_id"] == 0
        and new_orphan_user["id"] != orphan_refund_user["id"]
        and not any(peer == orphan_refund_telegram_id for peer, _ in orphan_client.sent),
        str(stored_orphan_refund),
    )

    # A completed order may still need a Stars refund after the customer has erased
    # their profile. The financial recipient snapshot must survive while chat/profile
    # data stays erased, and no customer message should target the placeholder id.
    refund_telegram_id = 880008
    refund_user = await users_db.get_or_create(
        refund_telegram_id, "post_erase_refund", "Post erase refund"
    )
    refund_product_id = await products_db.create(
        slug="post-erase-refund",
        name="Post erase refund",
        price_stars=52,
        duration_days=12,
        is_active=1,
    )
    refund_product = await products_db.get(refund_product_id)
    refund_order_id = await orders_db.create(
        user_id=refund_user["id"],
        product=refund_product,
        amount_stars=52,
        payment_method="stars",
        status=orders_db.PAID,
        payment_charge_id="post-erasure-refund",
        payment_provider_charge_id="provider-post-erasure-refund",
        payment_recipient_id=refund_telegram_id,
    )
    await billing.apply_payment(await orders_db.get(refund_order_id))
    await orders_db.set_status(refund_order_id, orders_db.COMPLETED)
    await users_db.erase(refund_user["id"])
    refund_client = flow.FakeClient()
    confirmation = await flow.click(
        owner, f"a:refundask:{refund_order_id}", event_client=refund_client
    )
    await flow.click(owner, f"a:refundgo:{refund_order_id}", event_client=refund_client)
    refund_calls = [
        request
        for request in refund_client.requests
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    post_erase_order = await orders_db.get(refund_order_id)
    post_erase_refund = await refunds_db.for_order(refund_order_id)
    check(
        "an erased customer's completed Stars order still opens refund confirmation",
        "refund order" in flow.produced(confirmation).lower(),
        flow.produced(confirmation)[:100],
    )
    check(
        "a post-erasure Stars refund uses the retained payment recipient",
        post_erase_order["telegram_id"] < 0
        and post_erase_order["payment_recipient_id"] == refund_telegram_id
        and post_erase_order["status"] == orders_db.REFUNDED
        and post_erase_refund["status"] == refunds_db.COMPLETED
        and post_erase_refund["telegram_id"] == 0
        and len(refund_calls) == 1
        and refund_calls[0].user_id == refund_telegram_id
        and refund_calls[0].charge_id == "post-erasure-refund"
        and not any(peer == refund_telegram_id for peer, _ in refund_client.sent),
        f"order={post_erase_order} refund={post_erase_refund}",
    )

    # A staff click can load a live completed order just before erasure commits. The
    # retained payment recipient still makes the refund valid, but that stale joined
    # Telegram id must never be used for a customer notification afterwards.
    stale_refund_telegram_id = 880011
    stale_refund_user = await users_db.get_or_create(
        stale_refund_telegram_id, "stale_refund_notice", "Stale refund notice"
    )
    stale_refund_order_id = await orders_db.create(
        user_id=stale_refund_user["id"],
        product=refund_product,
        amount_stars=52,
        payment_method="stars",
        status=orders_db.PAID,
        payment_charge_id="stale-refund-notice",
        payment_provider_charge_id="provider-stale-refund-notice",
        payment_recipient_id=stale_refund_telegram_id,
    )
    await billing.apply_payment(await orders_db.get(stale_refund_order_id))
    await orders_db.set_status(stale_refund_order_id, orders_db.COMPLETED)
    await flow.click(owner, f"a:refundask:{stale_refund_order_id}")
    original_order_get = orders_db.get
    stale_order_loaded = asyncio.Event()
    resume_stale_refund = asyncio.Event()
    stale_get_count = 0
    stale_refund_task = None
    stale_erase_task = None
    stale_refund_client = flow.FakeClient()

    async def pause_first_stale_order_read(order_id):
        nonlocal stale_get_count
        result = await original_order_get(order_id)
        if order_id == stale_refund_order_id and stale_get_count == 0:
            stale_get_count += 1
            stale_order_loaded.set()
            await resume_stale_refund.wait()
        return result

    orders_db.get = pause_first_stale_order_read
    try:
        stale_refund_task = asyncio.create_task(
            flow.click(
                owner,
                f"a:refundgo:{stale_refund_order_id}",
                event_client=stale_refund_client,
            )
        )
        await stale_order_loaded.wait()
        stale_erase_task = asyncio.create_task(users_db.erase(stale_refund_user["id"]))
        await stale_erase_task
        resume_stale_refund.set()
        await stale_refund_task
    finally:
        resume_stale_refund.set()
        orders_db.get = original_order_get
        pending = [
            task
            for task in (stale_refund_task, stale_erase_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    stale_refund_calls = [
        request
        for request in stale_refund_client.requests
        if type(request).__name__ == "RefundStarsChargeRequest"
    ]
    check(
        "a refund loaded before erasure never sends to the stale profile id",
        (await users_db.get_by_id(stale_refund_user["id"]))["telegram_id"] < 0
        and (await orders_db.get(stale_refund_order_id))["status"] == orders_db.REFUNDED
        and (await refunds_db.for_order(stale_refund_order_id))["telegram_id"] == 0
        and [request.charge_id for request in stale_refund_calls] == ["stale-refund-notice"]
        and not any(peer == stale_refund_telegram_id for peer, _ in stale_refund_client.sent),
    )

    # A transfer decision used to pause after reading the live user, let erasure commit,
    # then create a new order and send the old account fresh bank details. Erasure must
    # wait for the whole checkout response and then be the final database state.
    transfer_telegram_id = 880009
    transfer_sender = flow.Sender(
        transfer_telegram_id, "transfer_erase_race", "Transfer erase race"
    )
    transfer_user = await users_db.get_or_create(
        transfer_telegram_id, "transfer_erase_race", "Transfer erase race"
    )
    transfer_method = (await requisites_db.list_all(active_only=True))[0]
    await users_db.set_payment_method(transfer_user["id"], transfer_method["id"])
    transfer_product_id = await products_db.create(
        slug="transfer-erase-race",
        name="Transfer erase race",
        price_rub=321,
        duration_days=9,
        is_active=1,
    )
    original_order_create = orders_db.create
    transfer_create_entered = asyncio.Event()
    resume_transfer_create = asyncio.Event()
    transfer_task = None
    transfer_erase_task = None

    async def paused_transfer_create(*args, **kwargs):
        current_product = kwargs.get("product") or (args[1] if len(args) > 1 else {})
        if current_product.get("slug") == "transfer-erase-race":
            transfer_create_entered.set()
            await resume_transfer_create.wait()
        return await original_order_create(*args, **kwargs)

    orders_db.create = paused_transfer_create
    try:
        transfer_task = asyncio.create_task(
            flow.click(transfer_sender, f"pay_tr:{transfer_product_id}")
        )
        await transfer_create_entered.wait()
        transfer_erase_task = asyncio.create_task(users_db.erase(transfer_user["id"]))
        await asyncio.sleep(0)
        check(
            "erasure waits for an in-flight transfer checkout",
            not transfer_erase_task.done(),
        )
        resume_transfer_create.set()
        transfer_event, _ = await asyncio.gather(transfer_task, transfer_erase_task)
    finally:
        resume_transfer_create.set()
        orders_db.create = original_order_create
        pending = [
            task
            for task in (transfer_task, transfer_erase_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    transfer_order = await connection.fetch_one(
        "SELECT * FROM orders WHERE user_id = ? AND product_slug = ?",
        (transfer_user["id"], "transfer-erase-race"),
    )
    old_transfer_events = await connection.fetch_value(
        "SELECT COUNT(*) FROM events WHERE telegram_id = ?", (transfer_telegram_id,), 0
    )
    check(
        "transfer checkout cannot recreate data after erasure",
        (await users_db.get_by_id(transfer_user["id"]))["telegram_id"] < 0
        and transfer_order is not None
        and transfer_order["status"] == orders_db.CANCELLED
        and old_transfer_events == 0
        and "send it here" in flow.produced(transfer_event).lower(),
        f"order={transfer_order} events={old_transfer_events}",
    )

    # The same boundary applies to one-time Stars quotes. Once activation commits, the
    # lifecycle lock stays held until Telegram has accepted the invoice and the local
    # follow-up is complete; erasure then cancels/scrubs that invoice and its event.
    quote_telegram_id = 880010
    quote_sender = flow.Sender(quote_telegram_id, "quote_erase_race", "Quote erase race")
    quote_user = await users_db.get_or_create(
        quote_telegram_id, "quote_erase_race", "Quote erase race"
    )
    quote_product_id = await products_db.create(
        slug="quote-erase-race",
        name="Quote erase race",
        price_stars=63,
        duration_days=13,
        is_active=1,
    )
    await flow.click(quote_sender, f"pay_stars:{quote_product_id}")
    quote_record = await connection.fetch_one(
        "SELECT * FROM invoices WHERE telegram_id = ? AND product_id = ? ORDER BY id DESC",
        (quote_telegram_id, quote_product_id),
    )
    original_send_invoice = checkout.send_invoice
    invoice_send_entered = asyncio.Event()
    resume_invoice_send = asyncio.Event()
    quote_task = None
    quote_erase_task = None

    async def paused_send_invoice(client, telegram_id, current_product, token):
        if token == quote_record["token"]:
            invoice_send_entered.set()
            await resume_invoice_send.wait()
        return await original_send_invoice(client, telegram_id, current_product, token)

    checkout.send_invoice = paused_send_invoice
    try:
        quote_task = asyncio.create_task(
            flow.click(quote_sender, f"pay_stars_confirm:{quote_record['token']}")
        )
        await invoice_send_entered.wait()
        quote_erase_task = asyncio.create_task(users_db.erase(quote_user["id"]))
        await asyncio.sleep(0)
        check(
            "erasure waits while an activated Stars invoice is being sent",
            not quote_erase_task.done(),
        )
        resume_invoice_send.set()
        await asyncio.gather(quote_task, quote_erase_task)
    finally:
        resume_invoice_send.set()
        checkout.send_invoice = original_send_invoice
        pending = [
            task
            for task in (quote_task, quote_erase_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    erased_quote = await invoices_db.get(quote_record["token"])
    old_quote_events = await connection.fetch_value(
        "SELECT COUNT(*) FROM events WHERE telegram_id = ?", (quote_telegram_id,), 0
    )
    check(
        "Stars checkout leaves erasure as the final state",
        (await users_db.get_by_id(quote_user["id"]))["telegram_id"] < 0
        and erased_quote["telegram_id"] == 0
        and erased_quote["status"] == invoices_db.CANCELLED
        and old_quote_events == 0,
        f"invoice={erased_quote} events={old_quote_events}",
    )

    # Pause an extension after its eligibility checks, then start erasure. The gift
    # transaction must finish first and erasure must be the final state; older code
    # resumed afterwards and silently set the erased subscription back to active.
    race_user = await users_db.get_or_create(880006, "erase_gift_race", "Gift race")
    race_subscription = await subs_db.create(
        race_user["id"], product, dates.utcnow() + timedelta(days=10), False
    )
    await subs_db.set_credentials(race_subscription, "erase-me")
    original_add_days = subs_db.add_days
    add_days_entered = asyncio.Event()
    resume_add_days = asyncio.Event()
    gift_task = None
    erase_task = None

    async def paused_add_days(subscription_id, days):
        add_days_entered.set()
        await resume_add_days.wait()
        return await original_add_days(subscription_id, days)

    subs_db.add_days = paused_add_days
    try:
        gift_task = asyncio.create_task(billing.gift_days(race_subscription, 5))
        await add_days_entered.wait()
        erase_task = asyncio.create_task(users_db.erase(race_user["id"]))
        await asyncio.sleep(0)
        check(
            "erasure waits for an in-flight subscription extension",
            not erase_task.done(),
        )
        resume_add_days.set()
        await asyncio.gather(gift_task, erase_task)
    finally:
        resume_add_days.set()
        subs_db.add_days = original_add_days
        pending = [
            task for task in (gift_task, erase_task) if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    erased_race_user = await users_db.get_by_id(race_user["id"])
    erased_race_subscription = await subs_db.get(race_subscription)
    check(
        "an extension race cannot reactivate an erased customer",
        erased_race_user["telegram_id"] < 0
        and erased_race_user["is_blocked"]
        and erased_race_subscription["status"] == subs_db.CANCELLED
        and erased_race_subscription["credentials"] is None
        and not await subs_db.active_for_slug(race_user["id"], product["slug"]),
        str(erased_race_subscription),
    )

    # Pause at the last possible boundary, inside the customer send. Erasure must wait
    # for that message to finish instead of committing and letting the handler use a
    # Telegram id that has already been removed.
    notice_user_id = 880007
    notice_user = await users_db.get_or_create(
        notice_user_id, "erase_notice_race", "Notice race"
    )
    notice_subscription = await subs_db.create(
        notice_user["id"], product, dates.utcnow() + timedelta(days=10), False
    )
    original_notify_user = notify.to_user
    notice_send_entered = asyncio.Event()
    resume_notice_send = asyncio.Event()
    recipient_was_live = []
    handler_task = None
    notice_erase_task = None
    notice_event = None
    sent_before = len(flow.fake.sent)

    async def paused_customer_notice(client, telegram_id, *args, **kwargs):
        if telegram_id != notice_user_id:
            return await original_notify_user(client, telegram_id, *args, **kwargs)
        notice_send_entered.set()
        await resume_notice_send.wait()
        result = await original_notify_user(client, telegram_id, *args, **kwargs)
        current = await users_db.get_by_id(notice_user["id"])
        recipient_was_live.append(bool(current and current["telegram_id"] == notice_user_id))
        return result

    notify.to_user = paused_customer_notice
    try:
        await flow.click(owner, f"a:subdays:{notice_subscription}")
        sent_before = len(flow.fake.sent)
        handler_task = asyncio.create_task(
            flow.send_text(owner, "5", label="extend while customer is erased")
        )
        await notice_send_entered.wait()
        notice_erase_task = asyncio.create_task(users_db.erase(notice_user["id"]))
        await asyncio.sleep(0)
        check(
            "erasure waits for an in-flight gift notification",
            not notice_erase_task.done(),
        )
        resume_notice_send.set()
        notice_event, _ = await asyncio.gather(handler_task, notice_erase_task)
    finally:
        resume_notice_send.set()
        notify.to_user = original_notify_user
        pending = [
            task
            for task in (handler_task, notice_erase_task)
            if task is not None and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    check(
        "the gift notice finishes while the recipient is live and erasure wins last",
        recipient_was_live == [True]
        and any(peer == notice_user_id for peer, _ in flow.fake.sent[sent_before:])
        and "days added" in flow.produced(notice_event).lower()
        and (await users_db.get_by_id(notice_user["id"]))["telegram_id"] < 0
        and (await subs_db.get(notice_subscription))["status"] == subs_db.CANCELLED,
        flow.produced(notice_event)[:100],
    )

    blocked = await users_db.get_or_create(880001, "erase_blocked", "Blocked erase")
    blocked_order = await orders_db.create(
        user_id=blocked["id"],
        product=product,
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.PENDING_REVIEW,
    )
    refused = await flow.click(
        owner, f"a:erasego:{blocked['id']}", label="erase with unresolved order"
    )
    blocked_after = await users_db.get_by_id(blocked["id"])
    check(
        "the erase handler reports an unresolved order",
        "resolve order" in (refused.answered or "").lower(),
        str(refused.answered),
    )
    check(
        "a refused erase changes neither the user nor the order",
        blocked_after["telegram_id"] == 880001
        and (await orders_db.get(blocked_order))["status"] == orders_db.PENDING_REVIEW,
    )

    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    removed_user = await users_db.get_or_create(880002, "erase_removed", "Removed receipt")
    removed_order = await orders_db.create(
        user_id=removed_user["id"],
        product=product,
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.COMPLETED,
    )
    removed_name = "erase_success.jpg"
    removed_path = RECEIPTS_DIR / removed_name
    removed_path.write_bytes(b"receipt")
    await orders_db.update(removed_order, receipt_file=removed_name)
    removed_event = await flow.click(
        owner, f"a:erasego:{removed_user['id']}", label="erase after successful unlink"
    )
    check(
        "a successfully unlinked receipt loses its database pointer",
        not removed_path.exists()
        and (await orders_db.get(removed_order))["receipt_file"] is None,
        flow.produced(removed_event)[:80],
    )

    retained_user = await users_db.get_or_create(880003, "erase_retained", "Retained receipt")
    retained_order = await orders_db.create(
        user_id=retained_user["id"],
        product=product,
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.COMPLETED,
    )
    retained_name = "erase_unlink_failure.jpg"
    retained_path = RECEIPTS_DIR / retained_name
    retained_path.mkdir()
    await orders_db.update(retained_order, receipt_file=retained_name)
    retained_event = await flow.click(
        owner, f"a:erasego:{retained_user['id']}", label="erase after failed unlink"
    )
    check(
        "a failed receipt unlink stays linked for retry",
        retained_path.is_dir()
        and (await orders_db.get(retained_order))["receipt_file"] == retained_name,
        flow.produced(retained_event)[:80],
    )
    check(
        "the erase handler reports receipt cleanup failures",
        "manual cleanup" in flow.produced(retained_event).lower(),
        flow.produced(retained_event)[:80],
    )
    check(
        "all erase outcomes are handled inside the callback",
        len(flow.failures) == failures_before,
        str(flow.failures[failures_before:]),
    )


async def granting_access(owner, manager, bob):
    target = await users_db.get(BOB_ID)
    product_id = await products_db.create(
        slug="granted", name="Granted", price_stars=100, duration_days=30, is_active=1
    )

    event = await flow.click(bob, f"a:grant:{target['id']}")
    check(
        "a customer cannot reach the grant screen",
        "not enough rights" in (event.answered or "").lower(),
        str(event.answered),
    )

    event = await flow.click(owner, f"a:grant:{target['id']}")
    check(
        "staff get a product to pick",
        "grant access to" in flow.produced(event).lower(),
        flow.produced(event)[:60],
    )

    await flow.click(owner, f"a:grantp:{target['id']}:{product_id}")
    event = await flow.send_text(owner, "45", label="grant 45 days")
    subscription = await subs_db.active_for_slug(target["id"], "granted")
    check("the subscription appears", subscription is not None)
    check(
        "staff are told what happened",
        "granted" in flow.produced(event).lower()
        and "nothing was charged" in flow.produced(event).lower(),
        flow.produced(event)[:80],
    )
    check(
        "and the customer is told too",
        "access granted" in recent_traffic(len(flow.fake.sent) - 3).lower(),
        recent_traffic(len(flow.fake.sent) - 3)[:60],
    )

    event = await flow.click(owner, f"a:grantp:{target['id']}:{product_id}")
    check(
        "granting it a second time is refused",
        "already have it" in (event.answered or "").lower(),
        str(event.answered),
    )

    other_id = await products_db.create(
        slug="granted-two", name="Granted Two", price_stars=100, duration_days=30, is_active=1
    )
    await flow.click(owner, f"a:grantp:{target['id']}:{other_id}")
    event = await flow.send_text(owner, "99999", label="an absurd gift")
    check(
        "an absurd number of days is refused",
        "between 1 and 3650" in flow.produced(event).lower(),
        flow.produced(event)[:60],
    )
    await flow.send_text(owner, "cancel", label="close the prompt")

    # A manager may grant, since a manager can already add unlimited days.
    event = await flow.click(manager, f"a:grant:{target['id']}")
    check(
        "a manager may grant access",
        "grant access to" in flow.produced(event).lower(),
        (event.answered or "")[:40],
    )


async def housekeeping(owner, alice):
    # The jobs that quietly change customer state on a schedule. None of them was ever
    # executed by a check, so any of them could be deleted without a red run.
    user = await users_db.get(ALICE_ID)
    product_id = await products_db.create(
        slug="housekeeping", name="House", price_rub=400, duration_days=30, is_active=1
    )
    product = await products_db.get(product_id)

    # A receipt nobody reviews has to reach staff, not sit forever.
    order_id = await orders_db.create(
        user_id=user["id"],
        product=product,
        amount_rub=400,
        payment_method="transfer",
        status=orders_db.PENDING_REVIEW,
    )
    await connection.execute(
        "UPDATE orders SET updated_at = datetime('now','-99 hours') WHERE id = ?", (order_id,)
    )
    since = len(flow.fake.sent)
    await scheduler.flag_stale_reviews()
    check(
        "an unreviewed receipt is escalated to staff",
        (await orders_db.get(order_id))["status"] == orders_db.PROBLEM,
        (await orders_db.get(order_id))["status"],
    )
    check(
        "and staff are actually told",
        "waiting for review" in recent_traffic(since).lower(),
        recent_traffic(since)[:70],
    )
    check(
        "the payment can still be confirmed afterwards",
        await orders_db.claim_status(
            order_id, orders_db.PAID, (orders_db.PENDING_REVIEW, orders_db.PROBLEM)
        ),
    )
    check(
        "and confirming it finally books the money",
        (await orders_db.get(order_id))["paid_at"] is not None,
    )

    # The monthly report goes out once a month, not once a day.
    await settings.set_value("last_monthly_report", "")
    await settings.set_value("monthly_report_day", "1")
    since = len(flow.fake.sent)
    await scheduler.monthly_report_if_due()
    first = len(flow.fake.sent) - since
    await scheduler.monthly_report_if_due()
    check(
        "the monthly report is sent once",
        first >= 1 and len(flow.fake.sent) - since == first,
        f"first={first} total={len(flow.fake.sent) - since}",
    )

    # Old events go, recent ones stay.
    await connection.execute("DELETE FROM events")
    await journal.event("recent", ALICE_ID, "now")
    await journal.event("ancient", ALICE_ID, "then")
    await connection.execute(
        "UPDATE events SET created_at = datetime('now','-400 days') WHERE type = 'ancient'"
    )
    await scheduler.prune_events()
    kinds = {row["type"] for row in await connection.fetch_all("SELECT type FROM events")}
    check("old events are pruned", "ancient" not in kinds, str(kinds))
    check("recent ones are kept", "recent" in kinds, str(kinds))

    # A receipt file is removed only when its order has been closed long enough, and a
    # file nobody points at is reported rather than deleted.
    from config import RECEIPTS_DIR

    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    keep = RECEIPTS_DIR / "receipt_keep.jpg"
    drop = RECEIPTS_DIR / "receipt_drop.jpg"
    orphan = RECEIPTS_DIR / "receipt_orphan.jpg"
    for path in (keep, drop, orphan):
        path.write_bytes(b"x")
    import os as _os

    _os.utime(orphan, (0, 0))

    fresh_order = await orders_db.create(
        user_id=user["id"],
        product=product,
        amount_rub=400,
        payment_method="transfer",
        status=orders_db.PENDING_REVIEW,
    )
    await orders_db.update(fresh_order, receipt_file="receipt_keep.jpg")
    old_order = await orders_db.create(
        user_id=user["id"],
        product=product,
        amount_rub=400,
        payment_method="transfer",
        status=orders_db.COMPLETED,
    )
    await orders_db.update(old_order, receipt_file="receipt_drop.jpg")
    await connection.execute(
        "UPDATE orders SET updated_at = datetime('now','-400 days') WHERE id = ?", (old_order,)
    )

    since = len(flow.fake.sent)
    await scheduler.prune_receipts()
    await scheduler.report_orphan_receipts()
    check("a receipt still under review is kept", keep.exists())
    check("a receipt of a long closed order is removed", not drop.exists())
    check(
        "and its pointer is cleared", (await orders_db.get(old_order))["receipt_file"] is None
    )
    check("a file nobody points at is reported, not deleted", orphan.exists())
    check(
        "and staff hear about it",
        "not linked to any" in recent_traffic(since).lower(),
        recent_traffic(since)[:70],
    )

    cancelled_path = RECEIPTS_DIR / "receipt_cancelled_cleanup.jpg"
    cancelled_path.write_bytes(b"x")
    cancelled_order = await orders_db.create(
        user_id=user["id"],
        product=product,
        amount_rub=400,
        payment_method="transfer",
        status=orders_db.COMPLETED,
    )
    await orders_db.update(cancelled_order, receipt_file=cancelled_path.name)
    await connection.execute(
        "UPDATE orders SET updated_at = datetime('now','-400 days') WHERE id = ?",
        (cancelled_order,),
    )
    original_forget = orders_db.forget_receipt
    forget_started = asyncio.Event()
    calls = 0

    async def paused_forget(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            forget_started.set()
            await asyncio.Event().wait()
        return await original_forget(*args, **kwargs)

    orders_db.forget_receipt = paused_forget
    cleanup = asyncio.create_task(scheduler.prune_receipts())
    try:
        await forget_started.wait()
        cleanup.cancel()
        try:
            await cleanup
        except asyncio.CancelledError:
            pass
    finally:
        orders_db.forget_receipt = original_forget
        if not cleanup.done():
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
    check(
        "receipt cancellation completes the database cleanup after unlink",
        not cancelled_path.exists()
        and (await orders_db.get(cancelled_order))["receipt_file"] is None,
    )


async def delivery_truth():
    class Refusing:
        async def send_message(self, *args, **kwargs):
            raise RuntimeError("blocked")

    check(
        "a message that could not be sent is reported as not sent",
        not await notify.to_user(Refusing(), ALICE_ID, "hello"),
    )
    check(
        "and one that went through is reported as sent",
        await notify.to_user(flow.fake, ALICE_ID, "hello"),
    )


async def robustness():
    await settings.set_value("star_to_rub", "not a number")
    check(
        "a broken rate falls back to the default", settings.get_float("star_to_rub", 1.5) == 1.5
    )
    await settings.set_value("star_to_rub", "1.5")

    longest = max((len(str(text)) for _, text in flow.fake.sent), default=0)
    check("no message hits the telegram size limit", longest < 4096, f"longest={longest}")


async def run():
    await connection.connect()
    await settings.load()
    await users_db.ensure_owners([OWNER_ID])
    for item in DEMO_PRODUCTS:
        await products_db.create(owner_user_id=products_db.PUBLIC, is_active=1, **item)

    owner = flow.Sender(OWNER_ID, "boss", "Boss")
    alice = flow.Sender(ALICE_ID, "alice", "Alice")
    bob = flow.Sender(BOB_ID, "bob", "Bob")
    noname = flow.Sender(NONAME_ID, None, "NoName")
    blocked = flow.Sender(BLOCKED_ID, "blocked", "Blocked")
    manager = flow.Sender(MANAGER_ID, "mgr", "Mgr")

    await users_db.get_or_create(ALICE_ID, "alice", "Alice")
    await users_db.get_or_create(BOB_ID, "bob", "Bob")
    await users_db.set_role(
        (await users_db.get_or_create(MANAGER_ID, "mgr", "Mgr"))["id"], "manager"
    )
    await users_db.set_blocked(
        (await users_db.get_or_create(BLOCKED_ID, "blocked", "B"))["id"], True
    )

    print("\n-- users that must be refused", flush=True)
    await refused_users(alice, blocked, noname)
    print("\n-- unknown ids", flush=True)
    await unknown_ids(owner, alice)
    print("\n-- other people's data", flush=True)
    subscription = await other_peoples_data(alice, bob)
    print("\n-- broken invoices", flush=True)
    await broken_invoices(alice, bob)
    print("\n-- pre-checkout", flush=True)
    await precheckout(alice)
    print("\n-- payment options", flush=True)
    rub_only = await payment_options(alice)
    print("\n-- receipts", flush=True)
    await receipts(alice, bob, rub_only)
    print("\n-- role limits", flush=True)
    await role_limits(owner, manager)
    print("\n-- scheduled jobs", flush=True)
    await scheduled_jobs(subscription)
    print("\n-- staff customer locks", flush=True)
    await staff_customer_locks(owner, manager)
    print("\n-- expiry and credentials race", flush=True)
    await expiry_credentials_race()
    print("\n-- concurrency and limits", flush=True)
    await concurrency_and_limits(owner)
    print("\n-- refund recovery", flush=True)
    await refund_recovery(owner)
    print("\n-- deleted product", flush=True)
    await deleted_product_with_a_live_subscription(alice)
    print("\n-- closing orders out", flush=True)
    await closing_out_orders(owner, alice, rub_only)
    print("\n-- stale access", flush=True)
    await stale_access(alice)
    print("\n-- blocked uploads", flush=True)
    await blocked_uploads(blocked)
    print("\n-- flow state rules", flush=True)
    await flow_state_rules(owner)
    print("\n-- clock and reminders", flush=True)
    await clock_and_reminders(alice)
    print("\n-- granting access", flush=True)
    await granting_access(owner, manager, bob)
    print("\n-- guarded actions", flush=True)
    await guarded_actions(owner, manager, alice)
    print("\n-- erasure integrity", flush=True)
    await erasure_integrity(owner)
    print("\n-- housekeeping", flush=True)
    await housekeeping(owner, alice)
    print("\n-- delivery", flush=True)
    await delivery_truth()
    print("\n-- robustness", flush=True)
    await robustness()

    await connection.disconnect()


def main():
    try:
        asyncio.run(flow._sandbox.run_and_close(run))
    finally:
        flow._sandbox.cleanup()

    # Everything the harness recorded counts, including "no handler answers this
    # button": the old filter kept only two substrings and quietly dropped the rest.
    problems.extend(flow.failures)
    print(f"\nproblems: {len(problems)}")
    for item in problems:
        print("  FAIL", item)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
