# Date helpers. Everything is stored in UTC and shown in the configured timezone.

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from config import TIMEZONE

TZ = ZoneInfo(TIMEZONE)
SQL_FORMAT = "%Y-%m-%d %H:%M:%S"

Timelike = Union[datetime, str, None]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse(value: Timelike) -> Optional[datetime]:
    # Normalize a stored value to naive UTC.
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], SQL_FORMAT)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def to_sql(value: Timelike) -> Optional[str]:
    parsed = parse(value)
    return parsed.strftime(SQL_FORMAT) if parsed else None


def to_local(value: Timelike) -> Optional[datetime]:
    parsed = parse(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).astimezone(TZ)


def local_today() -> date:
    return datetime.now(TZ).date()


def fmt_date(value: Timelike) -> str:
    local = to_local(value)
    return local.strftime("%d.%m.%Y") if local else "-"


def fmt_datetime(value: Timelike) -> str:
    local = to_local(value)
    return local.strftime("%d.%m.%Y %H:%M") if local else "-"


def days_left(value: Timelike) -> Optional[int]:
    local = to_local(value)
    if local is None:
        return None
    return (local.date() - local_today()).days


def day_bounds_utc(offset_days: int = 0) -> tuple[str, str]:
    # Bounds of a local day (today + offset) as UTC strings for SQL comparison.
    day = local_today() + timedelta(days=offset_days)
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc.strftime(SQL_FORMAT), end_utc.strftime(SQL_FORMAT)


def month_bounds_utc(offset_months: int = 0) -> tuple[str, str]:
    # Bounds of a local calendar month as UTC strings. Reports must not be built on
    # SQLite's UTC month while every date the shop shows is local.
    today = local_today()
    year, month = today.year, today.month + offset_months
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    start_local = datetime(year, month, 1, tzinfo=TZ)
    next_month = datetime(year + (month // 12), month % 12 + 1, 1, tzinfo=TZ)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end = next_month.astimezone(timezone.utc).replace(tzinfo=None)
    return start.strftime(SQL_FORMAT), end.strftime(SQL_FORMAT)


def add_days(value: Timelike, days: int) -> Optional[datetime]:
    parsed = parse(value)
    return parsed + timedelta(days=days) if parsed else None
