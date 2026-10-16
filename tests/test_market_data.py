"""Real persistent cache behavior with only the external Yahoo boundary replaced."""

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from portfolio_lens import market_data
from portfolio_lens.market_data import MarketDataClient
from portfolio_lens.storage import TransactionStore

NOW = datetime(2026, 5, 18, 21, tzinfo=timezone.utc)
NEXT = datetime(2026, 5, 19, 21, tzinfo=timezone.utc)


def history(days=("2026-05-15", "2026-05-18"), closes=(100, 110), splits=None):
    index = pd.DatetimeIndex(days).tz_localize("America/New_York")
    return pd.DataFrame(
        {"Close": closes, "Stock Splits": splits or [0] * len(days), "Dividends": [0] * len(days)},
        index=index,
    )


class YahooTicker:
    def __init__(self, frame, metadata=None, failure=None):
        self.frame = frame
        self.metadata = metadata or {"quoteType": "EQUITY", "sector": "Technology"}
        self.failure = failure
        self.requests = []

    def history(self, **kwargs):
        self.requests.append(kwargs)
        assert kwargs["auto_adjust"] is False
        assert kwargs["actions"] is True
        assert kwargs["timeout"] <= 15
        if self.failure:
            raise self.failure
        return self.frame

    def get_info(self):
        if isinstance(self.metadata, Exception):
            raise self.metadata
        return self.metadata


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    TransactionStore(path)
    return path


def install_yahoo(monkeypatch, ticker):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda symbol: ticker)


def test_completed_close_and_previous_close_persist_across_client_restart(db_path, monkeypatch):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    snapshot = MarketDataClient(db_path).get_snapshot(["aapl"], now=NOW)
    quote = snapshot.quotes["AAPL"]
    assert quote.price == Decimal("110")
    assert quote.previous_close == Decimal("100")
    assert quote.as_of == datetime(2026, 5, 18, 20, tzinfo=timezone.utc)
    assert quote.previous_as_of == datetime(2026, 5, 15, 20, tzinfo=timezone.utc)
    assert quote.fetched_at == NOW
    assert quote.stale is False
    ticker.failure = RuntimeError("network must not be needed for daily cache")
    cached = MarketDataClient(db_path).get_snapshot(["AAPL"], now=NOW)
    assert cached.quotes["AAPL"] == quote
    assert len(ticker.requests) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT revision FROM app_state").fetchone()[0] == 0


def test_unsupported_raw_trade_date_reports_split_error_without_crashing(db_path, monkeypatch):
    install_yahoo(monkeypatch, YahooTicker(history()))
    snapshot = MarketDataClient(db_path).get_snapshot(
        ["AAPL"], split_start={"AAPL": date(1989, 12, 29)}, now=NOW
    )
    assert "AAPL" in snapshot.split_errors
    assert snapshot.warnings


def test_intraday_split_is_applied_even_though_prices_use_completed_sessions(db_path, monkeypatch):
    ticker = YahooTicker(
        history(("2024-06-06", "2024-06-07", "2024-06-10"), (100, 110, 120), [0, 0, 10])
    )
    install_yahoo(monkeypatch, ticker)
    now = datetime(2024, 6, 10, 15, tzinfo=timezone.utc)
    snapshot = MarketDataClient(db_path).get_snapshot(
        ["NVDA"], split_start={"NVDA": date(2024, 6, 7)}, now=now
    )
    assert snapshot.quotes["NVDA"].as_of.date() == date(2024, 6, 7)
    assert snapshot.quotes["NVDA"].price == 110
    assert snapshot.split_errors == {}
    assert [(event.date, event.ratio) for event in snapshot.splits["NVDA"]] == [
        (date(2024, 6, 10), Decimal("10"))
    ]


def test_next_session_failure_returns_persistent_stale_price(db_path, monkeypatch):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    MarketDataClient(db_path).get_snapshot(["AAPL"], now=NOW)
    ticker.failure = RuntimeError("offline")
    result = MarketDataClient(db_path).get_snapshot(["AAPL"], now=NEXT)
    quote = result.quotes["AAPL"]
    assert quote.price == Decimal("110")
    assert quote.stale is True
    assert "offline" in quote.error
    assert quote.fetched_at == NOW
    assert result.warnings
    assert len(ticker.requests) == 2


