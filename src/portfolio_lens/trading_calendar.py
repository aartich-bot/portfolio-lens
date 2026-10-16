"""NYSE sessions, holidays, and actual close times without rewriting trade dates."""

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import exchange_calendars as xcals

_FIRST_DAY = date(1990, 1, 1)
_LAST_DAY = date(2100, 12, 31)


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar("XNYS", start=_FIRST_DAY, end=_LAST_DAY)


def _check_day(day: date) -> None:
    if not _FIRST_DAY <= day <= _LAST_DAY:
        raise ValueError(
            f"Date {day.isoformat()} is outside the supported US trading calendar "
            f"({_FIRST_DAY.isoformat()} through {_LAST_DAY.isoformat()})."
        )


def non_trading_reason(day: date) -> str | None:
    """Explain closed dates; preserve the original date and established weekend prefix."""
    _check_day(day)
    if day.weekday() >= 5:
        return f"{day.isoformat()} falls on a weekend. The original date will be kept."
    if not _calendar().is_session(day):
        return f"{day.isoformat()} falls on a US market holiday. The original date will be kept."
    return None


def session_close(day: date) -> datetime:
    """Return the actual UTC close, including early closures, for a US equity session."""
    _check_day(day)
    calendar = _calendar()
    if not calendar.is_session(day):
        raise ValueError(f"{day.isoformat()} is not a US equity trading session.")
    return calendar.session_close(day).to_pydatetime()


def next_trading_day(day: date) -> date:
    """Keep an open date or map a closure to the next session, without editing trades."""
    _check_day(day)
    return _calendar().date_to_session(day, direction="next").date()


def sessions_between(start: date, end: date) -> tuple[date, ...]:
    """Return completed or future session dates within an inclusive date interval."""
    _check_day(start)
    _check_day(end)
    if start > end:
        return ()
    return tuple(day.date() for day in _calendar().sessions_in_range(start, end))


def latest_completed_session(now: datetime | None = None) -> date:
    """Find the last closed session; a session becomes completed at its close instant."""
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Market timestamps must include a timezone.")
    instant = instant.astimezone(timezone.utc)
    day = instant.date()
    _check_day(day)
    calendar = _calendar()
    candidate = calendar.date_to_session(day, direction="previous")
    if calendar.session_close(candidate).to_pydatetime() > instant:
        earlier = candidate.date() - timedelta(days=1)
        _check_day(earlier)
        candidate = calendar.date_to_session(earlier, direction="previous")
    return candidate.date()
