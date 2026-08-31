# Entry point.

import asyncio
import logging
import logging.handlers
import signal
import sys

from telethon import TelegramClient

import config
from db import connection, settings
from db import users as users_db
from handlers import register_all
from services import notify, scheduler

logger = logging.getLogger("narromarket")

# Exit code for a failure that restarting cannot fix: bad configuration, or a database
# that needs a human. systemd is told not to restart on it, so every other exit - a
# dropped connection above all - still restarts forever.
EXIT_NEEDS_HUMAN = 78


def setup_logging() -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(config.LOG_FORMAT)

    # Rotated, otherwise the file grows without limit for as long as the bot runs.
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    root.handlers = [file_handler, console_handler]

    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def main() -> None:
    missing = config.missing_required()
    if missing:
        logger.error("Missing environment variables: %s. See .env.example", ", ".join(missing))
        sys.exit(EXIT_NEEDS_HUMAN)
    bad = config.bad_values()
    if bad:
        logger.error("These variables are not valid: %s", ", ".join(bad))
        sys.exit(EXIT_NEEDS_HUMAN)

    try:
        await connection.connect()
    except RuntimeError as error:
        # The database needs a human before the bot can start; restarting will not help.
        logger.error("%s", error)
        sys.exit(EXIT_NEEDS_HUMAN)
    await settings.load()
    await users_db.ensure_owners(config.OWNER_IDS)

    client = TelegramClient(
        config.SESSION_NAME,
        config.API_ID,
        config.API_HASH,
        connection_retries=10,
        retry_delay=5,
    )
    register_all(client)

    # systemd stops the unit with SIGTERM, which by default kills the interpreter
    # without unwinding: nothing in the finally below would ever run.
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(name, lambda: asyncio.ensure_future(client.disconnect()))
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await client.start(bot_token=config.BOT_TOKEN)
        scheduler.set_client(client)
        scheduler.start()
        await scheduler.run_checks()
        await scheduler.reconcile()

        me = await client.get_me()
        logger.info("Bot started: @%s", me.username)
        await notify.to_staff(client, f"🟢 {settings.get('bot_name')} is up. Panel: /admin")

        await client.run_until_disconnected()
    finally:
        await scheduler.stop()
        await connection.disconnect()
        logger.info("Bot stopped")


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by Ctrl+C")
    except Exception as error:
        logger.exception("Fatal error: %s", error)
        sys.exit(1)
