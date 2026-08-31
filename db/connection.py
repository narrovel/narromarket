# SQLite connection and schema.

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Optional

import aiosqlite

from config import DATABASE_PATH, RECEIPTS_DIR

logger = logging.getLogger(__name__)

_connection: Optional[aiosqlite.Connection] = None

# One connection is shared by every handler, so a plain commit would also publish
# whatever another coroutine left half written. Writes take this lock, and a
# transaction holds it from BEGIN to COMMIT.
_write_lock = asyncio.Lock()
_transaction_owner: ContextVar[Optional[asyncio.Task]] = ContextVar(
    "transaction_owner", default=None
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    is_blocked INTEGER NOT NULL DEFAULT 0,
    payment_method_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (payment_method_id) REFERENCES payment_methods(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '📦',
    price_stars INTEGER NOT NULL DEFAULT 0,
    price_rub INTEGER NOT NULL DEFAULT 0,
    duration_days INTEGER NOT NULL DEFAULT 30,
    short_description TEXT,
    description TEXT,
    instruction TEXT,
    image TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (slug, owner_user_id)
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'sbp',
    details TEXT NOT NULL,
    bank TEXT,
    holder TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    product_slug TEXT NOT NULL,
    product_name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '📦',
    is_personal INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    credentials TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    notified_3d INTEGER NOT NULL DEFAULT 0,
    notified_1d INTEGER NOT NULL DEFAULT 0,
    notified_expired INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    product_slug TEXT NOT NULL,
    product_name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '📦',
    subscription_id INTEGER,
    is_personal INTEGER NOT NULL DEFAULT 0,
    is_renewal INTEGER NOT NULL DEFAULT 0,
    duration_days INTEGER NOT NULL DEFAULT 30,
    amount_stars INTEGER NOT NULL DEFAULT 0,
    amount_rub INTEGER NOT NULL DEFAULT 0,
    payment_method TEXT NOT NULL DEFAULT 'stars',
    payment_method_id INTEGER,
    payment_charge_id TEXT,
    payment_provider_charge_id TEXT,
    payment_recipient_id INTEGER,
    paid_at TEXT,
    reversed_at TEXT,
    receipt_file TEXT,
    status TEXT NOT NULL DEFAULT 'pending_receipt',
    processed_by_telegram_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL,
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    product_slug TEXT NOT NULL,
    product_name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '📦',
    owner_user_id INTEGER NOT NULL DEFAULT 0,
    amount_stars INTEGER NOT NULL,
    duration_days INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'XTR',
    terms_hash TEXT NOT NULL DEFAULT '',
    precheckout_approved_at TEXT,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    user_id INTEGER,
    telegram_id INTEGER NOT NULL,
    telegram_charge_id TEXT,
    provider_charge_id TEXT,
    source TEXT NOT NULL,
    payment_method TEXT NOT NULL DEFAULT 'stars',
    amount_stars INTEGER NOT NULL DEFAULT 0,
    amount_rub INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'XTR',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    resolution TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    type TEXT NOT NULL,
    data TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);
CREATE INDEX IF NOT EXISTS idx_products_owner ON products (owner_user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (user_id, status);
CREATE INDEX IF NOT EXISTS idx_subs_expires ON subscriptions (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type, created_at);
CREATE INDEX IF NOT EXISTS idx_refunds_status ON refunds (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_order
    ON refunds (order_id) WHERE order_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_telegram_charge
    ON refunds (telegram_charge_id) WHERE telegram_charge_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_provider_charge
    ON refunds (provider_charge_id) WHERE provider_charge_id IS NOT NULL;
"""

# Created only after _migrate has cleaned up any data that would violate them. Putting
# them in SCHEMA meant executescript ran them against untouched rows on the very first
# start after an upgrade, and a shop with real history could never boot again.
CONSTRAINTS = """
-- One Telegram charge may pay for exactly one order, whatever Telegram redelivers.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_charge
    ON orders (payment_charge_id) WHERE payment_charge_id IS NOT NULL;

-- A client cannot hold two running subscriptions for the same product.
CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_active_slug
    ON subscriptions (user_id, product_slug) WHERE status = 'active';
"""

# Applied in order to databases created before the statement was added. The list index
# plus one is the schema version stored in PRAGMA user_version.
MIGRATIONS = [
    # 1: duplicates of the UNIQUE autoindexes, whole rubles, receipt bookkeeping.
    [
        "DROP INDEX IF EXISTS idx_users_tg",
        "DROP INDEX IF EXISTS idx_invoices_token",
        "UPDATE products SET price_rub = CAST(price_rub + 0.5 AS INTEGER)",
        "UPDATE orders SET amount_rub = CAST(amount_rub + 0.5 AS INTEGER)",
        ("ADD COLUMN", "orders", "payment_method_id", "INTEGER"),
        ("ADD COLUMN", "payment_methods", "is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("RENAME COLUMN", "orders", "processed_by", "processed_by_telegram_id"),
    ],
    # 2: make the data satisfy the unique constraints that are about to be created.
    [
        # Keep the first order for a charge, tag the rest so the value stays readable
        # for reconciliation instead of being thrown away.
        """
        UPDATE orders SET payment_charge_id = payment_charge_id || ':dup' || id
        WHERE payment_charge_id IS NOT NULL AND id NOT IN (
            SELECT MIN(id) FROM orders WHERE payment_charge_id IS NOT NULL
            GROUP BY payment_charge_id
        )
        """,
        # Of several active subscriptions for one product keep the one that runs longest.
        """
        UPDATE subscriptions
        SET status = 'cancelled', credentials = NULL, updated_at = datetime('now')
        WHERE status = 'active' AND id NOT IN (
            SELECT id FROM (
                SELECT id, MAX(expires_at) FROM subscriptions WHERE status = 'active'
                GROUP BY user_id, product_slug
            )
        )
        """,
        # The old scheduler could leave notified_1d set with notified_3d clear; the new
        # catch-up query would then send one extra reminder.
        "UPDATE subscriptions SET notified_3d = 1 WHERE notified_1d = 1",
    ],
    # 3: key revenue on confirmed payment rather than the current workflow status.
    [
        ("ADD COLUMN", "orders", "paid_at", "TEXT"),
        """
        UPDATE orders SET paid_at = COALESCE(updated_at, created_at)
        WHERE paid_at IS NULL
          AND status IN ('paid', 'delivered', 'completed', 'refunded')
        """,
    ],
    # 4: a reversal is recorded on the order instead of erasing what it granted.
    [
        ("ADD COLUMN", "orders", "reversed_at", "TEXT"),
        """
        UPDATE orders SET reversed_at = COALESCE(updated_at, created_at)
        WHERE reversed_at IS NULL
          AND status IN ('refunded', 'rejected', 'cancelled')
          AND paid_at IS NOT NULL
        """,
    ],
    # 5: keep Telegram's two charge identifiers separate and freeze the commercial
    # terms carried by an invoice. Legacy pending invoices did not record those terms,
    # so they are closed and customers can create a fresh, fully specified invoice.
    [
        ("ADD COLUMN", "orders", "payment_provider_charge_id", "TEXT"),
        ("ADD COLUMN", "invoices", "product_slug", "TEXT NOT NULL DEFAULT ''"),
        ("ADD COLUMN", "invoices", "product_name", "TEXT NOT NULL DEFAULT ''"),
        ("ADD COLUMN", "invoices", "emoji", "TEXT NOT NULL DEFAULT '📦'"),
        ("ADD COLUMN", "invoices", "owner_user_id", "INTEGER NOT NULL DEFAULT 0"),
        ("ADD COLUMN", "invoices", "amount_stars", "INTEGER NOT NULL DEFAULT 0"),
        ("ADD COLUMN", "invoices", "duration_days", "INTEGER NOT NULL DEFAULT 30"),
        ("ADD COLUMN", "invoices", "currency", "TEXT NOT NULL DEFAULT 'XTR'"),
        """
        UPDATE invoices
        SET product_slug = COALESCE(
                (SELECT p.slug FROM products p WHERE p.id = invoices.product_id),
                'legacy-' || product_id
            ),
            product_name = COALESCE(
                (SELECT p.name FROM products p WHERE p.id = invoices.product_id),
                'Legacy product'
            ),
            emoji = COALESCE(
                (SELECT p.emoji FROM products p WHERE p.id = invoices.product_id),
                '📦'
            ),
            owner_user_id = COALESCE(
                (SELECT p.owner_user_id FROM products p WHERE p.id = invoices.product_id),
                0
            ),
            amount_stars = COALESCE(
                (SELECT p.price_stars FROM products p WHERE p.id = invoices.product_id),
                0
            ),
            duration_days = COALESCE(
                (SELECT p.duration_days FROM products p WHERE p.id = invoices.product_id),
                30
            ),
            currency = 'XTR'
        """,
        "UPDATE invoices SET status = 'cancelled' WHERE status = 'pending'",
    ],
    # 6: persist every refund obligation before talking to Telegram. A processing
    # lease makes retries idempotent across concurrent staff actions and restarts.
    [
        """
        CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            telegram_id INTEGER NOT NULL,
            telegram_charge_id TEXT,
            provider_charge_id TEXT,
            source TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'stars',
            amount_stars INTEGER NOT NULL DEFAULT 0,
            amount_rub INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'XTR',
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            resolution TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_refunds_status ON refunds (status, created_at)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_order
        ON refunds (order_id) WHERE order_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_telegram_charge
        ON refunds (telegram_charge_id) WHERE telegram_charge_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_provider_charge
        ON refunds (provider_charge_id) WHERE provider_charge_id IS NOT NULL
        """,
    ],
    # 7: replace raw Telegram ids in audit rows with internal references, and remove
    # identifiers whose user row has already been erased.
    # Completed refunds no longer need a Telegram id; unresolved ones keep it so staff
    # can still return the money.
    [
        """
        UPDATE audit_log
        SET target = (
            SELECT 'user:' || u.id FROM users u
            WHERE audit_log.target = 'tg:' || u.telegram_id
            LIMIT 1
        )
        WHERE target LIKE 'tg:%'
        """,
        """
        UPDATE audit_log SET admin_id = 0
        WHERE admin_id != 0 AND NOT EXISTS (
            SELECT 1 FROM users u WHERE u.telegram_id = audit_log.admin_id
        )
        """,
        """
        UPDATE orders SET processed_by_telegram_id = NULL
        WHERE processed_by_telegram_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.telegram_id = orders.processed_by_telegram_id
        )
        """,
        """
        UPDATE refunds SET telegram_id = 0
        WHERE status = 'completed' AND telegram_id != 0 AND NOT EXISTS (
            SELECT 1 FROM users u WHERE u.telegram_id = refunds.telegram_id
        )
        """,
    ],
    # 8: bind the confirmation screen to a one-time, expiring quote. The hash records
    # the exact product and shop terms shown before the invoice becomes payable.
    [
        ("ADD COLUMN", "invoices", "terms_hash", "TEXT NOT NULL DEFAULT ''"),
    ],
    # 9: a Stars refund still needs its original Telegram recipient after the customer
    # asks for profile erasure. Keep that one financial identifier on paid orders.
    [
        ("ADD COLUMN", "orders", "payment_recipient_id", "INTEGER"),
        """
        UPDATE orders
        SET payment_recipient_id = (
            SELECT u.telegram_id FROM users u
            WHERE u.id = orders.user_id AND u.telegram_id > 0
        )
        WHERE payment_method = 'stars'
          AND payment_charge_id IS NOT NULL
          AND payment_recipient_id IS NULL
        """,
    ],
    # 10: remember that Telegram's pre-checkout query was approved. The successful
    # payment update can arrive just after the invoice TTL (or after a restart) and must
    # not be refunded only because the local clock crossed that boundary meanwhile.
    [
        ("ADD COLUMN", "invoices", "precheckout_approved_at", "TEXT"),
    ],
    # 11: Telegram ids can be reused by a newly registered profile after erasure. Bind
    # refund notices to the original internal user row so old financial messages cannot
    # be delivered to that new profile.
    [
        ("ADD COLUMN", "refunds", "user_id", "INTEGER"),
        """
        UPDATE refunds SET user_id = COALESCE(
            (SELECT o.user_id FROM orders o WHERE o.id = refunds.order_id),
            (SELECT u.id FROM users u
             WHERE u.telegram_id = refunds.telegram_id AND u.telegram_id > 0)
        )
        WHERE user_id IS NULL
        """,
    ],
]

SCHEMA_VERSION = len(MIGRATIONS)


async def connect() -> None:
    global _connection
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DATABASE_PATH)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=30000")
        await conn.execute("PRAGMA foreign_keys=ON")
        fresh = not await _table_exists(conn, "users")
        await conn.executescript(SCHEMA)
        await conn.commit()
        await _migrate(conn, fresh)
        try:
            await conn.executescript(CONSTRAINTS)
            await conn.commit()
        except Exception as first_error:
            # Repair migrated and already-current databases once before failing startup.
            logger.warning("Uniqueness rules rejected the data (%s), repairing", first_error)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                for step in MIGRATIONS[1]:
                    await _run_migration_step(conn, step)
                await conn.commit()
                await conn.executescript(CONSTRAINTS)
                await conn.commit()
            except Exception as error:
                await _safe_rollback(conn)
                raise RuntimeError(
                    "The database contains rows that break a uniqueness rule "
                    f"({error}), and the automatic repair did not help. The tables and "
                    "migrations up to this point ARE already applied; only the "
                    "uniqueness indexes are missing. Back up the database, reconcile "
                    "duplicate orders.payment_charge_id values and duplicate active "
                    "subscriptions for the same user_id/product_slug, then start again."
                ) from error
    except Exception:
        await conn.close()
        raise
    _connection = conn
    _columns_cache.clear()
    logger.info("Database ready: %s (schema v%s)", DATABASE_PATH, SCHEMA_VERSION)


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def _column_names(conn: aiosqlite.Connection, table: str) -> set:
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def _migrate(conn: aiosqlite.Connection, fresh: bool) -> None:
    # A fresh file already matches the current schema, so it is only stamped.
    if fresh:
        await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await conn.commit()
        return

    async with conn.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    version = row[0] if row else 0
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema is v{version}, but this code supports up to v{SCHEMA_VERSION}. "
            "Start the same or a newer application version; refusing to write with an "
            "older schema definition."
        )
    if version >= SCHEMA_VERSION:
        return

    for index in range(version, SCHEMA_VERSION):
        await conn.execute("BEGIN IMMEDIATE")
        changed = 0
        try:
            for step in MIGRATIONS[index]:
                changed += await _run_migration_step(conn, step)
            await conn.execute(f"PRAGMA user_version = {index + 1}")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        # Row counts matter: migration 2 can cancel a customer's subscription, and an
        # operator has to know that happened.
        logger.info("Applied schema migration %s, %s row(s) changed", index + 1, changed)


async def _run_migration_step(conn: aiosqlite.Connection, step) -> int:
    if isinstance(step, str):
        cursor = await conn.execute(step)
        return max(0, cursor.rowcount)
    kind, table, column, extra = step
    columns = await _column_names(conn, table)
    if kind == "ADD COLUMN":
        if column not in columns:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {extra}")
    elif kind == "RENAME COLUMN":
        if column in columns and extra not in columns:
            await conn.execute(f"ALTER TABLE {table} RENAME COLUMN {column} TO {extra}")
    return 0


async def disconnect() -> None:
    global _connection
    conn = _connection
    if conn is not None:
        # Wait for any open transaction instead of closing under it, which would
        # discard the writes without a word.
        async with _write_lock:
            _connection = None
            await conn.close()
    _columns_cache.clear()


def _require_connection() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Database is not connected")
    return _connection


async def _safe_rollback(conn: aiosqlite.Connection) -> None:
    # Never let a rollback failure mask the error that caused it.
    try:
        await conn.rollback()
    except Exception as error:
        logger.error("Rollback failed: %s", error)


@asynccontextmanager
async def transaction():
    # Group several statements so a crash or an error cannot leave half of them behind.
    conn = _require_connection()
    if _owns_transaction():
        yield conn
        return
    async with _write_lock:
        await conn.execute("BEGIN IMMEDIATE")
        token = _transaction_owner.set(asyncio.current_task())
        try:
            yield conn
            await conn.commit()
        except BaseException:
            await _safe_rollback(conn)
            raise
        finally:
            _transaction_owner.reset(token)


def _owns_transaction() -> bool:
    # Context variables are copied into asyncio.create_task(). Comparing the actual
    # task, rather than inheriting a boolean, prevents a child task from bypassing the
    # write lock and observing or committing its parent's unfinished transaction.
    return _transaction_owner.get() is asyncio.current_task()


async def execute(sql: str, params: Iterable[Any] = ()) -> int:
    # Run a statement and return the inserted row id.
    conn = _require_connection()
    if _owns_transaction():
        cursor = await conn.execute(sql, tuple(params))
        return cursor.lastrowid
    async with _write_lock:
        try:
            cursor = await conn.execute(sql, tuple(params))
            await conn.commit()
        except BaseException:
            await _safe_rollback(conn)
            raise
        return cursor.lastrowid


async def execute_change(sql: str, params: Iterable[Any] = ()) -> int:
    # Run a statement and return how many rows it changed, for compare and swap claims.
    conn = _require_connection()
    if _owns_transaction():
        cursor = await conn.execute(sql, tuple(params))
        return cursor.rowcount
    async with _write_lock:
        try:
            cursor = await conn.execute(sql, tuple(params))
            await conn.commit()
        except BaseException:
            await _safe_rollback(conn)
            raise
        return cursor.rowcount


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    conn = _require_connection()
    values = tuple(params)

    async def query() -> Optional[dict]:
        async with conn.execute(sql, values) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    if _owns_transaction():
        return await query()
    # One SQLite connection exposes its uncommitted rows to every cursor on that same
    # connection. Readers therefore share the write lock: they see either the state
    # before a transaction or the state after its commit, never its temporary middle.
    async with _write_lock:
        return await query()


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    conn = _require_connection()
    values = tuple(params)

    async def query() -> list[dict]:
        async with conn.execute(sql, values) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    if _owns_transaction():
        return await query()
    async with _write_lock:
        return await query()


async def fetch_value(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    conn = _require_connection()
    values = tuple(params)

    async def query() -> Any:
        async with conn.execute(sql, values) as cursor:
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return default
            return row[0]

    if _owns_transaction():
        return await query()
    async with _write_lock:
        return await query()


async def update_row(table: str, row_id: int, **fields: Any) -> None:
    # Update a row by id. Column names come from code, never from user input.
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    if await _has_column(table, "updated_at"):
        assignments += ", updated_at = datetime('now')"
    await execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        [*fields.values(), row_id],
    )


_columns_cache: dict[str, set] = {}


async def _has_column(table: str, column: str) -> bool:
    if table not in _columns_cache:
        rows = await fetch_all(f"PRAGMA table_info({table})")
        _columns_cache[table] = {row["name"] for row in rows}
    return column in _columns_cache[table]
