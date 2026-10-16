"""Portfolio valuation uses remaining FIFO basis and aligned completed sessions."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from portfolio_lens.models import MarketQuote


def record(ticker, qty, price="100", broker="Fidelity", kind="BUY", day="2025-01-02"):
    return {
        "ticker": ticker,
        "date": day,
        "quantity": qty,
        "price": price,
        "broker": broker,
        "transaction_type": kind,
        "source_row": 2,
        "split_convention": "already_adjusted",
    }


def quote(
    ticker,
    price="120",
    previous="110",
    sector="Technology",
    quote_type="EQUITY",
    day=6,
    previous_day=3,
):
    return MarketQuote(
        ticker,
        Decimal(price) if price is not None else None,
        Decimal(previous) if previous is not None else None,
        datetime(2025, 1, day, 21, tzinfo=timezone.utc),
        datetime(2025, 1, previous_day, 21, tzinfo=timezone.utc),
        sector,
        quote_type,
    )


def test_value_remaining_lots_and_daily_change_uses_current_shares():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    ledger = replay_ledger(
        [record("AAPL", "10"), record("AAPL", "4", "130", kind="SELL", day="2025-01-03")]
    )
    value = value_portfolio(ledger, {"AAPL": quote("AAPL")})
    assert value.complete
    (holding,) = value.holdings
    assert holding.shares == Decimal("6")
    assert holding.average_cost == Decimal("100")
    assert holding.cost_basis == Decimal("600")
    assert holding.market_value == Decimal("720")
    assert holding.unrealized_gain == Decimal("120")
    assert holding.unrealized_pct == Decimal("0.2")
    assert holding.weight == Decimal("1")
    assert value.current_value == Decimal("720")
    assert value.realized_gain == Decimal("120")
    assert value.today_change == Decimal("60")
    assert abs(value.today_change_pct - Decimal("0.09090909090909090909090909091")) < Decimal(
        "1e-28"
    )


def test_consolidates_only_after_broker_fifo_and_filters_all_totals_and_allocations():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    ledger = replay_ledger(
        [
            record("AAPL", "10", broker="Fidelity"),
            record("AAPL", "5", "200", broker="Robinhood"),
            record("AAPL", "4", "150", broker="Fidelity", kind="SELL", day="2025-01-03"),
            record("MSFT", "2", "50", broker="Fidelity"),
            record("MSFT", "2", "75", broker="Fidelity", kind="SELL", day="2025-01-03"),
        ]
    )
    all_value = value_portfolio(ledger, {"AAPL": quote("AAPL", "250", "240")})
    assert all_value.cost_basis == Decimal("1600")
    assert all_value.realized_gain == Decimal("250")
    assert all_value.holdings[0].shares == Decimal("11")
    assert {a.name: a.value for a in all_value.allocations["broker"]} == {
        "Fidelity": Decimal("1500"),
        "Robinhood": Decimal("1250"),
    }
    filtered = value_portfolio(ledger, {"AAPL": quote("AAPL", "250", "240")}, broker="FIDELITY")
    assert filtered.current_value == Decimal("1500")
    assert filtered.cost_basis == Decimal("600")
    assert filtered.realized_gain == Decimal("250")
    assert filtered.today_change == Decimal("60")
    assert filtered.allocations["broker"][0].weight == Decimal("1")


@pytest.mark.parametrize("bad", [None, "NaN", "0", "-1", "Infinity"])
def test_missing_or_invalid_price_never_becomes_zero_or_known_weight(bad):
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    ledger = replay_ledger([record("AAPL", "1"), record("MSFT", "1")])
    value = value_portfolio(ledger, {"AAPL": quote("AAPL"), "MSFT": quote("MSFT", bad)})
    assert not value.complete
    assert value.current_value is None
    assert value.unrealized_gain is None
    assert value.unrealized_pct is None
    assert value.today_change is None
    assert value.today_change_pct is None
    assert value.cost_basis == Decimal("200")
    assert value.holdings[0].market_value == Decimal("120")
    assert all(h.weight is None for h in value.holdings)
    assert value.holdings[1].market_value is None
    assert value.allocations == {}
    assert value.concentration is None


def test_absent_quote_is_incomplete_and_closed_positions_need_no_quotes():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    open_ledger = replay_ledger([record("AAPL", "1")])
    assert not value_portfolio(open_ledger, {}).complete
    closed = replay_ledger(
        [record("AAPL", "1"), record("AAPL", "1", "130", kind="SELL", day="2025-01-03")]
    )
    value = value_portfolio(closed, {})
    assert value.complete
    assert value.current_value == Decimal("0")
    assert value.realized_gain == Decimal("30")
    assert value.unrealized_pct is None
    assert value.concentration is None
    assert value.allocations == {}


def test_missing_previous_close_leaves_current_value_but_daily_change_unknown():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    value = value_portfolio(
        replay_ledger([record("AAPL", "1")]), {"AAPL": quote("AAPL", previous=None)}
    )
    assert value.complete
    assert value.current_value == Decimal("120")
    assert value.today_change is None
    assert value.today_change_pct is None


def test_mixed_current_sessions_do_not_present_combined_portfolio():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    value = value_portfolio(
        replay_ledger([record("AAPL", "1"), record("MSFT", "1")]),
        {"AAPL": quote("AAPL"), "MSFT": quote("MSFT", day=7)},
    )
    assert not value.complete
    assert value.current_value is None
    assert value.today_change is None
    assert value.concentration is None


def test_mixed_previous_sessions_disable_daily_change_but_keep_current_value():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    value = value_portfolio(
        replay_ledger([record("AAPL", "1"), record("MSFT", "1")]),
        {"AAPL": quote("AAPL"), "MSFT": quote("MSFT", previous_day=2)},
    )
    assert value.complete
    assert value.current_value == Decimal("240")
    assert value.today_change is None
    assert value.today_change_pct is None
    assert value.issues


@pytest.mark.parametrize(
    "amounts,expected",
    [
        ([25, 20, 15, 15, 15, 10], "Low"),
        ([26, 20, 14, 14, 13, 13], "Moderate"),
        ([25, 24, 20, 16, 15], "Moderate"),
        ([30, 20, 15, 15, 10, 10], "High"),
    ],
)
def test_concentration_strict_thresholds(amounts, expected):
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    records = [record(f"T{i}", str(amount), "1") for i, amount in enumerate(amounts)]
    quotes = {f"T{i}": quote(f"T{i}", "1", "1") for i in range(len(amounts))}
    value = value_portfolio(replay_ledger(records), quotes)
    assert value.concentration == expected
    adjusted = value_portfolio(
        replay_ledger(records),
        quotes,
        single_threshold=Decimal("0.5"),
        top3_threshold=Decimal("0.8"),
    )
    assert adjusted.concentration == "Low"


def test_top_three_exact_boundary_is_not_breached_by_repeating_decimal_weights():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    amounts = [13, 12, 8, 8, 7, 7]  # Total 55; top three 33, exactly 60%.
    ledger = replay_ledger([record(f"T{i}", str(n), "1") for i, n in enumerate(amounts)])
    quotes = {f"T{i}": quote(f"T{i}", "1", "1") for i in range(len(amounts))}
    value = value_portfolio(ledger, quotes)
    assert value.concentration == "Low"
    assert value.top3_weight == Decimal("0.6")


def test_etf_sector_and_all_allocations_are_complete_market_values():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    ledger = replay_ledger(
        [record("VOO", "1", broker="Vanguard"), record("AAPL", "2", broker="Fidelity")]
    )
    value = value_portfolio(
        ledger, {"VOO": quote("VOO", "240", quote_type="ETF"), "AAPL": quote("AAPL", "120")}
    )
    assert {h.ticker: h.sector for h in value.holdings} == {"VOO": "ETF", "AAPL": "Technology"}
    assert {a.name: a.value for a in value.allocations["sector"]} == {
        "ETF": Decimal("240"),
        "Technology": Decimal("240"),
    }
    for groups in value.allocations.values():
        assert sum(a.value for a in groups) == Decimal("480")
        assert sum(a.weight for a in groups) == Decimal("1")


def test_invalid_ledger_cannot_be_valued():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.valuation import value_portfolio

    ledger = replay_ledger([record("AAPL", "1", kind="SELL")])
    with pytest.raises(ValueError, match="ledger"):
        value_portfolio(ledger, {})
