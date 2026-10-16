"""Exercise Insights controls, safe summary generation, missing data, and download links."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from portfolio_lens.models import ALREADY_ADJUSTED, HistoricalSnapshot, MarketQuote, MarketSnapshot
from portfolio_lens.services.transactions import TransactionService
from portfolio_lens.trading_calendar import sessions_between

PROJECT = Path(__file__).resolve().parents[1]
END = date(2026, 10, 12)


def history(self, tickers, start, end, **kwargs):
    return HistoricalSnapshot(
        {ticker: {day: Decimal(100) for day in sessions_between(start, end)} for ticker in tickers},
        {},
        {},
        fetched_at={ticker: datetime(2026, 10, 13, tzinfo=timezone.utc) for ticker in tickers},
    )


@pytest.fixture
def insights_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LENS_DB", str(tmp_path / "portfolio.db"))
    monkeypatch.setattr(
        "portfolio_lens.services.performance.latest_completed_session", lambda now=None: END
    )
    monkeypatch.setattr(
        "portfolio_lens.historical_market_data.HistoricalMarketClient.get_history", history
    )
    monkeypatch.setattr(
        "portfolio_lens.market_data.MarketDataClient.get_snapshot",
        lambda self, tickers, **kwargs: MarketSnapshot(
            {
                ticker: MarketQuote(
                    ticker,
                    sector="Technology"
                    if ticker in ("NVDA", "AAPL", "MSFT")
                    else "ETF"
                    if ticker == "VOO"
                    else "Healthcare",
                )
                for ticker in tickers
            },
            {},
            {},
        ),
    )
    service = TransactionService(tmp_path / "portfolio.db", today=date(2026, 10, 13))
    service.commit(
        service.preview_csv(
            b"ticker,date,transaction_type,quantity,price,broker\n"
            b"NVDA,2026-01-02,BUY,18,100,Fidelity\n"
            b"NVDA,2026-01-02,BUY,18,100,Robinhood\n"
            b"AAPL,2026-01-02,BUY,24,100,Fidelity\n"
            b"JNJ,2026-01-02,BUY,20,100,Vanguard\n"
            b"JPM,2026-01-02,BUY,20,100,Vanguard\n",
            "insights.csv",
            ALREADY_ADJUSTED,
        )
    )
    app = AppTest.from_file(PROJECT / "app.py", default_timeout=30)
    app.session_state["active_page"] = "Insights"
    return app


def test_insights_drawdown_alerts_projection_and_downloads(insights_app):
    app = insights_app.run()
    assert not app.exception
    assert any("NVDA" in item.value and "2 brokers" in item.value for item in app.markdown)
    assert next(item for item in app.metric if item.label == "Max drawdown").value != "Unavailable"
    assert len(app.get("plotly_chart")) == 2
    assert {item.proto.label for item in app.get("download_button")} == {
        "Download holdings CSV",
        "Download transactions CSV",
        "Download summary PDF",
    }
    assert any(
        "illustrative" in item.value.lower() and "forecast" in item.value.lower()
        for item in app.caption
    )


def test_risk_sliders_recompute_alerts(insights_app):
    app = insights_app.run()
    for label in ("Single holding limit (%)", "Top three limit (%)", "Sector limit (%)"):
        next(item for item in app.slider if item.label == label).set_value(100)
    app.run()
    assert not app.exception
    assert not any('class="insight-alert' in item.value for item in app.markdown)
    assert any("No configured" in item.value for item in app.success)


def test_summary_only_calls_api_boundary_on_button_and_resets_on_change(insights_app, monkeypatch):
    from portfolio_lens.services.summary import rule_based_summary

    calls = []

    def generate(facts, *, fingerprint=None, **kwargs):
        calls.append(tuple(facts))
        return rule_based_summary(facts, fingerprint=fingerprint, notice="Generated on request.")

    monkeypatch.setattr("portfolio_lens.services.summary.generate_summary", generate)
    app = insights_app.run()
    assert not calls
    next(item for item in app.button if item.label == "Generate AI Summary").click().run()
    assert len(calls) == 1
    assert app.session_state["insights_summary"].notice == "Generated on request."
    next(item for item in app.slider if item.label == "Monthly contribution ($)").set_value(
        1000
    ).run()
    assert len(calls) == 1
    assert "insights_summary" not in app.session_state
    assert not app.exception


def test_missing_history_never_renders_zero_drawdown_or_projection(insights_app, monkeypatch):
    def missing(self, tickers, start, end, **kwargs):
        snapshot = history(self, tickers, start, end, **kwargs)
        snapshot.prices["NVDA"].pop(end)
        return snapshot

    monkeypatch.setattr(
        "portfolio_lens.historical_market_data.HistoricalMarketClient.get_history", missing
    )
    app = insights_app.run()
    assert not app.exception
    assert next(item for item in app.metric if item.label == "Max drawdown").value == "Unavailable"
    assert not app.get("plotly_chart")
    assert any(
        "unavailable" in item.value.lower() or "incomplete" in item.value.lower()
        for item in app.warning
    )
    assert not app.success


def test_empty_insights_page_asks_for_transactions(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LENS_DB", str(tmp_path / "empty.db"))
    app = AppTest.from_file(PROJECT / "app.py", default_timeout=30)
    app.session_state["active_page"] = "Insights"
    app.run()
    assert not app.exception
    assert any("Add Transactions" in item.value for item in app.info)
    assert not app.slider
