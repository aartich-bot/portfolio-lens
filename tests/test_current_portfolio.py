"""Exercise portfolio navigation, consolidation, filtering, and missing-price states."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from portfolio_lens.models import ALREADY_ADJUSTED, MarketQuote, MarketSnapshot
from portfolio_lens.services.transactions import TransactionService

PROJECT = Path(__file__).resolve().parents[1]


def fake_snapshot(self, tickers, **kwargs):
    quotes = {
        ticker: MarketQuote(
            ticker,
            Decimal("100"),
            Decimal("90"),
            datetime(2026, 10, 12, 20, tzinfo=timezone.utc),
            datetime(2026, 10, 9, 20, tzinfo=timezone.utc),
            "ETF" if ticker == "VOO" else "Technology",
            "ETF" if ticker == "VOO" else "EQUITY",
            datetime(2026, 10, 13, 10, tzinfo=timezone.utc),
        )
        for ticker in tickers
    }
    return MarketSnapshot(quotes, {}, {})


@pytest.fixture
def portfolio_app(tmp_path, monkeypatch):
    db_path = tmp_path / "portfolio.db"
    monkeypatch.setenv("PORTFOLIO_LENS_DB", str(db_path))
    monkeypatch.setattr("portfolio_lens.market_data.MarketDataClient.get_snapshot", fake_snapshot)
    service = TransactionService(db_path, today=date(2026, 10, 13))
    sample = service.preview_csv(
        (PROJECT / "transactions.csv").read_bytes(), "sample.csv", ALREADY_ADJUSTED
    )
    service.commit(sample)
    app = AppTest.from_file(PROJECT / "app.py", default_timeout=30)
    app.session_state["active_page"] = "Current Portfolio"
    return app


def metric(app, label):
    return next(item for item in app.metric if item.label == label)


def test_current_portfolio_consolidates_and_filters_brokers(portfolio_app):
    app = portfolio_app.run()
    assert not app.exception
    assert metric(app, "Current value").value == "$15,300.00"
    table = app.dataframe[0].value
    assert len(table) == 7
    assert table.loc[table["Ticker"] == "NVDA", "Shares"].iloc[0] == 80
    assert any("MSFT on 2026-04-19" in item.value for item in app.warning)
    next(widget for widget in app.selectbox if widget.label == "Broker").set_value("Fidelity").run()
    assert not app.exception
    assert metric(app, "Current value").value == "$4,900.00"
    table = app.dataframe[0].value
    assert table.loc[table["Ticker"] == "NVDA", "Shares"].iloc[0] == 25
    assert len(table) == 3


def test_missing_quote_does_not_turn_into_zero_or_complete_allocation(portfolio_app, monkeypatch):
    def missing(self, tickers, **kwargs):
        snapshot = fake_snapshot(self, tickers, **kwargs)
        snapshot.quotes["NVDA"] = MarketQuote("NVDA", error="Price unavailable")
        return snapshot

    monkeypatch.setattr("portfolio_lens.market_data.MarketDataClient.get_snapshot", missing)
    app = portfolio_app.run()
    assert not app.exception
    assert metric(app, "Current value").value == "Unavailable"
    assert any("incomplete" in item.value.lower() for item in app.warning)
    assert len(app.get("plotly_chart")) == 0


def test_cached_fallback_banner_and_allocation_toggle(portfolio_app, monkeypatch):
    from dataclasses import replace

    def stale(self, tickers, **kwargs):
        snapshot = fake_snapshot(self, tickers, **kwargs)
        return MarketSnapshot(
            {ticker: replace(quote, stale=True) for ticker, quote in snapshot.quotes.items()},
            {},
            {},
            ("Using cached prices because Yahoo Finance is unavailable.",),
        )

    monkeypatch.setattr("portfolio_lens.market_data.MarketDataClient.get_snapshot", stale)
    app = portfolio_app.run()
    assert any("cached" in item.value.lower() for item in app.warning)
    assert metric(app, "Current value").value == "$15,300.00"
    next(widget for widget in app.radio if widget.label == "Allocation view").set_value(
        "By Sector"
    ).run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 1
