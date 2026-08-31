# Runtime settings, editable from the admin panel and persisted in SQLite.

import logging
import re

from db import connection

logger = logging.getLogger(__name__)

WELCOME_DEFAULT = (
    "🛒 <b>{bot_name}</b>\n\n"
    "Welcome to the subscription shop.\n"
    "Pick a plan, pay in a couple of taps and renew whenever you like.\n\n"
    "Choose a section 👇"
)

HELP_DEFAULT = (
    "❓ <b>Help</b>\n\n"
    "<b>How it works</b>\n"
    "1. Pick a plan in the catalog\n"
    "2. Pay with Telegram Stars\n"
    "3. Wait for the access details from the manager\n"
    "4. Confirm that everything works\n\n"
    "<b>Renewal</b>\n"
    "You can renew at any time. The new period is added to the "
    "current expiry date, not to the payment date.\n\n"
    "<b>Refunds</b>\n"
    "Available until the access details are delivered."
)

TERMS_DEFAULT = (
    "📄 <b>Payment terms</b>\n\n"
    "Before paying, check the product, price and subscription period shown in the "
    "confirmation. Pressing the confirmation button means you accept those exact terms.\n\n"
    "Digital goods bought inside Telegram are paid for with Telegram Stars. The manual "
    "transfer flow must not be offered as an alternative for digital access.\n\n"
    "Access details are normally sent within 24 hours. A refund is available until the "
    "access details are delivered. If a payment was taken but the order did not appear, "
    "use /paysupport right away."
)

SUPPORT_DEFAULT = (
    "🛟 <b>Payment support</b>\n\n"
    "If a payment was taken, an invoice failed, or a refund has not arrived, send the "
    "manager your order number, payment method and approximate payment time.\n\n"
    "Never send a password, a card number, a CVV code or a Telegram login code."
)

DEFAULTS = {
    "bot_name": "NarroMarket",
    "manager_username": "",
    "star_to_rub": "1.5",
    "notify_days_before": "3",
    "check_hour": "10",
    "check_minute": "0",
    "monthly_report_day": "1",
    "catalog_per_page": "6",
    "require_username": "1",
    "transfer_for_all": "0",
    "welcome_text": WELCOME_DEFAULT,
    "help_text": HELP_DEFAULT,
    "terms_text": TERMS_DEFAULT,
    "support_text": SUPPORT_DEFAULT,
    # Bookkeeping, not shown in the settings panel.
    "last_monthly_report": "",
}

TITLES = {
    "bot_name": "Bot name",
    "manager_username": "Manager username",
    "star_to_rub": "Star to RUB rate",
    "notify_days_before": "First reminder, days before",
    "check_hour": "Daily check hour",
    "check_minute": "Daily check minute",
    "monthly_report_day": "Monthly report day",
    "catalog_per_page": "Products per page",
    "require_username": "Require username (1/0)",
    "transfer_for_all": "Bank transfer for everyone (1/0)",
    "welcome_text": "Welcome message",
    "help_text": "Help message",
    "terms_text": "Payment terms",
    "support_text": "Payment support message",
}

# Values that reach APScheduler or a Telegram keyboard. Out of range they either stop
# the bot from starting at all or make a screen impossible to render.
LIMITS = {
    "star_to_rub": (0.1, 1000.0),
    "notify_days_before": (1, 30),
    "check_hour": (0, 23),
    "check_minute": (0, 59),
    "monthly_report_day": (1, 28),
    "catalog_per_page": (2, 20),
}

BOOLEAN_KEYS = {"require_username", "transfer_for_all"}
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}

MAX_TEXT_LENGTH = 3000

_cache: dict[str, str] = dict(DEFAULTS)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def validate(key: str, value: str):
    # Returns (cleaned_value, error). The error is shown to the admin as is.
    value = (value or "").strip()
    if key == "manager_username":
        # It goes straight into a Button.url; a space there makes Telegram reject the
        # whole keyboard, and the catalog and help screens stop rendering at all.
        cleaned = value.lstrip("@")
        if cleaned and not USERNAME_RE.match(cleaned):
            return None, "A username is 5 to 32 letters, digits or underscores."
        return cleaned, None
    if key in BOOLEAN_KEYS:
        normalized = value.lower()
        if normalized in TRUE_VALUES:
            return "1", None
        if normalized in FALSE_VALUES:
            return "0", None
        return None, "Send 1 to enable or 0 to disable."
    if key not in LIMITS:
        if len(value) > MAX_TEXT_LENGTH:
            return None, f"Too long, keep it under {MAX_TEXT_LENGTH} characters."
        return value, None

    low, high = LIMITS[key]
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None, "Send a number."
    if not low <= number <= high:
        return None, f"Allowed range: {_fmt(low)} to {_fmt(high)}."
    if isinstance(low, int) and isinstance(high, int):
        if not number.is_integer():
            return None, "Send a whole number."
        return str(int(number)), None
    return str(number), None


def _fmt(value) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


async def load() -> None:
    _cache.clear()
    _cache.update(DEFAULTS)
    for row in await connection.fetch_all("SELECT key, value FROM settings"):
        key = row["key"]
        if key not in DEFAULTS:
            continue
        value, error = validate(key, row["value"])
        if error:
            logger.error("Ignoring invalid stored setting %s: %s", key, error)
            continue
        _cache[key] = value


def get(key: str, default: str = "") -> str:
    return _cache.get(key, DEFAULTS.get(key, default))


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get(key)))
    except (OverflowError, TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return default


def get_bool(key: str) -> bool:
    return get(key).strip().lower() in TRUE_VALUES


def all_values() -> dict[str, str]:
    return dict(_cache)


async def set_value(key: str, value: str) -> None:
    await connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    _cache[key] = value


def manager_link() -> str:
    username = get("manager_username").lstrip("@")
    return f"https://t.me/{username}" if username else ""


def manager_mention() -> str:
    username = get("manager_username").lstrip("@")
    return f"@{username}" if username else "the manager"
