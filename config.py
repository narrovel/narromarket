# Environment settings. Everything editable at runtime lives in the settings table.

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_bad_values: list[str] = []
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_quotes(value: str) -> str:
    # Only a matching pair is stripped, so a value that legitimately ends in a quote
    # keeps it.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        _bad_values.append(".env (cannot be read as UTF-8)")
        return
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not _ENV_KEY.fullmatch(key):
            _bad_values.append(f".env line {line_number} (invalid variable name)")
            continue
        value = value.strip()
        if value[:1] not in "\"'":
            # Trailing comment, but only when it is clearly separated from the value.
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        try:
            os.environ.setdefault(key, _strip_quotes(value))
        except (OSError, ValueError):
            _bad_values.append(f".env line {line_number} (invalid value)")


_load_env_file(BASE_DIR / ".env")


def _int_list(name: str, raw: str) -> list[int]:
    result = []
    if not raw.strip():
        return result
    chunks = raw.replace(";", ",").split(",")
    invalid = False
    for chunk in chunks:
        chunk = chunk.strip()
        try:
            value = int(chunk) if chunk.isdigit() else 0
        except ValueError:
            value = 0
        if not 0 < value <= 9_223_372_036_854_775_807:
            invalid = True
            continue
        if value not in result:
            result.append(value)
    if invalid:
        _bad_values.append(name)
    return result


def _int_env(
    name: str, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
    # A bad number must not blow up at import time with a bare traceback: it is
    # collected and reported next to the missing variables instead.
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _bad_values.append(name)
        return default
    if minimum is not None and value < minimum:
        _bad_values.append(name)
        return default
    if maximum is not None and value > maximum:
        _bad_values.append(name)
        return default
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_API_ID_RAW = (os.getenv("API_ID") or "").strip()
API_ID = _int_env("API_ID", 0, minimum=1, maximum=2_147_483_647)
API_HASH = os.getenv("API_HASH", "").strip()
_OWNER_IDS_RAW = os.getenv("OWNER_IDS", "")
OWNER_IDS = _int_list("OWNER_IDS", _OWNER_IDS_RAW)

if BOT_TOKEN:
    token_id, separator, token_secret = BOT_TOKEN.partition(":")
    if not separator or not token_id.isdigit() or not token_secret:
        _bad_values.append("BOT_TOKEN")
if API_HASH and (
    len(API_HASH) != 32 or any(char not in "0123456789abcdefABCDEF" for char in API_HASH)
):
    _bad_values.append("API_HASH")


def _timezone(name: str) -> str:
    # Validate the timezone before utils.dates builds its module-level ZoneInfo.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _bad_values.append(f"TIMEZONE ({name!r} is not a known timezone)")
        return "UTC"
    return name


TIMEZONE = _timezone(os.getenv("TIMEZONE", "Europe/Moscow"))


def _path_env(name: str, default: Path) -> Path:
    raw = (os.getenv(name) or "").strip()
    path = Path(raw) if raw else default
    return path if path.is_absolute() else BASE_DIR / path


DATA_DIR = _path_env("DATA_DIR", Path("data"))
DATABASE_PATH = _path_env("DATABASE_PATH", DATA_DIR / "narromarket.db")
RECEIPTS_DIR = DATA_DIR / "receipts"
_SESSION_NAME = (os.getenv("SESSION_NAME") or "narromarket").strip()
_SESSION_PATH = Path(_SESSION_NAME)
if (
    not _SESSION_NAME
    or "\x00" in _SESSION_NAME
    or _SESSION_NAME in {".", ".."}
    or (not _SESSION_PATH.is_absolute() and _SESSION_PATH.name != _SESSION_NAME)
):
    _bad_values.append("SESSION_NAME")
    _SESSION_PATH = Path("narromarket")
SESSION_NAME = str(_SESSION_PATH if _SESSION_PATH.is_absolute() else DATA_DIR / _SESSION_PATH)
IMAGES_DIR = BASE_DIR / "images"
LOGS_DIR = _path_env("LOGS_DIR", Path("logs"))
LOG_FILE = LOGS_DIR / "bot.log"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    _bad_values.append("LOG_LEVEL")
    LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

INVOICE_CURRENCY = "XTR"
INVOICE_TTL_MINUTES = _int_env("INVOICE_TTL_MINUTES", 60, minimum=1, maximum=10_080)

# A bank transfer is not a Stars invoice: it can clear the next working day, so it gets
# its own, much longer deadline.
RECEIPT_TTL_HOURS = _int_env("RECEIPT_TTL_HOURS", 48, minimum=1, maximum=720)
REVIEW_STALE_HOURS = _int_env("REVIEW_STALE_HOURS", 72, minimum=1, maximum=2_160)
EVENT_RETENTION_DAYS = _int_env("EVENT_RETENTION_DAYS", 90, minimum=1, maximum=3_650)

# Receipts are customers' bank documents. They are kept long enough to settle a dispute
# and then deleted, because nothing else on this box ever shrinks.
RECEIPT_RETENTION_DAYS = _int_env(
    "RECEIPT_RETENTION_DAYS", 180, minimum=1, maximum=3_650
)

RECEIPT_MAX_BYTES = 5 * 1024 * 1024
RECEIPT_MIME_HINTS = ("jpeg", "jpg", "png", "pdf")

# Checked against the first bytes of the downloaded file: the client picked mime type is
# not evidence of anything.
RECEIPT_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"%PDF", ".pdf"),
)


def missing_required() -> list[str]:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not _API_ID_RAW:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not _OWNER_IDS_RAW.strip():
        missing.append("OWNER_IDS")
    return missing


def bad_values() -> list[str]:
    return list(_bad_values)