def test_current_incomplete_candle_is_excluded(db_path, monkeypatch):
    ticker = YahooTicker(history(("2026-05-15", "2026-05-18", "2026-05-19"), (100, 110, 900)))
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(["AAPL"], now=NOW)
    assert result.quotes["AAPL"].price == Decimal("110")
    assert ticker.requests[0]["end"] == date(2026, 5, 19)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "broken"])
def test_invalid_new_close_never_overwrites_good_cached_price(db_path, monkeypatch, bad):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    client = MarketDataClient(db_path)
    client.get_snapshot(["AAPL"], now=NOW)
    ticker.frame = history(("2026-05-18", "2026-05-19"), (110, bad))
    result = client.get_snapshot(["AAPL"], force=True, now=NEXT)
    assert result.quotes["AAPL"].price == Decimal("110")
    assert result.quotes["AAPL"].stale is True
    assert result.quotes["AAPL"].error
    ticker.failure = RuntimeError("offline")
    assert MarketDataClient(db_path).get_snapshot(["AAPL"], now=NEXT).quotes["AAPL"].price == 110


def test_missing_ticker_keeps_price_none(db_path, monkeypatch):
    ticker = YahooTicker(pd.DataFrame())
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(["MISSING"], now=NOW)
    assert result.quotes["MISSING"].price is None
    assert result.quotes["MISSING"].error
    assert result.warnings


def test_previous_close_requires_actual_preceding_exchange_session(db_path, monkeypatch):
    ticker = YahooTicker(history(("2026-05-14", "2026-05-18"), (100, 110)))
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(["AAPL"], now=NOW)
    assert result.quotes["AAPL"].price == 110
    assert result.quotes["AAPL"].previous_close is None
    assert any("preceding" in warning for warning in result.warnings)


def test_raw_split_request_expands_cached_coverage_and_keeps_dividends_unadjusted(
    db_path, monkeypatch
):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    client = MarketDataClient(db_path)
    client.get_snapshot(["NVDA"], now=NOW)
    days = pd.bdate_range("2024-06-07", "2026-05-18").strftime("%Y-%m-%d").tolist()
    closes = [120] * len(days)
    closes[-2:] = [100, 110]
    split_ratios = [0] * len(days)
    split_ratios[days.index("2024-06-10")] = 10
    ticker.frame = history(days, closes, split_ratios)
    result = client.get_snapshot(["NVDA"], split_start={"NVDA": date(2024, 6, 7)}, now=NOW)
    assert ticker.requests[-1]["start"] == date(2024, 6, 7)
    assert result.split_errors == {}
    assert len(result.splits["NVDA"]) == 1
    assert result.splits["NVDA"][0].date == date(2024, 6, 10)
    assert result.splits["NVDA"][0].ratio == Decimal("10")
    ticker.failure = RuntimeError("network unavailable")
    cached = MarketDataClient(db_path).get_snapshot(
        ["NVDA"], split_start={"NVDA": date(2024, 6, 7)}, now=NOW
    )
    assert cached.splits == result.splits
    assert cached.split_errors == {}
    assert len(ticker.requests) == 2


def test_short_history_does_not_claim_missing_raw_split_history_is_complete(db_path, monkeypatch):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(
        ["NVDA"], split_start={"NVDA": date(2020, 1, 2)}, now=NOW
    )
    assert result.quotes["NVDA"].price == 110
    assert "NVDA" in result.split_errors
    assert result.warnings


def test_failed_extended_split_fetch_does_not_mark_existing_daily_quote_stale(db_path, monkeypatch):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    client = MarketDataClient(db_path)
    client.get_snapshot(["NVDA"], now=NOW)
    ticker.failure = RuntimeError("split provider offline")
    result = client.get_snapshot(["NVDA"], split_start={"NVDA": date(2020, 1, 2)}, now=NOW)
    assert result.quotes["NVDA"].price == 110
    assert "NVDA" in result.split_errors
    assert result.quotes["NVDA"].stale is False


