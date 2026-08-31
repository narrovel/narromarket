# Offline checks for the shop logic, no Telegram connection: python -m tools.selfcheck
#
# Runs against a throwaway database and verifies the rules that are easy to break:
# renewal dates, reminder queues, personal offers, payment options and access rights.

import asyncio
import os
import pathlib
import shutil
import sys
from datetime import timedelta

from tools import _sandbox  # noqa: F401  imported first, it sets DATABASE_PATH

from db import connection, invoices, journal, orders, products, refunds, requisites  # noqa: E402
from db import settings, stats, subscriptions, users  # noqa: E402
import config  # noqa: E402
from services import access, billing, scheduler  # noqa: E402
from utils import dates  # noqa: E402

_sandbox.guard_live_database()

results = []


async def migration_from_an_old_database() -> None:
    # Nothing else ever reaches _migrate: every check starts from a fresh file, which
    # takes the "stamp and return" branch. This builds a database the way the pre
    # migration code did, with data that violates the constraints added since, and
    # asserts the upgrade repairs rather than refuses.
    import sqlite3
    import tempfile

    directory = tempfile.mkdtemp(prefix="narromarket-migration-")
    path = pathlib.Path(directory) / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE, username TEXT, first_name TEXT,
            last_name TEXT, role TEXT NOT NULL DEFAULT 'user',
            is_blocked INTEGER NOT NULL DEFAULT 0, payment_method_id INTEGER, note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE products (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL,
            owner_user_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT 'X', price_stars INTEGER NOT NULL DEFAULT 0,
            price_rub REAL NOT NULL DEFAULT 0, duration_days INTEGER NOT NULL DEFAULT 30,
            short_description TEXT, description TEXT, instruction TEXT, image TEXT,
            is_active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (slug, owner_user_id));
        CREATE TABLE payment_methods (id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'sbp', details TEXT NOT NULL,
            bank TEXT, holder TEXT, is_default INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, product_id INTEGER, product_slug TEXT NOT NULL,
            product_name TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT 'X',
            is_personal INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
            credentials TEXT, started_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL, notified_3d INTEGER NOT NULL DEFAULT 0,
            notified_1d INTEGER NOT NULL DEFAULT 0,
            notified_expired INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            product_id INTEGER, product_slug TEXT NOT NULL, product_name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT 'X', subscription_id INTEGER,
            is_personal INTEGER NOT NULL DEFAULT 0, is_renewal INTEGER NOT NULL DEFAULT 0,
            duration_days INTEGER NOT NULL DEFAULT 30, amount_stars INTEGER NOT NULL DEFAULT 0,
            amount_rub REAL NOT NULL DEFAULT 0, payment_method TEXT NOT NULL DEFAULT 'stars',
            payment_charge_id TEXT, receipt_file TEXT,
            status TEXT NOT NULL DEFAULT 'pending_receipt', processed_by INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
            type TEXT NOT NULL, data TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL, action TEXT NOT NULL, target TEXT, details TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE INDEX idx_users_tg ON users (telegram_id);
        CREATE INDEX idx_invoices_token ON invoices (token);
        INSERT INTO users (telegram_id) VALUES (900);
        INSERT INTO products (slug, name, price_rub) VALUES ('m', 'M', 299.99);
        INSERT INTO orders (user_id, product_slug, product_name, payment_charge_id, status,
            amount_rub, processed_by) VALUES (1, 'm', 'M', 'dup', 'paid', 299.99, 7);
        INSERT INTO orders (user_id, product_slug, product_name, payment_charge_id, status,
            amount_rub) VALUES (1, 'm', 'M', 'dup', 'paid', 100.0);
        INSERT INTO subscriptions (user_id, product_slug, product_name, expires_at,
            notified_1d) VALUES (1, 'm', 'M', '2030-01-01 00:00:00', 1);
        INSERT INTO subscriptions (user_id, product_slug, product_name, expires_at,
            notified_1d) VALUES (1, 'm', 'M', '2029-01-01 00:00:00', 1);
        INSERT INTO invoices (telegram_id, product_id, token)
            VALUES (900, 1, 'legacy-pending');
        INSERT INTO audit_log (admin_id, action, target)
            VALUES (900, 'legacy_live_target', 'tg:900');
        INSERT INTO audit_log (admin_id, action, target)
            VALUES (919, 'legacy_orphan_target', 'tg:919');
        """
    )
    raw.commit()
    raw.close()

    original = connection.DATABASE_PATH
    connection.DATABASE_PATH = path
    try:
        await connection.connect()
        version = await connection.fetch_value("PRAGMA user_version")
        charges = [
            row["payment_charge_id"]
            for row in await connection.fetch_all(
                "SELECT payment_charge_id FROM orders ORDER BY id"
            )
        ]
        refund_recipients = [
            row["payment_recipient_id"]
            for row in await connection.fetch_all(
                "SELECT payment_recipient_id FROM orders ORDER BY id"
            )
        ]
        subs_rows = await connection.fetch_all(
            "SELECT status, expires_at, notified_3d, credentials FROM subscriptions ORDER BY id"
        )
        columns = {
            row["name"] for row in await connection.fetch_all("PRAGMA table_info(orders)")
        }
        invoice_columns = {
            row["name"] for row in await connection.fetch_all("PRAGMA table_info(invoices)")
        }
        refund_columns = {
            row["name"] for row in await connection.fetch_all("PRAGMA table_info(refunds)")
        }
        legacy_invoice = await connection.fetch_one(
            "SELECT * FROM invoices WHERE token = 'legacy-pending'"
        )
        indexes = {
            row["name"]
            for row in await connection.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        rubles = await connection.fetch_value("SELECT price_rub FROM products LIMIT 1")
        paid_at = await connection.fetch_value("SELECT paid_at FROM orders LIMIT 1")
        migrated_audit = {
            row["action"]: row
            for row in await connection.fetch_all(
                "SELECT admin_id, action, target FROM audit_log ORDER BY id"
            )
        }
        migrated_processor = await connection.fetch_value(
            "SELECT processed_by_telegram_id FROM orders WHERE id = 1"
        )
        integrity = await connection.fetch_value("PRAGMA integrity_check")
        await connection.disconnect()

        check(
            "an old database migrates instead of refusing to start",
            version == connection.SCHEMA_VERSION,
            str(version),
        )
        check(
            "duplicate charge ids are kept but made unique",
            charges == ["dup", "dup:dup2"],
            str(charges),
        )
        check(
            "legacy Stars orders retain the recipient needed for a refund",
            refund_recipients == [900, 900],
            str(refund_recipients),
        )
        check(
            "the longest of two overlapping subscriptions survives",
            [row["status"] for row in subs_rows] == ["active", "cancelled"],
            str([row["status"] for row in subs_rows]),
        )
        check(
            "the one that was closed loses its access details",
            subs_rows[1]["credentials"] is None,
        )
        check(
            "reminder flags are squared up so nobody is told twice",
            all(row["notified_3d"] == 1 for row in subs_rows),
        )
        check(
            "the renamed column arrives",
            "processed_by_telegram_id" in columns and "processed_by" not in columns,
        )
        check(
            "the new order columns arrive",
            {
                "paid_at",
                "reversed_at",
                "payment_method_id",
                "payment_provider_charge_id",
                "payment_recipient_id",
            }
            <= columns,
        )
        check(
            "invoice terms are added during migration",
            {
                "product_slug",
                "product_name",
                "emoji",
                "owner_user_id",
                "amount_stars",
                "duration_days",
                "currency",
                "terms_hash",
                "precheckout_approved_at",
            }
            <= invoice_columns,
        )
        check(
            "legacy pending invoices without an original snapshot are cancelled",
            legacy_invoice["status"] == "cancelled",
            legacy_invoice["status"],
        )
        check(
            "durable refund state is added during migration",
            {
                "order_id",
                "user_id",
                "telegram_charge_id",
                "provider_charge_id",
                "status",
                "attempts",
                "lease_token",
                "lease_expires_at",
                "last_error",
                "resolution",
            }
            <= refund_columns,
        )
        check("paid orders are backfilled with a payment date", paid_at is not None)
        check("rubles are rounded to whole numbers", rubles == 300, str(rubles))
        check(
            "the duplicate indexes are dropped",
            "idx_users_tg" not in indexes and "idx_invoices_token" not in indexes,
        )
        check(
            "the uniqueness rules are in place afterwards",
            {
                "idx_orders_charge",
                "idx_subs_active_slug",
                "idx_refunds_order",
                "idx_refunds_telegram_charge",
                "idx_refunds_provider_charge",
            }
            <= indexes,
        )
        check(
            "legacy Telegram audit targets become internal user references",
            migrated_audit["legacy_live_target"]["target"] == "user:1",
            str(migrated_audit["legacy_live_target"]),
        )
        check(
            "orphan Telegram identifiers are scrubbed during migration",
            migrated_audit["legacy_orphan_target"]["target"] is None
            and migrated_audit["legacy_orphan_target"]["admin_id"] == 0
            and migrated_processor is None,
            f"audit={migrated_audit['legacy_orphan_target']} processor={migrated_processor}",
        )
        check("and the file is still sound", integrity == "ok", str(integrity))

        # Second start must be a no op.
        await connection.connect()
        check(
            "a second start changes nothing",
            await connection.fetch_value("PRAGMA user_version") == connection.SCHEMA_VERSION,
        )
        await connection.disconnect()
    finally:
        connection.DATABASE_PATH = original
        shutil.rmtree(directory, ignore_errors=True)


async def privacy_migration_from_v6() -> None:
    # Exercise v7 directly: a v0 fixture proves the whole chain boots, but cannot seed
    # rows in the refund table that only appeared in v6.
    import sqlite3
    import tempfile

    directory = tempfile.mkdtemp(prefix="narromarket-privacy-migration-")
    path = pathlib.Path(directory) / "v6.db"
    raw = sqlite3.connect(path)
    raw.executescript(connection.SCHEMA)
    raw.executescript(
        """
        PRAGMA user_version = 6;
        INSERT INTO users (id, telegram_id, username) VALUES (1, 900, 'live');
        INSERT INTO products (id, slug, name) VALUES (1, 'privacy', 'Privacy');
        INSERT INTO orders (
            user_id, product_id, product_slug, product_name, status,
            processed_by_telegram_id
        ) VALUES (1, 1, 'privacy', 'Privacy', 'completed', 919);
        INSERT INTO audit_log (admin_id, action, target)
            VALUES (900, 'live_target', 'tg:900');
        INSERT INTO audit_log (admin_id, action, target)
            VALUES (919, 'orphan_target', 'tg:919');
        INSERT INTO refunds (telegram_id, source, reason, status)
            VALUES (919, 'admin', 'completed legacy refund', 'completed');
        INSERT INTO refunds (telegram_id, source, reason, status)
            VALUES (918, 'admin', 'unresolved legacy refund', 'pending');
        """
    )
    raw.commit()
    raw.close()

    original = connection.DATABASE_PATH
    connection.DATABASE_PATH = path
    try:
        await connection.connect()
        audit = {
            row["action"]: row
            for row in await connection.fetch_all(
                "SELECT admin_id, action, target FROM audit_log ORDER BY id"
            )
        }
        processor = await connection.fetch_value(
            "SELECT processed_by_telegram_id FROM orders LIMIT 1"
        )
        refund_ids = {
            row["status"]: row["telegram_id"]
            for row in await connection.fetch_all(
                "SELECT status, telegram_id FROM refunds ORDER BY id"
            )
        }
        check(
            "v7 replaces legacy Telegram audit targets and orphan actor ids",
            await connection.fetch_value("PRAGMA user_version") == connection.SCHEMA_VERSION
            and audit["live_target"]["target"] == "user:1"
            and audit["orphan_target"]["target"] is None
            and audit["orphan_target"]["admin_id"] == 0
            and processor is None,
            f"audit={audit} processor={processor}",
        )
        check(
            "v7 scrubs completed refund ids but preserves unresolved ones",
            refund_ids == {"completed": 0, "pending": 918},
            str(refund_ids),
        )
    finally:
        await connection.disconnect()
        connection.DATABASE_PATH = original
        shutil.rmtree(directory, ignore_errors=True)


async def _transaction_still_works() -> bool:
    try:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO settings (key, value) VALUES ('probe', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
        return True
    except Exception:
        return False


def _inside_local_day(offset_days: int):
    # Middle of that local day, so the fixture sits inside the reminder window at any
    # hour of the day and in any timezone.
    start, _ = dates.day_bounds_utc(offset_days)
    return dates.parse(start) + timedelta(hours=12)


def check(name: str, condition, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")


async def run() -> None:
    await migration_from_an_old_database()
    await privacy_migration_from_v6()

    await connection.connect()
    await settings.load()

    await users.ensure_owners([111])
    owner = await users.get(111)
    check("owner from env gets the owner role", owner and owner["role"] == "owner")

    customer = await users.get_or_create(222, "client", "Ann")
    staffer = await users.get_or_create(333, "manager", "Max")
    await users.set_role(staffer["id"], "manager")
    staffer = await users.get(333)

    check("manager can open orders", access.can(staffer, "orders"))
    check("manager cannot edit the catalog", not access.can(staffer, "catalog"))
    check("owner can edit the catalog", access.can(owner, "catalog"))
    check("owner role cannot be reassigned", not access.can_assign_role(owner, owner, "user"))
    check("owner can grant manager", access.can_assign_role(owner, customer, "manager"))

    product_id = await products.create(
        slug="music",
        name="Music",
        emoji="🎧",
        price_stars=200,
        price_rub=300,
        duration_days=30,
        is_active=1,
    )
    product = await products.get(product_id)

    order_id = await orders.create(
        user_id=customer["id"],
        product=product,
        amount_stars=200,
        payment_method="stars",
        status=orders.PAID,
    )
    first = await billing.apply_payment(await orders.get(order_id))
    first_expiry = dates.parse(first["subscription"]["expires_at"])
    days = (first_expiry - dates.utcnow()).days
    check("new subscription lasts 30 days", days in (29, 30), f"days={days}")
    check("first payment is not a renewal", first["renewed"] is False)

    repeat = await billing.apply_payment(await orders.get(order_id))
    check(
        "applying the same order twice changes nothing",
        dates.parse(repeat["subscription"]["expires_at"]) == first_expiry,
    )

    renew_id = await orders.create(
        user_id=customer["id"],
        product=product,
        amount_stars=200,
        payment_method="stars",
        status=orders.PAID,
        is_renewal=True,
    )
    renewed = await billing.apply_payment(await orders.get(renew_id))
    second_expiry = dates.parse(renewed["subscription"]["expires_at"])
    check(
        "paying early adds days to the current expiry",
        second_expiry == first_expiry + timedelta(days=30),
        f"{first_expiry} -> {second_expiry}",
    )
    check(
        "renewal reuses the subscription",
        renewed["subscription"]["id"] == first["subscription"]["id"],
    )

    subscription_id = first["subscription"]["id"]
    await connection.execute(
        "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
        (dates.to_sql(dates.utcnow() - timedelta(days=5)), subscription_id),
    )
    late_id = await orders.create(
        user_id=customer["id"],
        product=product,
        amount_stars=200,
        payment_method="stars",
        status=orders.PAID,
        is_renewal=True,
    )
    late = await billing.apply_payment(await orders.get(late_id))
    late_expiry = dates.parse(late["subscription"]["expires_at"])
    check(
        "an expired subscription restarts from today",
        abs((late_expiry - dates.utcnow()).days - 29) <= 1,
        f"expiry={late_expiry}",
    )

    await connection.execute(
        "UPDATE subscriptions SET expires_at = ?, notified_1d = 0 WHERE id = ?",
        (dates.to_sql(_inside_local_day(1)), subscription_id),
    )
    check(
        "subscription expiring tomorrow is queued",
        len(await subscriptions.expiring_in(1, "notified_1d")) == 1,
    )
    await subscriptions.mark_notified(subscription_id, "notified_1d")
    check(
        "reminder is not repeated", len(await subscriptions.expiring_in(1, "notified_1d")) == 0
    )

    await connection.execute(
        "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
        (dates.to_sql(dates.utcnow() - timedelta(minutes=5)), subscription_id),
    )
    check("expired subscription is found", len(await subscriptions.expired()) == 1)
    await subscriptions.set_status(subscription_id, subscriptions.EXPIRED)
    check("closed subscription leaves the queue", len(await subscriptions.expired()) == 0)
    check(
        "expired subscription is not active",
        len(await subscriptions.active_for_user(customer["id"])) == 0,
    )

    personal_id = await products.create(
        slug="music",
        owner_user_id=customer["id"],
        name="Music VIP",
        emoji="🎧",
        price_stars=100,
        price_rub=150,
        duration_days=30,
        is_active=1,
    )
    check(
        "personal offer hides the public product",
        all(
            item["slug"] != "music"
            for item in await products.public_catalog_for(customer["id"])
        ),
    )
    check("personal offer is listed", len(await products.list_personal(customer["id"])) == 1)
    check("clients with offers are listed", len(await products.owners()) == 1)

    for delivered in (order_id, renew_id, late_id):
        await orders.set_status(delivered, orders.DELIVERED)
    check(
        "delivered order does not block a renewal",
        await orders.open_for_product(customer["id"], "music") is None,
    )

    pending = await orders.create(
        user_id=customer["id"],
        product=product,
        amount_rub=300,
        payment_method="transfer",
        status=orders.PENDING_RECEIPT,
    )
    check(
        "unpaid order blocks a second purchase",
        await orders.open_for_product(customer["id"], "music") is not None,
    )
    await orders.set_status(pending, orders.CANCELLED)
    check(
        "cancelled order stops blocking",
        await orders.open_for_product(customer["id"], "music") is None,
    )

    check("stars payment is available", billing.can_pay_stars(product))
    check(
        "no transfer without assigned details",
        await billing.transfer_method(customer, product) is None,
    )
    method_id = await requisites.create("Main", "sbp", "+70000000000", "Bank", "Owner")
    check(
        "first payment method becomes default",
        (await requisites.get_default())["id"] == method_id,
    )
    await users.set_payment_method(customer["id"], method_id)
    customer = await users.get(222)
    check(
        "assigned details unlock transfer",
        await billing.transfer_method(customer, product) is not None,
    )
    check("personal price wins", billing.rub_price(await products.get(personal_id)) == 150)

    first_invoice = await invoices.create(222, product, "token-1")
    blocked_invoice = await invoices.create(222, product, "token-2")
    check(
        "one live invoice blocks another", first_invoice is not None and blocked_invoice is None
    )
    await connection.execute(
        "UPDATE invoices SET created_at = datetime('now', '-2 hours') WHERE token = ?",
        ("token-1",),
    )
    await invoices.create(222, product, "token-2")
    check(
        "previous invoice is cancelled",
        (await invoices.get("token-1"))["status"] == "cancelled",
    )
    frozen = await invoices.get("token-2")
    check(
        "an invoice freezes product amount and duration",
        frozen["amount_stars"] == 200
        and frozen["duration_days"] == 30
        and frozen["product_slug"] == "music"
        and frozen["product_name"] == "Music",
        str(frozen),
    )
    check("a pending invoice can be claimed once", await invoices.claim_for_payment("token-2"))
    check("paid invoice is marked", (await invoices.get("token-2"))["status"] == "paid")
    check(
        "a second claim on the same invoice loses",
        not await invoices.claim_for_payment("token-2"),
    )
    check(
        "a cancelled invoice cannot be claimed",
        not await invoices.claim_for_payment("token-1"),
    )
    await invoices.create(222, product, "old-approved-token")
    await invoices.approve_precheckout("old-approved-token")
    await connection.execute(
        "UPDATE invoices SET precheckout_approved_at = datetime('now', ?) WHERE token = ?",
        (
            f"-{invoices.APPROVED_GRACE_MINUTES + 1} minutes",
            "old-approved-token",
        ),
    )
    check(
        "an abandoned pre-checkout approval eventually stops being payable",
        not await invoices.claim_for_payment("old-approved-token"),
    )
    replacement = await invoices.create(222, product, "after-approved-timeout")
    check(
        "an abandoned approval no longer blocks a fresh invoice",
        replacement is not None
        and (await invoices.get("old-approved-token"))["status"] == invoices.CANCELLED,
    )
    await invoices.cancel("after-approved-timeout")

    await settings.set_value("star_to_rub", "2")
    await settings.load()
    check("settings survive a reload", settings.get_float("star_to_rub") == 2.0)

    dashboard = await stats.dashboard()
    check("dashboard counts users", dashboard["users"] == 3, f"users={dashboard['users']}")
    check(
        "dashboard counts revenue",
        dashboard["revenue_stars"] == 600,
        f"stars={dashboard['revenue_stars']}",
    )

    # refunds and cancellations take the granted period back
    buyer = await users.get_or_create(444, "buyer", "<b>Bob")
    check("customer names are escaped for html", "&lt;b&gt;Bob" in users.display_name(buyer))

    plan_id = await products.create(
        slug="plan", name="Plan", emoji="📦", price_stars=100, duration_days=30, is_active=1
    )
    plan = await products.get(plan_id)

    snapshot_id = await products.create(
        slug="snapshot-original",
        name="Original",
        emoji="🧊",
        price_stars=17,
        duration_days=17,
        is_active=1,
    )
    snapshot_product = await products.get(snapshot_id)
    snapshot_order = await orders.create(
        user_id=buyer["id"],
        product=snapshot_product,
        amount_stars=17,
        payment_method="stars",
        status=orders.PAID,
    )
    await products.update(
        snapshot_id,
        slug="snapshot-edited",
        name="Edited",
        emoji="🔥",
        duration_days=99,
    )
    snapshot_result = await billing.apply_payment(await orders.get(snapshot_order))
    snapshot_subscription = snapshot_result["subscription"]
    snapshot_days = (
        dates.parse(snapshot_subscription["expires_at"]) - dates.utcnow()
    ).total_seconds() / 86400
    check(
        "fulfilment uses the order snapshot after the catalog changes",
        snapshot_subscription["product_slug"] == "snapshot-original"
        and snapshot_subscription["product_name"] == "Original"
        and snapshot_subscription["emoji"] == "🧊"
        and 16.9 <= snapshot_days <= 17.1,
        str(snapshot_subscription),
    )

    buy_id = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_stars=100,
        payment_method="stars",
        status=orders.PAID,
    )
    bought = await billing.apply_payment(await orders.get(buy_id))
    bought_until = dates.parse(bought["subscription"]["expires_at"])
    await orders.set_status(buy_id, orders.DELIVERED)

    renew_id = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_stars=100,
        payment_method="stars",
        status=orders.PAID,
        is_renewal=True,
    )
    renewed = await billing.apply_payment(await orders.get(renew_id))
    check(
        "a renewal adds its period to the current date",
        dates.parse(renewed["subscription"]["expires_at"]) == bought_until + timedelta(days=30),
        f"until={renewed['subscription']['expires_at']}",
    )

    # Refunding one order removes only that order's period from the subscription.
    rolled = await billing.revoke_payment(await orders.get(buy_id))
    check(
        "refunding a first purchase keeps the days a renewal paid for",
        rolled["status"] == "active" and dates.parse(rolled["expires_at"]) == bought_until,
        f"status={rolled['status']} until={rolled['expires_at']}",
    )

    await subscriptions.set_credentials(rolled["id"], "login:secret")
    closed = await billing.revoke_payment(await orders.get(renew_id))
    check(
        "refunding the last remaining order closes the subscription",
        closed["status"] == "cancelled",
        f"status={closed['status']}",
    )
    check("closing a subscription drops the access details", closed["credentials"] is None)
    check(
        "a stale close cannot rewrite a subscription that is already closed",
        await billing.close_subscription(closed["id"], subscriptions.EXPIRED) is None
        and (await subscriptions.get(closed["id"]))["status"] == subscriptions.CANCELLED,
    )
    check(
        "revoking an order without a subscription is a no-op",
        await billing.revoke_payment(await orders.get(pending)) is None,
    )

    # --- the primitives everything else rests on -------------------------------
    claimable = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_stars=100,
        payment_method="stars",
        status=orders.PENDING_REVIEW,
    )
    check(
        "claim_status wins once",
        await orders.claim_status(claimable, orders.PAID, (orders.PENDING_REVIEW,)),
    )
    check(
        "the second claim on the same order loses",
        not await orders.claim_status(claimable, orders.PAID, (orders.PENDING_REVIEW,)),
    )
    check(
        "being claimed as paid stamps paid_at",
        (await orders.get(claimable))["paid_at"] is not None,
    )
    # Closed again so it stops blocking the product for the checks further down.
    await orders.set_status(claimable, orders.COMPLETED)

    async def request_same_refund():
        return await refunds.create(
            telegram_id=444,
            source="automatic",
            reason="idempotency probe",
            telegram_charge_id="refund-probe-charge",
            provider_charge_id="refund-probe-provider",
            amount_stars=10,
        )

    requested = await asyncio.gather(request_same_refund(), request_same_refund())
    refund_id = requested[0]["id"]
    check(
        "concurrent refund requests create one durable obligation",
        requested[1]["id"] == refund_id
        and await connection.fetch_value(
            "SELECT COUNT(*) FROM refunds WHERE provider_charge_id = ?",
            ("refund-probe-provider",),
            0,
        )
        == 1,
    )
    leases = await asyncio.gather(refunds.claim(refund_id), refunds.claim(refund_id))
    lease = next((value for value in leases if value), None)
    check(
        "only one worker claims a refund",
        lease is not None and sum(bool(value) for value in leases) == 1,
        str(leases),
    )
    check(
        "a wrong lease cannot requeue a refund",
        not await refunds.mark_failed(refund_id, "wrong-lease", "wrong worker"),
    )
    check("a fresh processing refund cannot be reclaimed", not await refunds.claim(refund_id))
    await connection.execute(
        "UPDATE refunds SET lease_expires_at = datetime('now', '-1 second') WHERE id = ?",
        (refund_id,),
    )
    reclaimed = await asyncio.gather(refunds.claim(refund_id), refunds.claim(refund_id))
    new_lease = next((value for value in reclaimed if value), None)
    check(
        "an abandoned refund is reclaimed once",
        new_lease is not None and sum(bool(value) for value in reclaimed) == 1,
        str(reclaimed),
    )
    check(
        "an expired worker cannot complete a reclaimed refund",
        not await refunds.mark_completed(refund_id, lease),
    )
    check(
        "the current worker completes the refund",
        await refunds.mark_completed(refund_id, new_lease),
    )
    check("a completed refund is never claimed again", not await refunds.claim(refund_id))

    before = (await stats.dashboard())["revenue_rub"]
    unconfirmed = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_rub=777,
        payment_method="transfer",
        status=orders.PENDING_REVIEW,
    )
    await orders.claim_status(unconfirmed, orders.PROBLEM, (orders.PENDING_REVIEW,))
    check(
        "an order nobody confirmed is not revenue",
        (await stats.dashboard())["revenue_rub"] == before,
        f"before={before} after={(await stats.dashboard())['revenue_rub']}",
    )

    try:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO settings (key, value) VALUES ('rollback_probe', '1')"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check(
        "a failed transaction leaves nothing behind",
        await connection.fetch_one("SELECT 1 FROM settings WHERE key = 'rollback_probe'")
        is None,
    )

    await connection.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('visibility_probe', 'before')"
    )
    uncommitted_written = asyncio.Event()
    release_rollback = asyncio.Event()

    async def paused_rollback():
        try:
            async with connection.transaction():
                await connection.execute(
                    "UPDATE settings SET value = 'temporary' WHERE key = 'visibility_probe'"
                )
                uncommitted_written.set()
                await release_rollback.wait()
                raise RuntimeError("rollback visibility probe")
        except RuntimeError:
            pass

    rollback_task = asyncio.create_task(paused_rollback())
    await uncommitted_written.wait()
    outside_read = asyncio.create_task(
        connection.fetch_value("SELECT value FROM settings WHERE key = 'visibility_probe'")
    )
    await asyncio.sleep(0)
    check(
        "an outside reader waits for an open write transaction",
        not outside_read.done(),
    )
    release_rollback.set()
    await rollback_task
    check(
        "a reader never sees a transaction that rolled back",
        await outside_read == "before",
    )

    inherited_read = None
    try:
        async with connection.transaction():
            await connection.execute(
                "UPDATE settings SET value = 'child-temporary' WHERE key = 'visibility_probe'"
            )
            inherited_read = asyncio.create_task(
                connection.fetch_value(
                    "SELECT value FROM settings WHERE key = 'visibility_probe'"
                )
            )
            await asyncio.sleep(0)
            check(
                "a child task cannot inherit permission to read an open transaction",
                not inherited_read.done(),
            )
            raise RuntimeError("child context rollback probe")
    except RuntimeError:
        pass
    check(
        "an inherited-context reader sees the rolled-back value",
        inherited_read is not None and await inherited_read == "before",
    )

    write_failed = False
    try:
        await connection.execute("INSERT INTO users (telegram_id) VALUES (222)")
    except Exception:
        write_failed = True
    check(
        "a failed write does not poison the connection",
        write_failed and await _transaction_still_works(),
    )

    # --- settings that can brick a screen or the scheduler ---------------------
    for key, bad in (
        ("check_hour", "24"),
        ("check_hour", "10.9"),
        ("catalog_per_page", "500"),
        ("monthly_report_day", "0"),
        ("star_to_rub", "abc"),
    ):
        value, error = settings.validate(key, bad)
        check(f"settings refuses {key}={bad}", error is not None and value is None, str(error))
    for key, good in (("check_hour", "23"), ("catalog_per_page", "6")):
        value, error = settings.validate(key, good)
        check(f"settings accepts {key}={good}", error is None and value == good)
    check(
        "a malformed boolean setting is refused",
        settings.validate("require_username", "maybe")[1] is not None,
    )
    check(
        "a readable boolean setting is normalized",
        settings.validate("transfer_for_all", "yes") == ("1", None),
    )
    check(
        "a manager username with a space is refused",
        settings.validate("manager_username", "John Smith")[1] is not None,
    )
    check(
        "a normal manager username is accepted",
        settings.validate("manager_username", "@shop_manager") == ("shop_manager", None),
    )
    await connection.execute(
        "INSERT INTO settings (key, value) VALUES ('check_hour', '1e309') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    await settings.load()
    check(
        "a corrupt stored number falls back instead of stopping the scheduler",
        settings.get("check_hour") == settings.DEFAULTS["check_hour"]
        and settings.get_int("check_hour", 10) == 10,
    )
    await settings.set_value("check_hour", settings.DEFAULTS["check_hour"])

    probe_name = "NARROMARKET_LIMIT_PROBE"
    old_errors = len(config._bad_values)
    os.environ[probe_name] = "999999999999999999999"
    try:
        limited = config._int_env(probe_name, 7, minimum=1, maximum=10)
        check(
            "an environment duration above its limit uses the safe default",
            limited == 7 and config._bad_values[old_errors:] == [probe_name],
        )
    finally:
        os.environ.pop(probe_name, None)
        del config._bad_values[old_errors:]

    job_started = asyncio.Event()
    finish_job = asyncio.Event()
    job_finished = asyncio.Event()

    async def shutdown_probe():
        job_started.set()
        await finish_job.wait()
        job_finished.set()

    scheduled_job = asyncio.create_task(scheduler._run_scheduled(shutdown_probe))
    await job_started.wait()
    scheduled_job.cancel()
    await asyncio.sleep(0)
    check(
        "scheduler cancellation lets in-flight maintenance drain",
        not scheduled_job.done() and not job_finished.is_set(),
    )
    finish_job.set()
    await scheduled_job
    check("drained scheduler maintenance finishes cleanly", job_finished.is_set())

    # --- malformed HTML handling -------------------------------------------------
    from utils import texts as texts_mod

    check(
        "a bare ampersand is escaped",
        texts_mod.html_safe("Order for R&D") == "Order for R&amp;D",
    )
    check(
        "an existing entity is left alone",
        texts_mod.html_safe("a &amp; b &#39;") == "a &amp; b &#39;",
    )
    check(
        "a link with a query string keeps its tail",
        texts_mod.clamp("open https://x.io/?a=1&b=2 now").endswith("now"),
    )

    # --- roles come from .env, in both directions ------------------------------
    stranger = await users.get_or_create(4242, "stranger")
    await users.set_role(stranger["id"], "owner")
    await users.ensure_owners([444])
    check(
        "an owner missing from OWNER_IDS loses staff access",
        (await users.get(4242))["role"] == "user",
        (await users.get(4242))["role"],
    )
    await users.set_blocked((await users.get(444))["id"], True)
    await users.ensure_owners([444])
    check(
        "an owner listed in OWNER_IDS is unblocked",
        not (await users.get(444))["is_blocked"],
    )

    # --- deleted payment details stay readable for old orders -------------------
    doomed = await requisites.create(title="Old", kind="card", details="1111")
    await requisites.delete(doomed)
    check(
        "deleted details disappear from the list",
        all(item["id"] != doomed for item in await requisites.list_all()),
    )
    check(
        "deleted details are still readable by id", (await requisites.get(doomed)) is not None
    )
    check(
        "a client assigned to them falls back to the default",
        (await requisites.for_user({"payment_method_id": doomed})) is not None,
    )
    current_default = await requisites.get_default()
    await requisites.update(current_default["id"], is_active=0)
    replacement = await requisites.create(title="Replacement", kind="card", details="2222")
    check(
        "a new active payment method fills an empty default slot",
        (await requisites.get_default())["id"] == replacement,
    )

    # --- which statuses block a second purchase, on a product of its own ---------
    block_id = await products.create(
        slug="blocking",
        name="Blocking",
        emoji="B",
        price_stars=10,
        duration_days=30,
        is_active=1,
    )
    block_product = await products.get(block_id)
    blocker = await orders.create(
        user_id=buyer["id"],
        product=block_product,
        amount_stars=10,
        payment_method="stars",
        status=orders.PAID,
    )
    for status in (
        orders.PAID,
        orders.PENDING_REVIEW,
        orders.PENDING_RECEIPT,
        orders.PROBLEM,
        orders.REFUND_PENDING,
    ):
        await orders.set_status(blocker, status)
        check(
            f"an order in {status} blocks a second purchase of the same product",
            await orders.open_for_product(buyer["id"], "blocking") is not None,
        )
    for status in (orders.COMPLETED, orders.REFUNDED, orders.DELIVERED):
        await orders.set_status(blocker, status)
        check(
            f"an order in {status} does not",
            await orders.open_for_product(buyer["id"], "blocking") is None,
        )

    # --- staff who cannot act are not notified ------------------------------------
    notified = await users.get_or_create(6300, "onduty")
    await users.set_role(notified["id"], "manager")
    check("a manager is on the notification list", 6300 in await users.staff_telegram_ids())
    await users.set_blocked(notified["id"], True)
    check("a blocked one is not", 6300 not in await users.staff_telegram_ids())
    await users.set_blocked(notified["id"], False)

    # --- a stale page shows the page that exists, not an empty one ----------------
    from handlers.admin import base as admin_base

    items = list(range(admin_base.PAGE_SIZE * 2))
    check(
        "a page past the end falls back to the last one",
        admin_base.page_slice(items, 99) == items[admin_base.PAGE_SIZE :],
    )
    check(
        "and a negative one to the first",
        admin_base.page_slice(items, -5) == items[: admin_base.PAGE_SIZE],
    )

    # --- prices, payment options and the details a customer is quoted ------------
    check(
        "a price is rounded, never truncated",
        billing.rub_price({"price_rub": 299.6}) == 300,
        str(billing.rub_price({"price_rub": 299.6})),
    )
    check("and a whole price is left alone", billing.rub_price({"price_rub": 300}) == 300)

    stranger_row = await users.get_or_create(6100, "stranger2")
    await settings.set_value("transfer_for_all", "0")
    check(
        "without assigned details and with the setting off there is no transfer",
        await billing.transfer_method(stranger_row, {"price_rub": 100}) is None,
    )
    await settings.set_value("transfer_for_all", "1")
    check(
        "the transfer for everyone setting actually opens it",
        await billing.transfer_method(stranger_row, {"price_rub": 100}) is not None,
    )
    await settings.set_value("transfer_for_all", "0")

    spare = await requisites.create(title="Spare", kind="card", details="2222")
    await users.set_payment_method(stranger_row["id"], spare)
    stranger_row = await users.get_by_id(stranger_row["id"])
    await requisites.update(spare, is_active=0)
    quoted = await requisites.for_user(stranger_row)
    check(
        "a deactivated account is never quoted to a customer",
        quoted is None or quoted["id"] != spare,
        str(quoted and quoted["id"]),
    )

    # --- the constraints that stop one payment becoming two ----------------------
    import sqlite3 as _sqlite3

    charged = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_stars=10,
        payment_method="stars",
        status=orders.PAID,
        payment_charge_id="only-once",
    )
    duplicated = False
    try:
        await orders.create(
            user_id=buyer["id"],
            product=plan,
            amount_stars=10,
            payment_method="stars",
            status=orders.PAID,
            payment_charge_id="only-once",
        )
        duplicated = True
    except _sqlite3.IntegrityError:
        pass
    check("one Telegram charge can pay for one order only", not duplicated)
    await orders.set_status(charged, orders.COMPLETED)

    twin_user = await users.get_or_create(6200, "twin")
    await subscriptions.create(twin_user["id"], plan, dates.utcnow() + timedelta(days=5), False)
    twinned = False
    try:
        await subscriptions.create(
            twin_user["id"], plan, dates.utcnow() + timedelta(days=9), False
        )
        twinned = True
    except _sqlite3.IntegrityError:
        pass
    check("and one customer cannot hold the same product twice", not twinned)

    # --- what the customer is actually shown --------------------------------------
    from utils import texts as texts_check

    # Cut exactly where a tag opens: clamp keeps limit - 24 characters, so the 76th
    # character here is the "<" of the tag.
    tagged = "x" * 75 + "<blockquote>tail</blockquote>" + "y" * 200
    cut = texts_check.clamp(tagged, 100)
    check(
        "truncation never ends inside a tag", cut.count("<") == cut.count(">"), repr(cut[70:85])
    )
    entity = "y" * 75 + "&amp;" + "z" * 200
    cut = texts_check.clamp(entity, 100)
    check("nor inside an html entity", cut.count("&") == cut.count(";"), repr(cut[70:85]))
    check(
        "an emoji costs two characters, the way Telegram counts",
        texts_check._utf16_len("🙂" * 10) == 20,
    )
    check("so an emoji wall is still truncated", len(texts_check.clamp("🙂" * 3000)) < 3000)

    # --- the monthly figure has to account for every rouble that moved -----------
    baseline = await stats.revenue_for_month(0)
    month_buyer = await users.get_or_create(6001, "month")
    kept = await orders.create(
        user_id=month_buyer["id"],
        product=plan,
        amount_rub=500,
        payment_method="transfer",
        status=orders.PENDING_REVIEW,
    )
    await orders.claim_status(kept, orders.PAID, (orders.PENDING_REVIEW,))
    taken_back = await orders.create(
        user_id=month_buyer["id"],
        product=block_product,
        amount_rub=700,
        payment_method="transfer",
        status=orders.PENDING_REVIEW,
    )
    await orders.claim_status(taken_back, orders.PAID, (orders.PENDING_REVIEW,))
    await orders.claim_status(taken_back, orders.REFUNDED, (orders.PAID,))
    await billing.revoke_payment(await orders.get(taken_back))

    month = await stats.revenue_for_month(0)
    check(
        "this month's collected payments are counted",
        month["rub"] - baseline["rub"] == 1200,
        f"{baseline['rub']} -> {month['rub']}",
    )
    check(
        "money that was taken and given back is shown, not silently dropped",
        month["reversed_rub"] - baseline["reversed_rub"] == 700
        and month["reversed_orders"] - baseline["reversed_orders"] == 1,
        str(month),
    )
    check(
        "and the gross order count includes every payment",
        month["orders"] - baseline["orders"] == 2,
        f"{baseline['orders']} -> {month['orders']}",
    )
    check(
        "net cash movement subtracts reversals in the same month",
        month["net_rub"] - baseline["net_rub"] == 500,
        str(month),
    )
    check("last month has nothing in it", (await stats.revenue_for_month(-1))["rub"] == 0)

    # --- and the crash states have somewhere to be seen --------------------------
    stranded = await orders.create(
        user_id=month_buyer["id"],
        product=plan,
        amount_stars=10,
        payment_method="stars",
        status=orders.PENDING_REVIEW,
    )
    await orders.claim_status(stranded, orders.PAID, (orders.PENDING_REVIEW,))
    found = await orders.inconsistencies()
    check(
        "an order that was paid but granted nothing is reported",
        stranded in [row["id"] for row in found["paid_without_subscription"]],
        str(found["paid_without_subscription"]),
    )
    await orders.set_status(stranded, orders.COMPLETED)
    await connection.execute("UPDATE orders SET subscription_id = 1 WHERE id = ?", (stranded,))

    # --- access handed over without a payment ------------------------------------
    gifted_to = await users.get_or_create(7100, "gifted")
    gift_product_id = await products.create(
        slug="gift",
        name="Gift",
        emoji="G",
        price_stars=100,
        price_rub=400,
        duration_days=30,
        is_active=1,
    )
    gift_product = await products.get(gift_product_id)
    revenue_before = (await stats.dashboard())["revenue_rub"]

    granted = await billing.grant_subscription(gifted_to["id"], gift_product, 45)
    check(
        "a grant creates a running subscription",
        granted is not None and granted["status"] == subscriptions.ACTIVE,
    )
    check(
        "for exactly the days asked for",
        abs((dates.parse(granted["expires_at"]) - dates.utcnow()).days - 44) <= 1,
        granted["expires_at"],
    )
    check("and it is not income", (await stats.dashboard())["revenue_rub"] == revenue_before)

    gift_orders = [
        row
        for row in await orders.list_for_user(gifted_to["id"], limit=5)
        if row["payment_method"] == "grant"
    ]
    check("a zero amount order is written alongside", len(gift_orders) == 1, str(gift_orders))
    check("with no payment date on it", gift_orders[0]["paid_at"] is None)
    check(
        "and it points at what was granted", gift_orders[0]["subscription_id"] == granted["id"]
    )
    check(
        "so the gift never looks like access nobody paid for",
        not await connection.fetch_one(
            "SELECT 1 FROM subscriptions s WHERE s.id = ? AND NOT EXISTS "
            "(SELECT 1 FROM orders o WHERE o.subscription_id = s.id)",
            (granted["id"],),
        ),
    )
    check(
        "a closed grant order does not block buying the product properly",
        await orders.open_for_product(gifted_to["id"], "gift") is None,
    )
    check(
        "granting the same product twice is refused",
        await billing.grant_subscription(gifted_to["id"], gift_product, 10) is None,
    )

    # Fulfilment rechecks the recipient inside the write transaction. A stale paid order
    # or grant form must not recreate access after erasure, or grant it to a blocked user.
    erased_recipient = await users.get_or_create(7120, "erased-recipient")
    await users.erase(erased_recipient["id"])
    erased_order = await orders.create(
        user_id=erased_recipient["id"],
        product=plan,
        amount_stars=10,
        payment_method="stars",
        status=orders.PAID,
    )
    erased_payment_refused = False
    try:
        await billing.apply_payment(await orders.get(erased_order))
    except billing.UserNotEligibleError:
        erased_payment_refused = True
    erased_grant_refused = False
    try:
        await billing.grant_subscription(erased_recipient["id"], gift_product, 10)
    except billing.UserNotEligibleError:
        erased_grant_refused = True
    check(
        "fulfilment and grants refuse an erased recipient",
        erased_payment_refused
        and erased_grant_refused
        and not await subscriptions.active_for_user(erased_recipient["id"]),
    )

    blocked_recipient = await users.get_or_create(7121, "blocked-recipient")
    await users.set_blocked(blocked_recipient["id"], True)
    blocked_order = await orders.create(
        user_id=blocked_recipient["id"],
        product=plan,
        amount_stars=10,
        payment_method="stars",
        status=orders.PAID,
    )
    blocked_payment_refused = False
    try:
        await billing.apply_payment(await orders.get(blocked_order))
    except billing.UserNotEligibleError:
        blocked_payment_refused = True
    blocked_grant_refused = False
    try:
        await billing.grant_subscription(blocked_recipient["id"], gift_product, 10)
    except billing.UserNotEligibleError:
        blocked_grant_refused = True
    check(
        "fulfilment and grants refuse a blocked recipient",
        blocked_payment_refused
        and blocked_grant_refused
        and not await subscriptions.active_for_user(blocked_recipient["id"]),
    )

    # --- erasing a customer on request -----------------------------------------
    refused_states = []
    for offset, status in enumerate(
        (
            orders.PENDING_REVIEW,
            orders.PAID,
            orders.DELIVERED,
            orders.PROBLEM,
            orders.REFUND_PENDING,
        )
    ):
        telegram_id = 5160 + offset
        protected = await users.get_or_create(telegram_id, f"protected-{offset}")
        protected_order = await orders.create(
            user_id=protected["id"],
            product=plan,
            amount_rub=900,
            payment_method="transfer",
            status=status,
        )
        if status == orders.PENDING_REVIEW:
            await orders.update(protected_order, receipt_file="review-evidence.jpg")
        try:
            await users.erase(protected["id"])
        except users.EraseBlockedError:
            current_user = await users.get_by_id(protected["id"])
            current_order = await orders.get(protected_order)
            intact = (
                current_user["telegram_id"] == telegram_id
                and current_order["status"] == status
                and (
                    status != orders.PENDING_REVIEW
                    or current_order["receipt_file"] == "review-evidence.jpg"
                )
            )
            if intact:
                refused_states.append(status)
    check(
        "erasure atomically refuses every unresolved order state",
        refused_states
        == [
            orders.PENDING_REVIEW,
            orders.PAID,
            orders.DELIVERED,
            orders.PROBLEM,
            orders.REFUND_PENDING,
        ],
        str(refused_states),
    )

    awaiting_receipt = await users.get_or_create(5170, "awaiting-receipt")
    awaiting_order = await orders.create(
        user_id=awaiting_receipt["id"],
        product=plan,
        amount_rub=900,
        payment_method="transfer",
        status=orders.PENDING_RECEIPT,
    )
    await orders.update(awaiting_order, receipt_file="early-receipt.jpg")
    awaiting_result = await users.erase(awaiting_receipt["id"])
    awaiting_after = await orders.get(awaiting_order)
    check(
        "a receipt still awaiting submission can be cancelled during erasure",
        awaiting_after["status"] == orders.CANCELLED
        and awaiting_after["receipt_file"] == "early-receipt.jpg"
        and awaiting_result["receipts"]
        == [{"order_id": awaiting_order, "receipt_file": "early-receipt.jpg"}],
    )

    refund_owner = await users.get_or_create(5180, "refund-owner")
    open_refund = await refunds.create(
        telegram_id=5180,
        source="automatic",
        reason="privacy regression",
        provider_charge_id="privacy-refund-provider",
        amount_stars=10,
    )
    refund_blocked = False
    try:
        await users.erase(refund_owner["id"])
    except users.EraseBlockedError:
        refund_blocked = (await users.get_by_id(refund_owner["id"]))["telegram_id"] == 5180
    check("an unresolved refund blocks erasure", refund_blocked)
    refund_lease = await refunds.claim(open_refund["id"])
    await refunds.mark_completed(open_refund["id"], refund_lease)
    await users.erase(refund_owner["id"])
    check(
        "a completed refund permits erasure and loses its Telegram id",
        (await refunds.get(open_refund["id"]))["telegram_id"] == 0,
    )

    leaver = await users.get_or_create(5150, "leaver", "Leaver")
    gone_order = await orders.create(
        user_id=leaver["id"],
        product=plan,
        amount_rub=900,
        payment_method="transfer",
        status=orders.COMPLETED,
    )
    await orders.update(gone_order, receipt_file="receipt_x.jpg")
    gone_sub = await subscriptions.create(
        leaver["id"], plan, dates.utcnow() + timedelta(days=10), False
    )
    await subscriptions.set_credentials(gone_sub, "leaver-login")

    await connection.execute(
        "INSERT INTO events (telegram_id, type, data) VALUES (?, 'view', 'x')", (5150,)
    )
    await invoices.create(5150, plan, "leaver-token")
    await orders.update(gone_order, processed_by_telegram_id=5150)
    await journal.action(5150, "former_staff_action", f"order:{gone_order}")
    await journal.action(owner["telegram_id"], "legacy_direct_message", "tg:5150")
    await journal.action(owner["telegram_id"], "legacy_raw_target", "5150")
    erased = await users.erase(leaver["id"])
    row = await users.get_by_id(leaver["id"])
    check(
        "erasing reports the receipt order and file to remove",
        erased["receipts"] == [{"order_id": gone_order, "receipt_file": "receipt_x.jpg"}],
    )
    check("the telegram id is gone", row["telegram_id"] != 5150)
    check(
        "the name and username are gone", not row["username"] and row["first_name"] == "erased"
    )
    check(
        "the access details are gone",
        (await subscriptions.get(gone_sub))["credentials"] is None,
    )
    check(
        "the receipt stays linked until its file is removed",
        (await orders.get(gone_order))["receipt_file"] == "receipt_x.jpg",
    )
    cleared_receipt = await orders.forget_receipt(gone_order, "receipt_x.jpg")
    check(
        "a successful file cleanup clears the matching pointer",
        cleared_receipt and (await orders.get(gone_order))["receipt_file"] is None,
    )
    check("the money history survives", (await orders.get(gone_order))["amount_rub"] == 900)
    check(
        "the activity history is gone",
        await connection.fetch_value(
            "SELECT COUNT(*) FROM events WHERE telegram_id = ?", (5150,), 0
        )
        == 0,
    )
    check(
        "and the invoices no longer point at them",
        await connection.fetch_value(
            "SELECT COUNT(*) FROM invoices WHERE telegram_id = ?", (5150,), 0
        )
        == 0,
    )
    check(
        "audit and order history no longer contain the exact Telegram id",
        await connection.fetch_value(
            "SELECT COUNT(*) FROM audit_log WHERE admin_id = ? OR target IN (?, ?)",
            (5150, "tg:5150", "5150"),
            0,
        )
        == 0
        and await connection.fetch_value(
            "SELECT COUNT(*) FROM orders WHERE processed_by_telegram_id = ?", (5150,), 0
        )
        == 0,
    )
    check(
        "nothing they had is still running",
        (await subscriptions.get(gone_sub))["status"] != subscriptions.ACTIVE,
        (await subscriptions.get(gone_sub))["status"],
    )
    await journal.action(5150, "late_erased_actor", f"order:{gone_order}")
    late_actor_order = await orders.create(
        user_id=customer["id"],
        product=plan,
        amount_rub=1,
        payment_method="transfer",
        status=orders.PENDING_REVIEW,
    )
    await orders.claim_status(
        late_actor_order,
        orders.REJECTED,
        (orders.PENDING_REVIEW,),
        processed_by=5150,
    )
    check(
        "late audit and order writes cannot restore an erased actor id",
        await connection.fetch_value(
            "SELECT admin_id FROM audit_log WHERE action = 'late_erased_actor'"
        )
        == 0
        and (await orders.get(late_actor_order))["processed_by_telegram_id"] is None,
    )
    check(
        "coming back creates a new person, not the old row",
        (await users.get_or_create(5150, "leaver"))["id"] != leaver["id"],
    )

    # --- receipts do not live forever ------------------------------------------
    keeper = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_rub=100,
        payment_method="transfer",
        status=orders.PENDING_REVIEW,
    )
    await orders.update(keeper, receipt_file="fresh.jpg")
    stale = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_rub=100,
        payment_method="transfer",
        status=orders.COMPLETED,
    )
    await orders.update(stale, receipt_file="old.jpg")
    await connection.execute(
        "UPDATE orders SET updated_at = datetime('now', '-400 days') WHERE id = ?", (stale,)
    )
    delivered_receipt = await orders.create(
        user_id=buyer["id"],
        product=plan,
        amount_rub=100,
        payment_method="transfer",
        status=orders.DELIVERED,
    )
    await orders.update(delivered_receipt, receipt_file="delivered-old.jpg")
    await connection.execute(
        "UPDATE orders SET updated_at = datetime('now', '-400 days') WHERE id = ?",
        (delivered_receipt,),
    )
    forget = await orders.receipts_to_forget(180)
    check(
        "old closed and delivered receipts are due for removal",
        [row["id"] for row in forget] == [stale, delivered_receipt],
        str([row["id"] for row in forget]),
    )
    check("a receipt still under review is kept", keeper not in [row["id"] for row in forget])
    await orders.forget_receipt(stale)
    await orders.forget_receipt(delivered_receipt)
    check(
        "forgetting a receipt clears the pointer",
        (await orders.get(stale))["receipt_file"] is None,
    )

    # --- roles cannot be escalated, and a blocked one carries no rights -----------
    boss = await users.get_or_create(777, "boss")
    await users.set_role(boss["id"], "owner")
    helper = await users.get_or_create(778, "helper")
    await users.set_role(helper["id"], "admin")
    boss = await users.get_by_id(boss["id"])
    helper = await users.get_by_id(helper["id"])
    plain = await users.get_by_id((await users.get_or_create(779, "plain"))["id"])

    check(
        "an admin cannot hand out the owner role",
        not access.can_assign_role(helper, plain, "owner"),
    )
    check(
        "nor can an owner, it only comes from .env",
        not access.can_assign_role(boss, plain, "owner"),
    )
    check("an admin cannot touch an owner", not access.can_assign_role(helper, boss, "admin"))
    check(
        "an admin can still appoint a manager", access.can_assign_role(helper, plain, "manager")
    )

    await users.set_blocked(helper["id"], True)
    blocked_admin = await users.get_by_id(helper["id"])
    check(
        "a blocked admin keeps no rights at all",
        not any(access.can(blocked_admin, section) for section in access.SECTION_ROLES),
    )
    check(
        "and cannot assign roles either",
        not access.can_assign_role(blocked_admin, plain, "manager")
        or not access.can(blocked_admin, "roles"),
    )
    await users.set_blocked(helper["id"], False)

    # --- abandoned flow states are actually freed, not merely ignored -------------
    from utils import states as states_mod

    class _Event:
        chat_id = 10
        sender_id = 20

    states_mod.set_for(_Event(), "probe", "settings", user_id=7, name="x", section="s")
    stored = states_mod.get(10, 20)
    check(
        "a flow can store any key it likes, including user_id and name",
        stored is not None and stored["data"] == {"user_id": 7, "name": "x", "section": "s"},
        str(stored and stored["data"]),
    )
    check("and the flow name is still its own", stored["name"] == "probe")
    states_mod.clear(10, 20)

    states_mod.set(10, 20, "probe", "settings")
    check("a fresh state is readable", states_mod.get(10, 20) is not None)
    original_ttl = states_mod.TTL_SECONDS
    states_mod.TTL_SECONDS = 0
    try:
        check("the hourly sweep frees it", states_mod.sweep() >= 1)
        check("and nothing is left behind", not states_mod._states)
    finally:
        states_mod.TTL_SECONDS = original_ttl

    from handlers import common as handlers_common

    check(
        "an image path cannot escape the images folder",
        all(
            handlers_common.image_path({"image": name}) is None
            for name in ("../.env", "/etc/passwd", "sub/dir.png", "..", "")
        ),
    )

    await products.delete(product_id)
    orphan = await orders.get(order_id)
    check("deleted product keeps order history readable", orphan["product_name"] == "Music")
    check(
        "order still knows its product", billing.product_from_order(orphan)["slug"] == "music"
    )

    await connection.disconnect()


def main() -> None:
    try:
        asyncio.run(_sandbox.run_and_close(run))
    finally:
        _sandbox.cleanup()

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed:", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
