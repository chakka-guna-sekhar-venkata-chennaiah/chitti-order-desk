"""One place for "now".

All code asks this module for the time. Tests replace the clock with a fixed
one (`set_clock`), so time-based rules (expiry, overdue bills) are testable.
Times are UTC ISO-8601 strings, the same format as the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def set_clock(fn: Callable[[], datetime]) -> None:
    """Replace the clock (tests only)."""
    global _now
    _now = fn


def reset_clock() -> None:
    global _now
    _now = lambda: datetime.now(timezone.utc)


def now() -> datetime:
    return _now()


def now_iso() -> str:
    return to_iso(now())


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, ISO_FORMAT).replace(tzinfo=timezone.utc)


def iso_in(hours: float = 0, days: float = 0, seconds: float = 0) -> str:
    """Time `hours`/`days`/`seconds` from now, as an ISO string."""
    return to_iso(now() + timedelta(hours=hours, days=days, seconds=seconds))
