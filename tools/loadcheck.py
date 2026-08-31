# Scale and limits: python -m tools.loadcheck
#
# A shop with three products behaves nothing like a shop with three hundred.
# This one fills the database up, renders the heaviest screens and checks that
# every message and keyboard still fits what Telegram accepts, then makes sure
# the startup checks clean up whatever happened while the bot was down.

import asyncio
import sys
from datetime import timedelta

from tools import _sandbox  # noqa: F401  imported first, it sets DATABASE_PATH
from tools import walkthrough as flow

from db import connection, orders as orders_db, products as products_db
from db import settings, subscriptions as subs_db, users as users_db
from services import billing, scheduler
from tools.seed_demo import DEMO_PRODUCTS
from utils import dates

_sandbox.guard_live_database()

OWNER_ID, ALICE_ID = 111, 222

TEXT_LIMIT = 4096
BUTTON_LIMIT = 100
CALLBACK_LIMIT = 64

problems = []


def check(name, condition, detail=""):
    if not condition:
        problems.append(f"{name}: {detail}")
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}"[:130], flush=True)


def measure(event):
    # Longest message and biggest keyboard this screen produced.
    longest = max((len(text or "") for text in event.replies), default=0)
    buttons = longest_data = 0
    for keyboard in event.keyboards:
        for row in keyboard or []:
            for button in row if isinstance(row, list) else [row]:
                buttons += 1
                data = getattr(button, "data", None)
                if data:
                    longest_data = max(longest_data, len(data))
    return longest, buttons, longest_data


def fits(event, label, button_limit=BUTTON_LIMIT):
    longest, buttons, data_length = measure(event)
    check(f"{label} fits the message limit", longest < TEXT_LIMIT, f"chars={longest}")
    check(f"{label} fits the keyboard limit", buttons <= button_limit, f"buttons={buttons}")
    check(
        f"{label} keeps callback data short",
        data_length <= CALLBACK_LIMIT,
        f"bytes={data_length}",
    )


async def big_catalog(owner, alice, alice_row):
    for index in range(300):
        await products_db.create(
            slug=f"bulk-{index}",
            name=f"Bulk plan number {index}",
            emoji="📦",
            price_stars=100 + index,
            price_rub=200 + index,
            duration_days=30,
            is_active=1,
            sort_order=index,
        )

    fits(await flow.click(alice, "menu:catalog", label="customer catalog"), "customer catalog")
    fits(await flow.click(owner, "a:cat", label="admin catalog"), "admin catalog")
    fits(await flow.click(owner, "a:cat:5", label="admin catalog page six"), "catalog page six")
    fits(
        await flow.click(owner, f"a:oclone:{alice_row['id']}", label="copy from catalog"),
        "copy list",
    )
    fits(await flow.click(owner, "a:orders", label="orders queue"), "orders queue")
    fits(await flow.click(owner, "a:subs", label="subscriptions list"), "subscriptions list")


async def many_offers_and_clients(owner, alice_row):
    for index in range(120):
        await products_db.create(
            slug=f"offer-{index}",
            owner_user_id=alice_row["id"],
            name=f"Offer {index}",
            emoji="🎁",
            price_rub=100,
            duration_days=30,
            is_active=1,
        )
    fits(
        await flow.click(owner, f"a:uoffers:{alice_row['id']}", label="offers of one client"),
        "offers of one client",
    )

    for index in range(60):
        client = await users_db.get_or_create(
            900000 + index, f"client{index}", f"Client {index}"
        )
        await products_db.create(
            slug=f"vip-{index}",
            owner_user_id=client["id"],
            name=f"Vip {index}",
            emoji="🎁",
            price_rub=100,
            duration_days=30,
            is_active=1,
        )
    fits(await flow.click(owner, "a:offers", label="clients with offers"), "client list")


