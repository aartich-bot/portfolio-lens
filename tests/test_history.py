"""Replay on trading sessions without filling missing market prices."""

from datetime import date
from decimal import Decimal


def row(day="2025-01-03", kind="BUY", qty="2", price="100", broker="A", ticker="AAPL"):
    return {
        "date": day,
        "transaction_type": kind,
        "quantity": qty,
        "price": price,
        "ticker": ticker,
        "broker": broker,
        "source_row": 2,
    }


FRIDAY, MONDAY, TUESDAY = date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)
SESSIONS = (FRIDAY, MONDAY, TUESDAY)


def test_daily_values_and_markers_preserve_missing_quote_gap():
    from portfolio_lens.calculations.history import build_history

    result = build_history(
        [row()], {"AAPL": {FRIDAY: Decimal(100), TUESDAY: Decimal(110)}}, SESSIONS, {}
    )
    assert [p.value for p in result.points] == [Decimal(200), None, Decimal(220)]
    assert not result.complete
    assert result.issues
    assert result.markers[0].amount == Decimal(200)
    assert result.markers[0].value == Decimal(200)


def test_closed_position_needs_no_quote():
    from portfolio_lens.calculations.history import build_history

    result = build_history(
        [row(), row("2025-01-06", "SELL")], {"AAPL": {FRIDAY: Decimal(100)}}, SESSIONS, {}
    )
    assert [p.value for p in result.points] == [Decimal(200), Decimal(0), Decimal(0)]
    assert result.complete


def test_weekend_maps_next_session_but_original_trade_order_is_retained():
    from portfolio_lens.calculations.history import build_history

    saturday, sunday = date(2025, 1, 4), date(2025, 1, 5)
    result = build_history(
        [row("2025-01-05", "SELL"), row("2025-01-04")],
        {"AAPL": {}},
        SESSIONS,
        {saturday: MONDAY, sunday: MONDAY},
    )
    assert [p.value for p in result.points] == [Decimal(0)] * 3
    assert result.complete
    assert [m.kind for m in result.markers] == ["BUY", "SELL"]
    assert result.markers[0].original_date == saturday
    assert result.markers[0].effective_date == MONDAY


def test_same_day_oversell_or_cross_broker_sale_invalidates_history():
    from portfolio_lens.calculations.history import build_history

    for rows in ([row(kind="SELL"), row()], [row(), row("2025-01-06", "SELL", broker="B")]):
        result = build_history(rows, {"AAPL": {d: Decimal(100) for d in SESSIONS}}, SESSIONS, {})
        assert not result.complete
        assert all(p.value is None for p in result.points)
        assert result.issues


def test_current_basis_shares_and_prices_are_multiplied_without_double_adjustment():
    from portfolio_lens.calculations.history import build_history

    normalized = row(qty="8", price="25")
    normalized.update(raw_quantity="2", raw_price="100")
    result = build_history([normalized], {"AAPL": {FRIDAY: Decimal(25)}}, (FRIDAY,), {})
    assert result.points[0].value == Decimal(200)
    assert result.markers[0].amount == Decimal(200)


def test_future_trade_keeps_older_points_but_history_is_incomplete():
    from portfolio_lens.calculations.history import build_history

    result = build_history(
        [row(), row("2025-01-08")], {"AAPL": {d: Decimal(100) for d in SESSIONS}}, SESSIONS, {}
    )
    assert [p.value for p in result.points] == [Decimal(200)] * 3
    assert not result.complete
    assert result.issues
