"""Performance orchestration preserves raw trades and unavailable states."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from portfolio_lens.models import ALREADY_ADJUSTED, AS_TRADED, HistoricalSnapshot, SplitEvent
from portfolio_lens.services.performance import analyze_performance

FRIDAY = date(2026, 5, 15)
MONDAY = date(2026, 5, 18)
TUESDAY = date(2026, 5, 19)
NOW = datetime(2026, 5, 18, 21, tzinfo=timezone.utc)


def row(day, kind="BUY", *, ticker="AAPL", qty="1", price="100", convention=ALREADY_ADJUSTED):
    return {
        "ticker": ticker,
        "date": day.isoformat(),
        "transaction_type": kind,
        "quantity": qty,
        "price": price,
        "broker": "Broker",
        "source_row": 2,
        "split_convention": convention,
    }


class Market:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def get_history(self, tickers, start, end, **kwargs):
        self.calls.append((tuple(tickers), start, end, kwargs))
        return self.snapshot


def snapshot(aapl=None, index=None, *, splits=(), split_error=None, warnings=(), stale=()):
    return HistoricalSnapshot(
        {
            "AAPL": aapl if aapl is not None else {FRIDAY: Decimal(100), MONDAY: Decimal(120)},
            "^GSPC": index if index is not None else {FRIDAY: Decimal(100), MONDAY: Decimal(120)},
        },
        {"AAPL": splits, "^GSPC": ()},
        {"AAPL": split_error} if split_error else {},
        warnings,
        {"AAPL": NOW, "^GSPC": NOW},
        stale,
    )


def test_raw_split_copy_keeps_trade_amounts_and_uses_one_market_request(tmp_path):
    original = row(FRIDAY, qty="1", price="100", convention=AS_TRADED)
    records = [original]
    market = Market(
        snapshot(
            aapl={FRIDAY: Decimal(50), MONDAY: Decimal(60)},
            splits=(SplitEvent("AAPL", MONDAY, Decimal(2)),),
        )
    )
    result = analyze_performance(records, tmp_path / "portfolio.db", market_client=market, now=NOW)
    assert records == [original]
    assert result.as_of == MONDAY
    assert [point.value for point in result.portfolio.points] == [Decimal(100), Decimal(120)]
    assert result.totals.invested == Decimal(100)
    assert result.totals.current_value == Decimal(120)
    assert result.gains[0].ticker == "AAPL"
    assert result.gains[0].unrealized == Decimal(20)
    assert result.spread == pytest.approx(0)
    assert len(market.calls) == 1
    tickers, start, end, options = market.calls[0]
    assert tickers == ("AAPL", "^GSPC")
    assert (start, end) == (FRIDAY, MONDAY)
    assert options["split_start"] == {"AAPL": FRIDAY}


def test_weekend_trade_maps_forward_but_cashflow_keeps_original_date(tmp_path):
    saturday = date(2026, 5, 16)
    records = [row(FRIDAY), row(saturday, qty="1", price="50")]
    market = Market(snapshot())
    result = analyze_performance(records, tmp_path / "portfolio.db", market_client=market, now=NOW)
    assert len(result.mappings) == 1
    assert (result.mappings[0].original, result.mappings[0].effective) == (saturday, MONDAY)
    assert [marker.effective_date for marker in result.portfolio.markers] == [FRIDAY, MONDAY]
    assert result.totals.invested == Decimal(150)
    assert result.benchmark_returns.invested == Decimal(150)
    assert [point.value for point in result.portfolio.points] == [Decimal(100), Decimal(240)]
    assert market.calls[0][1:3] == (FRIDAY, MONDAY)


def test_closed_ticker_keeps_realized_gain_and_true_zero_unrealized(tmp_path):
    records = [row(FRIDAY), row(MONDAY, "SELL", price="120")]
    market = Market(snapshot())
    result = analyze_performance(records, tmp_path / "portfolio.db", market_client=market, now=NOW)
    assert result.totals.current_value == Decimal(0)
    assert result.gains == (result.gains[0],)
    assert (result.gains[0].realized, result.gains[0].unrealized) == (Decimal(20), Decimal(0))
    assert result.benchmark.final_value == Decimal(0)
    assert result.spread == pytest.approx(0)


def test_missing_latest_held_close_disables_valuation_without_zeroing_gain(tmp_path):
    market = Market(snapshot(aapl={FRIDAY: Decimal(100)}))
    result = analyze_performance(
        [row(FRIDAY)], tmp_path / "portfolio.db", market_client=market, now=NOW
    )
    assert result.portfolio.points[-1].value is None
    assert result.totals.current_value is None
    assert result.totals.xirr.rate is None
    assert result.gains[0].unrealized is None
    assert result.spread is None
    assert any(
        "latest" in warning.lower() or "missing" in warning.lower() for warning in result.warnings
    )


def test_cached_market_warning_and_timestamp_are_visible(tmp_path):
    market = Market(snapshot(warnings=("AAPL: using cached history.",), stale=("AAPL",)))
    result = analyze_performance(
        [row(FRIDAY)], tmp_path / "portfolio.db", market_client=market, now=NOW
    )
    assert any("cached" in warning.lower() for warning in result.warnings)
    assert result.market.fetched_at["AAPL"] == NOW
    assert result.totals.current_value == Decimal(120)


def test_pending_trade_preserves_prior_points_but_disables_lifetime_values(tmp_path):
    records = [row(FRIDAY), row(TUESDAY)]
    market = Market(snapshot())
    result = analyze_performance(records, tmp_path / "portfolio.db", market_client=market, now=NOW)
    assert [point.value for point in result.portfolio.points] == [Decimal(100), Decimal(120)]
    assert result.totals.invested == Decimal(200)
    assert result.totals.current_value is None
    assert result.benchmark_returns.current_value is None
    assert result.gains == ()
    assert result.spread is None
    assert any(
        "pending" in warning.lower() or "after" in warning.lower() for warning in result.warnings
    )


def test_split_coverage_error_prevents_unsafe_portfolio_values(tmp_path):
    market = Market(snapshot(split_error="Missing raw split actions."))
    result = analyze_performance(
        [row(FRIDAY, convention=AS_TRADED)],
        tmp_path / "portfolio.db",
        market_client=market,
        now=NOW,
    )
    assert result.totals.invested == Decimal(100)
    assert result.totals.current_value is None
    assert result.gains == ()
    assert result.spread is None
    assert any("split" in error.lower() for error in result.errors)


def test_infeasible_benchmark_withdrawal_blocks_comparison(tmp_path):
    records = [row(FRIDAY), row(MONDAY, "SELL", price="200")]
    market = Market(snapshot(index={FRIDAY: Decimal(100), MONDAY: Decimal(100)}))
    result = analyze_performance(records, tmp_path / "portfolio.db", market_client=market, now=NOW)
    assert result.totals.current_value == Decimal(0)
    assert result.benchmark.infeasible
    assert result.benchmark_returns.current_value is None
    assert result.spread is None
    assert any("infeasible" in error.lower() for error in result.errors)


def test_unsupported_trade_date_is_unavailable_without_market_call(tmp_path):
    market = Market(snapshot())
    result = analyze_performance(
        [row(date(1989, 12, 29))], tmp_path / "portfolio.db", market_client=market, now=NOW
    )
    assert not market.calls
    assert result.totals.invested == Decimal(100)
    assert result.totals.current_value is None
    assert result.portfolio.points[-1].value is None
    assert result.errors