async def long_texts(owner, alice, alice_row):
    await products_db.update(
        1, description="Very long story. " * 400, instruction="Steps. " * 300
    )
    fits(
        await flow.click(alice, "prod:1", label="card with a huge description"), "product card"
    )

    order_id = await orders_db.create(
        user_id=alice_row["id"],
        product=await products_db.get(2),
        amount_stars=10,
        payment_method="stars",
        status=orders_db.PAID,
    )
    result = await billing.apply_payment(await orders_db.get(order_id))
    await subs_db.set_credentials(result["subscription"]["id"], "login and password " * 250)

    fits(
        await flow.click(
            alice, f"subdata:{result['subscription']['id']}", label="huge credentials"
        ),
        "access details",
    )
    fits(
        await flow.click(
            owner, f"a:sub:{result['subscription']['id']}", label="admin sub card"
        ),
        "admin subscription card",
    )

    await flow.click(owner, "a:cast")
    event = await flow.send_text(owner, "Sale! " * 900, label="huge broadcast")
    fits(event, "broadcast preview")
    return result["subscription"]["id"]


async def markup_typed_by_staff(alice):
    await products_db.update(2, name="Plan <b>Pro</b> & Co")
    event = await flow.click(alice, "prod:2", label="product name with markup")
    text = flow.produced(event)
    check(
        "a product name never leaks raw markup",
        "&lt;b&gt;Pro&lt;/b&gt;" in text and "&amp;" in text,
        text[:70],
    )


async def catching_up_after_downtime(alice_row, subscription_id):
    stale = await orders_db.create(
        user_id=alice_row["id"],
        product=await products_db.get(3),
        amount_rub=100,
        payment_method="transfer",
        status=orders_db.PENDING_RECEIPT,
    )
    # Older than the transfer deadline, which is counted in hours and no longer borrows
    # the one hour lifetime of a Stars invoice.
    await connection.execute(
        "UPDATE orders SET created_at = datetime('now', '-100 hours') WHERE id = ?", (stale,)
    )
    await connection.execute(
        "UPDATE subscriptions SET expires_at = ?, status = 'active' WHERE id = ?",
        (dates.to_sql(dates.utcnow() - timedelta(days=10)), subscription_id),
    )

    scheduler.set_client(flow.fake)
    await scheduler.run_checks()

    check(
        "an order abandoned during downtime is closed on startup",
        (await orders_db.get(stale))["status"] == orders_db.PAYMENT_EXPIRED,
    )
    check(
        "a subscription that ran out during downtime is closed on startup",
        (await subs_db.get(subscription_id))["status"] == subs_db.EXPIRED,
    )
    check(
        "the write ahead log is on, so a crash leaves the database readable",
        await connection.fetch_value("PRAGMA journal_mode") == "wal",
    )


async def run():
    await connection.connect()
    await settings.load()
    await users_db.ensure_owners([OWNER_ID])
    for item in DEMO_PRODUCTS:
        await products_db.create(owner_user_id=products_db.PUBLIC, is_active=1, **item)

    owner = flow.Sender(OWNER_ID, "boss", "Boss")
    alice = flow.Sender(ALICE_ID, "alice", "Alice")
    alice_row = await users_db.get_or_create(ALICE_ID, "alice", "Alice")

    print("\n-- a shop with 300 products", flush=True)
    await big_catalog(owner, alice, alice_row)
    print("\n-- many offers and many clients", flush=True)
    await many_offers_and_clients(owner, alice_row)
    print("\n-- long texts", flush=True)
    subscription_id = await long_texts(owner, alice, alice_row)
    print("\n-- markup typed by staff", flush=True)
    await markup_typed_by_staff(alice)
    print("\n-- catching up after downtime", flush=True)
    await catching_up_after_downtime(alice_row, subscription_id)

    await connection.disconnect()


def main():
    try:
        asyncio.run(flow._sandbox.run_and_close(run))
    finally:
        flow._sandbox.cleanup()

    problems.extend(flow.failures)
    print(f"\nproblems: {len(problems)}")
    for item in problems:
        print("  FAIL", item)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
