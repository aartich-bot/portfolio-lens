"""Risk checks use consolidated market values, strict limits and honest coverage."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from portfolio_lens.calculations.accounting import LedgerResult, Position
from portfolio_lens.calculations.valuation import value_portfolio
from portfolio_lens.models import MarketQuote


def portfolio(amounts, sectors=None):
    positions = tuple(
        Position(ticker, broker, Decimal(value), Decimal(value), Decimal(0))
        for ticker, broker, value in amounts
    )
    ledger = LedgerResult(positions, ())
    quotes = {
        ticker: MarketQuote(
            ticker,
            Decimal(1),
            Decimal(1),
            datetime(2025, 1, 3, tzinfo=timezone.utc),
            datetime(2025, 1, 2, tzinfo=timezone.utc),
            (sectors or {}).get(ticker, ticker),
            "ETF" if (sectors or {}).get(ticker) == "ETF" else "EQUITY",
        )
        for ticker, _, _ in amounts
    }
    return ledger, value_portfolio(ledger, quotes)


def test_nvda_is_consolidated_across_two_open_brokers():
    from portfolio_lens.calculations.risk import analyze_risk

    ledger, value = portfolio(
        [
            ("NVDA", "One", 18),
            ("NVDA", "Two", 18),
            ("NVDA", "Closed", 0),
            ("A", "One", 16),
            ("B", "One", 16),
            ("C", "One", 16),
            ("D", "One", 16),
        ]
    )
    result = analyze_risk(value, ledger)
    alert = next(alert for alert in result.alerts if alert.kind == "holding")
    assert alert.weight == Decimal(".36")
    assert "NVDA" in alert.message and "2 brokers" in alert.message
    assert result.complete


def test_exact_boundaries_and_below_have_no_alerts():
    from portfolio_lens.calculations.risk import analyze_risk

    ledger, value = portfolio(
        [
            ("A", "One", 25),
            ("B", "One", 20),
            ("C", "One", 15),
            ("D", "One", 15),
            ("E", "One", 15),
            ("F", "One", 10),
        ],
        {"A": "Tech", "B": "Tech", "F": "Finance"},
    )
    result = analyze_risk(value, ledger)
    assert result.alerts == ()
    assert result.top3_weight == Decimal(".60")
    assert result.sector_coverage == Decimal(1)
    assert result.issues == ()


def test_repeating_weights_do_not_breach_exact_top_three_boundary():
    from portfolio_lens.calculations.risk import analyze_risk

    ledger, value = portfolio([(f"T{i}", "One", n) for i, n in enumerate([13, 12, 8, 8, 7, 7])])
    assert analyze_risk(value, ledger).alerts == ()


def test_known_sector_breach_and_etf_unknown_coverage_are_disclosed():
    from portfolio_lens.calculations.risk import analyze_risk

    ledger, value = portfolio(
        [("A", "One", 30), ("B", "One", 30), ("VOO", "One", 20), ("X", "One", 20)],
        {"A": "Tech", "B": "Tech", "VOO": "ETF", "X": "Unknown"},
    )
    result = analyze_risk(value, ledger)
    sectors = [alert for alert in result.alerts if alert.kind == "sector"]
    assert len(sectors) == 1 and sectors[0].weight == Decimal(".60")
    assert result.sector_coverage == Decimal(".60")
    assert result.issues and "ETF" in " ".join(result.issues)
    assert "Unknown" in " ".join(result.issues)


def test_missing_prices_make_risk_unavailable():
    from portfolio_lens.calculations.risk import analyze_risk

    ledger, _ = portfolio([("A", "One", 10)])
    result = analyze_risk(value_portfolio(ledger, {}), ledger)
    assert not result.complete
    assert result.alerts == () and result.issues
    assert result.top3_weight is None and result.sector_coverage is None


def test_custom_thresholds_are_applied_and_invalid_thresholds_rejected():
    from portfolio_lens.calculations.risk import RiskThresholds, analyze_risk

    ledger, value = portfolio([("A", "One", 40), ("B", "One", 30), ("C", "One", 30)])
    assert analyze_risk(value, ledger, RiskThresholds(0.5, 1, 0.5)).alerts == ()
    with pytest.raises(ValueError):
        RiskThresholds(single=Decimal("NaN"))
    with pytest.raises(ValueError):
        RiskThresholds(sector=1.1)


def test_empty_portfolio_does_not_invent_concentration_weights():
    from portfolio_lens.calculations.risk import analyze_risk

    ledger = LedgerResult((), ())
    result = analyze_risk(value_portfolio(ledger, {}), ledger)
    assert result.alerts == () and result.top3_weight is None
    assert result.sector_coverage is None and result.issues
