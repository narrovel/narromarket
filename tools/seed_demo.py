# Demo catalog for a fresh install: python -m tools.seed_demo
#
# Unlike the other tools this one writes to the configured database on purpose, so it
# names it and refuses to touch a catalog that already has products unless asked twice.

import asyncio
import sys

from config import DATABASE_PATH
from db import connection, products as products_db

DEMO_PRODUCTS = [
    {
        "slug": "music",
        "name": "Music Premium",
        "emoji": "🎧",
        "price_stars": 200,
        "price_rub": 300,
        "duration_days": 30,
        "short_description": "Ad free music for a month",
        "description": ("🎧 No ads\n📥 Offline downloads\n🎚 Best available audio quality"),
        "instruction": (
            "<b>How to activate</b>\n"
            "1. Wait for the invite from the manager\n"
            "2. Accept it in the app\n"
            "3. Check that the plan is active"
        ),
        "sort_order": 10,
    },
    {
        "slug": "cloud-storage",
        "name": "Cloud Storage Plus",
        "emoji": "☁️",
        "price_stars": 800,
        "price_rub": 1200,
        "duration_days": 30,
        "short_description": "Extra storage for one month",
        "description": ("☁️ 100 GB of storage\n📄 Large file uploads\n🔗 Shareable links"),
        "instruction": (
            "<b>How to activate</b>\n"
            "1. Receive the activation link from the manager\n"
            "2. Open the link\n"
            "3. Confirm that the storage is available"
        ),
        "sort_order": 20,
    },
    {
        "slug": "secure-net",
        "name": "Secure Connection",
        "emoji": "🔒",
        "price_stars": 150,
        "price_rub": 250,
        "duration_days": 30,
        "short_description": "Works on every device",
        "description": ("🔒 Every device\n🚀 No speed limits\n👥 Up to 3 members"),
        "instruction": (
            "<b>How to activate</b>\n"
            "1. Get the key from the manager\n"
            "2. Add it to the app\n"
            "3. Keep the key private"
        ),
        "sort_order": 30,
    },
]


async def seed(force: bool = False) -> None:
    print(f"Database: {DATABASE_PATH}")
    await connection.connect()
    if not force and await products_db.list_public(active_only=False):
        await connection.disconnect()
        print("The catalog already has products. Re-run with --force to add the demo ones.")
        return
    created = 0
    for item in DEMO_PRODUCTS:
        if await products_db.get_by_slug(item["slug"]):
            continue
        await products_db.create(owner_user_id=products_db.PUBLIC, is_active=1, **item)
        created += 1
    await connection.disconnect()
    print(f"Products created: {created}")


if __name__ == "__main__":
    asyncio.run(seed("--force" in sys.argv))
