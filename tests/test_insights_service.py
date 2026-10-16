"""Verify Insights connects a single price basis to risk, returns, and safe summary facts."""

from datetime import date, datetime, timezone
from decimal import Decimal

from portfolio_lens.models import ALREADY_ADJUSTED, HistoricalSnapshot, MarketQuote, MarketSnapshot
from portfolio_lens.trading_calendar import sessions_between

END = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 22, tzinfo=timezone.utc)


def rows():
    return [
        {
            "ticker": ticker,
            "date": "2026-01-02",
            "transaction_type": "BUY",
            "quantity": quantity,
            "price": "100",
            "broker": broker,
            "split_convention": ALREADY_ADJUSTED,
            "source_row": 2,
        }
        for ticker, quantity, broker in [
            ("NVDA", "18", "Fidelity"),
            ("NVDA", "18", "Robinhood"),
            ("AAPL", "24", "Fidelity"),
            ("JNJ", "20", "Vanguard"),
            ("JPM", "20", "Vanguard"),
        ]
    ]


class History:
    def __init__(self, missing=False, pending_warning=False):
        self.missing = missing
        self.pending_warning = pending_warning

    def get_history(self, tickers, start, end, **kwargs):
        prices = {
            ticker: {day: Decimal(100) for day in sessions_between(start, end)}
            for ticker in tickers
        }
        if self.missing:
            prices["NVDA"].pop(end)
        return HistoricalSnapshot(
            prices, {}, {}, ("Using cached history.",) if self.pending_warning else ()
        )


class Quotes:
    def get_snapshot(self, tickers, **kwargs):
        sectors = {
            "NVDA": "Technology",
            "AAPL": "Technology",
            "JNJ": "Healthcare",
            "JPM": "Financial Services",
        }
        # A deliberately different quote basis must not replace historical endpoint prices.
        return MarketSnapshot(
            {
                ticker: MarketQuote(ticker, Decimal(999), sector=sectors[ticker])
                for ticker in tickers
            },
            {},
            {},
        )


def analyze(tmp_path, **kwargs):
    from portfolio_lens.services.insights import analyze_insights

    return analyze_insights(
        rows(),
        tmp_path / "portfolio.db",
        history_client=History(),
        quote_client=Quotes(),
        now=NOW,
        **kwargs,
    )


def test_consolidated_risk_and_exports_use_the_same_history_prices(tmp_path):
    result = analyze(tmp_path)
    assert result.valuation.current_value == Decimal(10000)
    nvda = next(
        item for item in result.risk.alerts if item.kind == "holding" and "NVDA" in item.message
    )
    assert "36" in nvda.message and "2 brokers" in nvda.message
    assert result.drawdown.max_drawdown == 0
    assert result.projection.scenarios
    assert result.projection.scenarios[1].points[-1].balance == 70000
    assert result.risk.complete


def test_summary_facts_contain_aggregates_and_no_transaction_metadata(tmp_path):
    result = analyze(tmp_path)
    text = repr(result.facts)
    assert "NVDA" in text and "36" in text
    for forbidden in (
        "Fidelity",
        "Robinhood",
        "Vanguard",
        "source_row",
        "split_convention",
        "2026-01-02",
    ):
        assert forbidden not in text


def test_changed_settings_invalidate_summary_fingerprint(tmp_path):
    from portfolio_lens.calculations.risk import RiskThresholds

    original = analyze(tmp_path)
    changed = analyze(
        tmp_path, thresholds=RiskThresholds(Decimal("0.50"), Decimal("0.90"), Decimal("0.80"))
    )
    assert changed.fingerprint != original.fingerprint
    assert not changed.risk.alerts
    assert analyze(tmp_path, monthly=Decimal(600)).fingerprint != original.fingerprint


def test_missing_end_close_disables_risk_drawdown_and_projection(tmp_path):
    from portfolio_lens.services.insights import analyze_insights

    result = analyze_insights(
        rows(),
        tmp_path / "p.db",
        history_client=History(missing=True),
        quote_client=Quotes(),
        now=NOW,
    )
    assert result.valuation.current_value is None
    assert not result.risk.complete
    assert result.drawdown.max_drawdown is None
    assert not result.projection.scenarios
    assert not any("No configured" in fact.statement for fact in result.facts)


def test_report_preserves_warnings_and_current_simulator_parameters(tmp_path):
    from portfolio_lens.services.insights import analyze_insights, make_report

    result = analyze_insights(
        rows(),
        tmp_path / "p.db",
        history_client=History(pending_warning=True),
        quote_client=Quotes(),
        now=NOW,
    )
    report = make_report(result)
    assert END.isoformat() in report.as_of and "market close" in report.as_of
    assert any("cached" in item.lower() for item in report.notices)
    assert any("500" in item and "10" in item for item in report.projection)
    assert report.summary
    assert any("2026-01-02" in item and "2026-10-12" in item for item in report.projection)


def test_sector_all_clear_fact_never_implies_unknown_exposures_passed(tmp_path):
    from portfolio_lens.calculations.risk import RiskThresholds
    from portfolio_lens.services.insights import analyze_insights

    class UnknownQuotes:
        def get_snapshot(self, tickers, **kwargs):
            return MarketSnapshot({ticker: MarketQuote(ticker) for ticker in tickers}, {}, {})

    result = analyze_insights(
        rows(),
        tmp_path / "unknown.db",
        thresholds=RiskThresholds(1, 1, 1),
        history_client=History(),
        quote_client=UnknownQuotes(),
        now=NOW,
    )
    clear = next(fact for fact in result.facts if fact.id == "concentration_clear")
    assert "known" in clear.statement and "unassessed" in clear.statement


def test_failed_raw_history_never_exports_unverified_split_quantities(tmp_path):
    from portfolio_lens.models import AS_TRADED
    from portfolio_lens.services.insights import analyze_insights

    class FailedHistory:
        def get_history(self, *args, **kwargs):
            raise OSError("Offline")

    records = [dict(row, split_convention=AS_TRADED) for row in rows()]
    result = analyze_insights(
        records, tmp_path / "raw.db", history_client=FailedHistory(), quote_client=Quotes(), now=NOW
    )
    assert result.valuation is None
    assert not result.risk.complete
    assert not result.projection.scenarios
