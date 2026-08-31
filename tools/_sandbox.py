# Throwaway database for the offline tools.
#
# Import before config so offline checks always select a temporary database.

import atexit
import os
import shutil
import tempfile

TEMP_DIR = tempfile.mkdtemp(prefix="narromarket-tools-")

os.environ["DATA_DIR"] = TEMP_DIR
os.environ["DATABASE_PATH"] = os.path.join(TEMP_DIR, "sandbox.db")
os.environ.setdefault("OWNER_IDS", "111")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "sandbox")
os.environ.setdefault("BOT_TOKEN", "sandbox")

# Assigned, not defaulted: several checks assert on these exact values, so a developer's
# own .env must not change what the suite measures.
os.environ["TIMEZONE"] = "Europe/Moscow"
os.environ["INVOICE_TTL_MINUTES"] = "60"
os.environ["RECEIPT_TTL_HOURS"] = "48"
os.environ["REVIEW_STALE_HOURS"] = "72"
os.environ["EVENT_RETENTION_DAYS"] = "90"
os.environ["RECEIPT_RETENTION_DAYS"] = "180"


def cleanup() -> None:
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


atexit.register(cleanup)


def guard_live_database() -> None:
    # Last line of defence: refuse to run if anything reset the path back to the real one.
    from config import DATABASE_PATH

    if TEMP_DIR not in str(DATABASE_PATH):
        raise SystemExit(
            f"Refusing to run against {DATABASE_PATH}: the sandbox was not applied. "
            "Import tools._sandbox before any module that imports config."
        )


async def run_and_close(main) -> None:
    # aiosqlite's worker thread is not a daemon, so a tool that raises before it
    # disconnects would leave the process hanging at exit instead of reporting.
    from db import connection

    try:
        await main()
    finally:
        await connection.disconnect()
