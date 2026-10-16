"""Exercise the Performance screen with deterministic histories and real calculations."""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from portfolio_lens.models import ALREADY_ADJUSTED, HistoricalSnapshot
from portfolio_lens.services.transactions import TransactionService
from portfolio_lens.trading_calendar import sessions_between

PROJECT = Path(__file__).resolve().parents[1]
END = date(2026, 10, 12)


def history(self, tickers, start, end, **kwargs):
    sessions = sessions_between(start, end)
    prices = {
        ticker: {day: Decimal("100") if ticker == "^GSPC" else Decimal("150") for day in sessions}
        for ticker in tickers
    }
    return HistoricalSnapshot(
        prices,
        {},
        {},
        fetched_at={ticker: datetime(2026, 10, 13, tzinfo=timezone.utc) for ticker in tickers},
    )


@pytest.fixture
def performance_app(tmp_path, monkeypatch):
    db_path = tmp_path / "portfolio.db"
    monkeypatch.setenv("PORTFOLIO_LENS_DB", str(db_path))
    monkeypatch.setattr(
        "portfolio_lens.historical_market_data.HistoricalMarketClient.get_history", history
    )
    # Fix the market date so screenshots/tests do not change with the wall clock.
    monkeypatch.setattr(
        "portfolio_lens.services.performance.latest_completed_session", lambda now=None: END
    )
    service = TransactionService(db_path, today=date(2026, 10, 13))
    preview = service.preview_csv(
        b"ticker,date,transaction_type,quantity,price,broker\n"
        b"AAPL,2025-01-02,BUY,10,100,Fidelity\n"
        b"AAPL,2025-04-19,SELL,2,120,Fidelity\n",
        "performance.csv",
        ALREADY_ADJUSTED,
    )
    service.commit(preview)
    app = AppTest.from_file(PROJECT / "app.py", default_timeout=30)
    app.session_state["active_page"] = "Performance"
    return app


def cards(app):
    return {item.label: item.value for item in app.metric}


def traces(app):
    return json.loads(app.get("plotly_chart")[0].proto.spec)["data"]


def test_performance_cards_markers_verdict_and_disclosed_mapping(performance_app):
    app = performance_app.run()
    assert not app.exception
    values = cards(app)
    assert values["Total invested"] == "$1,000.00"
    assert values["Total sold"] == "$240.00"
    assert values["Current value"] == "$1,200.00"
    assert values["Total return"] == "+44.00%"
    assert values["XIRR"] != "Unavailable"
    assert any("ahead" in item.value and "percentage points" in item.value for item in app.success)
    assert any("2025-04-19" in item.value and "2025-04-21" in item.value for item in app.warning)
    assert len(app.get("plotly_chart")) == 2
    assert {trace["name"] for trace in traces(app)} == {
        "Your portfolio",
        "Simulated S&P 500",
        "Buys",
        "Sells",
    }
    assert any("Dividends" in item.value for item in app.caption)
    assert any(item.label == "Assumptions" for item in app.expander)


def test_range_and_benchmark_controls_only_change_chart(performance_app):
    app = performance_app.run()
    original = cards(app)
    all_days = traces(app)[0]["x"]
    app.get("button_group")[0].set_value("1M").run()
    assert not app.exception
    assert cards(app) == original
    assert len(traces(app)[0]["x"]) < len(all_days)
    next(item for item in app.checkbox if item.label == "Show S&P 500 benchmark").uncheck().run()
    assert cards(app) == original
    assert "Simulated S&P 500" not in {trace["name"] for trace in traces(app)}


def test_missing_terminal_price_has_notice_not_zero(performance_app, monkeypatch):
    def missing(self, tickers, start, end, **kwargs):
        snapshot = history(self, tickers, start, end, **kwargs)
        snapshot.prices["AAPL"].pop(end)
        return snapshot

    monkeypatch.setattr(
        "portfolio_lens.historical_market_data.HistoricalMarketClient.get_history", missing
    )
    app = performance_app.run()
    assert not app.exception
    assert cards(app)["Current value"] == "Unavailable"
    assert cards(app)["XIRR"] == "Unavailable"
    assert cards(app)["Total return"] == "Unavailable"
    assert any("incomplete" in item.value.lower() for item in app.warning)
    assert not app.success
    assert traces(app)[0]["y"][-1] is None


def test_infeasible_benchmark_sale_and_cached_banner(performance_app, monkeypatch):
    def infeasible(self, tickers, start, end, **kwargs):
        snapshot = history(self, tickers, start, end, **kwargs)
        snapshot.prices["^GSPC"] = {
            day: Decimal("100") if day < date(2025, 4, 21) else Decimal("1")
            for day in snapshot.prices["^GSPC"]
        }
        return replace(
            snapshot,
            warnings=("Using cached history because Yahoo Finance is unavailable.",),
            stale_tickers=("AAPL",),
        )

    monkeypatch.setattr(
        "portfolio_lens.historical_market_data.HistoricalMarketClient.get_history", infeasible
    )
    app = performance_app.run()
    assert not app.exception
    assert cards(app)["Current value"] == "$1,200.00"
    assert any("cached" in item.value.lower() for item in app.warning)
    assert any("sale" in item.value.lower() for item in [*app.warning, *app.error])
    assert not app.success


def test_performance_empty_state_does_not_request_prices(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LENS_DB", str(tmp_path / "empty.db"))

    def forbidden(*args, **kwargs):
        pytest.fail("An empty portfolio must not fetch prices")

    monkeypatch.setattr(
        "portfolio_lens.historical_market_data.HistoricalMarketClient.get_history", forbidden
    )
    app = AppTest.from_file(PROJECT / "app.py", default_timeout=30)
    app.session_state["active_page"] = "Performance"
    app.run()
    assert not app.exception
    assert not app.metric
    assert any("Add Transactions" in item.value for item in app.info)