def test_missing_actions_column_blocks_raw_split_coverage_but_keeps_quote(db_path, monkeypatch):
    ticker = YahooTicker(history().drop(columns=["Stock Splits"]))
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(
        ["AAPL"], split_start={"AAPL": date(2026, 5, 15)}, now=NOW
    )
    assert result.quotes["AAPL"].price == 110
    assert "AAPL" in result.split_errors


@pytest.mark.parametrize(
    "symbol, expected", [("JNJ", "Healthcare"), ("JPM", "Financial Services"), ("ODD", "Unknown")]
)
def test_metadata_failure_uses_fallback_without_discarding_price(
    db_path, monkeypatch, symbol, expected
):
    ticker = YahooTicker(history(), metadata=RuntimeError("metadata offline"))
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot([symbol], now=NOW)
    assert result.quotes[symbol].price == 110
    assert result.quotes[symbol].sector == expected
    assert result.warnings


def test_etf_metadata_identifies_fund_sector(db_path, monkeypatch):
    ticker = YahooTicker(history(), metadata={"quoteType": "ETF"})
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(["VOO"], now=NOW)
    assert result.quotes["VOO"].quote_type == "ETF"
    assert result.quotes["VOO"].sector == "ETF"


def test_non_usd_quote_is_not_used_for_usd_portfolio(db_path, monkeypatch):
    ticker = YahooTicker(history(), metadata={"quoteType": "EQUITY", "currency": "CAD"})
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(["FOREIGN"], now=NOW)
    assert result.quotes["FOREIGN"].price is None
    assert "CAD" in result.quotes["FOREIGN"].error


def test_successful_next_session_replaces_daily_quote_and_keeps_old_fetch_time_until_then(
    db_path, monkeypatch
):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    client = MarketDataClient(db_path)
    client.get_snapshot(["AAPL"], now=NOW)
    ticker.frame = history(("2026-05-18", "2026-05-19"), (110, 120))
    result = client.get_snapshot(["AAPL"], now=NEXT)
    assert result.quotes["AAPL"].price == 120
    assert result.quotes["AAPL"].previous_close == 110
    assert result.quotes["AAPL"].fetched_at == NEXT
    assert result.quotes["AAPL"].stale is False


def test_recent_raw_trade_coverage_does_not_require_unneeded_older_history(db_path, monkeypatch):
    ticker = YahooTicker(history())
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(
        ["AAPL"], split_start={"AAPL": date(2026, 5, 15)}, now=NOW
    )
    assert result.split_errors == {}
    assert ticker.requests[0]["start"] == date(2026, 5, 15)


def test_gaps_within_raw_history_are_not_certified_as_no_splits(db_path, monkeypatch):
    ticker = YahooTicker(history(("2024-06-07", "2026-05-15", "2026-05-18"), (100, 100, 110)))
    install_yahoo(monkeypatch, ticker)
    result = MarketDataClient(db_path).get_snapshot(
        ["NVDA"], split_start={"NVDA": date(2024, 6, 7)}, now=NOW
    )
    assert result.quotes["NVDA"].price == 110
    assert "NVDA" in result.split_errors


def test_partial_refresh_keeps_previously_valid_cached_split_events(db_path, monkeypatch):
    ticker = YahooTicker(history(splits=[2, 0]))
    install_yahoo(monkeypatch, ticker)
    client = MarketDataClient(db_path)
    original = client.get_snapshot(["AAPL"], split_start={"AAPL": date(2026, 5, 15)}, now=NOW)
    assert original.splits["AAPL"][0].ratio == 2
    ticker.frame = history(("2026-05-18",), (110,))
    refreshed = client.get_snapshot(
        ["AAPL"], split_start={"AAPL": date(2026, 5, 15)}, force=True, now=NOW
    )
    assert refreshed.splits["AAPL"] == original.splits["AAPL"]
