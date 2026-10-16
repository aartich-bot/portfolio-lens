"""US exchange closures, completed-session boundaries, and preserved trade dates."""

from datetime import date, datetime, timezone

import pytest

from portfolio_lens.trading_calendar import (
    latest_completed_session,
    next_trading_day,
    non_trading_reason,
    session_close,
    sessions_between,
)


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_sunday_and_memorial_day_warn_without_changing_dates():
    sunday = date(2026, 4, 19)
    memorial_day = date(2026, 5, 25)
    assert "2026-04-19 falls on a weekend" in non_trading_reason(sunday)
    assert "2026-05-25 falls on a US market holiday" in non_trading_reason(memorial_day)
    assert non_trading_reason(date(2026, 5, 26)) is None
    assert sunday == date(2026, 4, 19)
    assert memorial_day == date(2026, 5, 25)


def test_monday_before_close_and_holiday_use_fridays_completed_session():
    assert latest_completed_session(utc(2026, 5, 18, 19, 59)) == date(2026, 5, 15)
    assert latest_completed_session(utc(2026, 5, 18, 20)) == date(2026, 5, 18)
    assert latest_completed_session(utc(2026, 5, 25, 23)) == date(2026, 5, 22)


def test_early_close_is_complete_at_1300_eastern():
    assert session_close(date(2026, 11, 27)) == utc(2026, 11, 27, 18)
    assert latest_completed_session(utc(2026, 11, 27, 17, 59)) == date(2026, 11, 25)
    assert latest_completed_session(utc(2026, 11, 27, 18)) == date(2026, 11, 27)


def test_regular_close_has_dst_and_timezone_information():
    assert session_close(date(2026, 7, 6)) == utc(2026, 7, 6, 20)
    assert session_close(date(2026, 1, 5)) == utc(2026, 1, 5, 21)


def test_invalid_session_and_out_of_range_have_clear_errors():
    with pytest.raises(ValueError, match="trading session"):
        session_close(date(2026, 5, 25))
    with pytest.raises(ValueError, match="supported"):
        non_trading_reason(date(1800, 1, 1))
    with pytest.raises(ValueError, match="timezone"):
        latest_completed_session(datetime(2026, 5, 18, 20))


def test_next_session_keeps_open_dates_and_maps_closures_forward():
    assert next_trading_day(date(2026, 4, 19)) == date(2026, 4, 20)
    assert next_trading_day(date(2026, 5, 25)) == date(2026, 5, 26)
    assert next_trading_day(date(2026, 5, 26)) == date(2026, 5, 26)


def test_history_sessions_are_inclusive_and_skip_holidays_and_weekends():
    assert sessions_between(date(2026, 5, 22), date(2026, 5, 26)) == (
        date(2026, 5, 22),
        date(2026, 5, 26),
    )
    assert sessions_between(date(2026, 5, 23), date(2026, 5, 25)) == ()
    assert sessions_between(date(2026, 5, 26), date(2026, 5, 22)) == ()
    with pytest.raises(ValueError, match="supported"):
        next_trading_day(date(1800, 1, 1))
