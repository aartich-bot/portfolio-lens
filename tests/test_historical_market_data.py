"""Historical close and split cache behavior with Yahoo as the only fake boundary."""

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd

from portfolio_lens import historical_market_data
from portfolio_lens.historical_market_data import HistoricalMarketClient
from portfolio_lens.storage import TransactionStore

NOW = datetime(2026, 5, 18, 21, tzinfo=timezone.utc)
NEXT = datetime(2026, 5, 19, 21, tzinfo=timezone.utc)


def frame(days, closes, splits=None, dividends=None):
    return pd.DataFrame(
        {
            "Close": closes,
            "Stock Splits": splits if splits is not None else [0] * len(days),
            "Dividends": dividends if dividends is not None else [0] * len(days),
        },
        index=pd.DatetimeIndex(days).tz_localize("America/New_York"),
    )


class YahooTicker:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def get_history_metadata(self):
        return {"currency": getattr(self, "currency", "USD")}


def install(monkeypatch, results):
    tickers = {symbol: YahooTicker(result) for symbol, result in results.items()}
    monkeypatch.setattr(historical_market_data.yf, "Ticker", tickers.__getitem__)
    return tickers


def db(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    TransactionStore(path)
    return path


def test_exact_close_dates_and_unadjusted_dividend_with_index_support(tmp_path, monkeypatch):
    path = db(tmp_path)
    yahoo = install(
        monkeypatch,
        {
            "AAPL": frame(
                ["2026-05-14", "2026-05-15", "2026-05-18", "2026-05-19"],
                [100, 96, 97, 999],
                dividends=[0, 4, 0, 0],
            ),
            "^GSPC": frame(["2026-05-15", "2026-05-18"], [5000, 5010]),
        },
    )
    result = HistoricalMarketClient(path).get_history(
        ["aapl", "^gspc"], date(2026, 5, 15), date(2026, 5, 18), now=NOW
    )
    assert result.prices["AAPL"] == {
        date(2026, 5, 15): Decimal("96"),
        date(2026, 5, 18): Decimal("97"),
    }
    assert result.prices["^GSPC"] == {
        date(2026, 5, 15): Decimal("5000"),
        date(2026, 5, 18): Decimal("5010"),
    }
    assert result.fetched_at == {"AAPL": NOW, "^GSPC": NOW}
    assert yahoo["AAPL"].calls[0]["start"] == date(2026, 5, 15)
    assert yahoo["AAPL"].calls[0]["end"] == date(2026, 5, 19)
    assert yahoo["AAPL"].calls[0]["auto_adjust"] is False
    assert yahoo["AAPL"].calls[0]["actions"] is True
    assert yahoo["AAPL"].calls[0]["interval"] == "1d"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT revision FROM app_state").fetchone()[0] == 0


def test_restart_cache_and_range_expansion_replace_whole_series(tmp_path, monkeypatch):
    path = db(tmp_path)
    yahoo = install(monkeypatch, {"AAPL": frame(["2026-05-15", "2026-05-18"], [100, 110])})["AAPL"]
    first = HistoricalMarketClient(path).get_history(
        ["AAPL"], date(2026, 5, 15), date(2026, 5, 18), now=NOW
    )
    yahoo.result = RuntimeError("cache expected")
    cached = HistoricalMarketClient(path).get_history(
        ["AAPL"], date(2026, 5, 15), date(2026, 5, 18), now=NOW
    )
    assert cached.prices == first.prices
    assert cached.stale_tickers == ()
    assert len(yahoo.calls) == 1
    yahoo.result = frame(["2026-05-14", "2026-05-15", "2026-05-18"], [90, 101, 111])
    expanded = HistoricalMarketClient(path).get_history(
        ["AAPL"], date(2026, 5, 14), date(2026, 5, 18), now=NOW
    )
    assert expanded.prices["AAPL"] == {
        date(2026, 5, 14): Decimal("90"),
        date(2026, 5, 15): Decimal("101"),
        date(2026, 5, 18): Decimal("111"),
    }
    assert yahoo.calls[-1]["start"] == date(2026, 5, 14)
    assert len(yahoo.calls) == 2


def test_force_refresh_replaces_price_and_split_basis_atomically(tmp_path, monkeypatch):
    path = db(tmp_path)
    yahoo = install(
        monkeypatch,
        {"AAPL": frame(["2026-05-15", "2026-05-18"], [100, 110], [0, 2])},
    )["AAPL"]
    client = HistoricalMarketClient(path)
    first = client.get_history(
        ["AAPL"],
        date(2026, 5, 15),
        date(2026, 5, 18),
        split_start={"AAPL": date(2026, 5, 15)},
        now=NOW,
    )
    assert first.splits["AAPL"][0].ratio == 2
    yahoo.result = frame(["2026-05-15", "2026-05-18"], [50, 55], [0, 4])
    second = client.get_history(
        ["AAPL"],
        date(2026, 5, 15),
        date(2026, 5, 18),
        split_start={"AAPL": date(2026, 5, 15)},
        force=True,
        now=NOW,
    )
    assert second.prices["AAPL"] == {
        date(2026, 5, 15): Decimal("50"),
        date(2026, 5, 18): Decimal("55"),
    }
    assert [(event.date, event.ratio) for event in second.splits["AAPL"]] == [
        (date(2026, 5, 18), Decimal("4"))
    ]
    assert len(yahoo.calls) == 2


def test_failed_refresh_returns_old_history_and_marks_stale(tmp_path, monkeypatch):
    path = db(tmp_path)
    yahoo = install(monkeypatch, {"AAPL": frame(["2026-05-15", "2026-05-18"], [100, 110])})["AAPL"]
    HistoricalMarketClient(path).get_history(
        ["AAPL"], date(2026, 5, 15), date(2026, 5, 18), now=NOW
    )
    yahoo.result = RuntimeError("offline")
    stale = HistoricalMarketClient(path).get_history(
        ["AAPL"], date(2026, 5, 15), date(2026, 5, 19), now=NEXT
    )
    assert stale.prices["AAPL"] == {
        date(2026, 5, 15): Decimal("100"),
        date(2026, 5, 18): Decimal("110"),
    }
    assert stale.fetched_at["AAPL"] == NOW
    assert stale.stale_tickers == ("AAPL",)
    assert any("offline" in warning for warning in stale.warnings)
    assert any("2026-05-19" in warning for warning in stale.warnings)
    missing = HistoricalMarketClient(db(tmp_path / "fresh")).get_history(
        ["AAPL"], date(2026, 5, 15), date(2026, 5, 18), now=NOW
    )
    assert missing.prices["AAPL"] == {}
    assert missing.warnings


def test_nonfinite_rows_leave_visible_gaps_and_incomplete_action_coverage(tmp_path, monkeypatch):
    path = db(tmp_path)
    install(
        monkeypatch,
        {
            "AAPL": frame(
                ["2026-05-14", "2026-05-15", "2026-05-18"], [100, float("nan"), float("inf")]
            )
        },
    )
    result = HistoricalMarketClient(path).get_history(
        ["AAPL"],
        date(2026, 5, 14),
        date(2026, 5, 18),
        split_start={"AAPL": date(2026, 5, 14)},
        now=NOW,
    )
    assert result.prices["AAPL"] == {date(2026, 5, 14): Decimal("100")}
    assert "AAPL" in result.split_errors
    assert any("gaps" in warning.lower() for warning in result.warnings)


def test_intraday_split_requires_raw_coverage_through_today(tmp_path, monkeypatch):
    path = db(tmp_path)
    now = datetime(2024, 6, 10, 15, tzinfo=timezone.utc)
    yahoo = install(
        monkeypatch,
        {"NVDA": frame(["2024-06-07", "2024-06-10"], [110, 120], [0, 10])},
    )["NVDA"]
    result = HistoricalMarketClient(path).get_history(
        ["NVDA"],
        date(2024, 6, 7),
        date(2024, 6, 7),
        split_start={"NVDA": date(2024, 6, 7)},
        now=now,
    )
    assert result.prices["NVDA"] == {date(2024, 6, 7): Decimal("110")}
    assert [(event.date, event.ratio) for event in result.splits["NVDA"]] == [
        (date(2024, 6, 10), Decimal("10"))
    ]
    assert result.split_errors == {}
    assert yahoo.calls[0]["end"] == date(2024, 6, 11)


def test_missing_current_split_row_never_claims_raw_coverage(tmp_path, monkeypatch):
    path = db(tmp_path)
    now = datetime(2024, 6, 10, 15, tzinfo=timezone.utc)
    install(monkeypatch, {"NVDA": frame(["2024-06-07"], [110])})
    result = HistoricalMarketClient(path).get_history(
        ["NVDA"],
        date(2024, 6, 7),
        date(2024, 6, 7),
        split_start={"NVDA": date(2024, 6, 7)},
        now=now,
    )
    assert result.prices["NVDA"] == {date(2024, 6, 7): Decimal("110")}
    assert "NVDA" in result.split_errors
    assert result.splits["NVDA"] == ()


def test_intraday_split_fetch_cannot_cache_unfinished_price_as_completed(tmp_path, monkeypatch):
    path = db(tmp_path)
    intraday = datetime(2024, 6, 10, 15, tzinfo=timezone.utc)
    after_close = datetime(2024, 6, 10, 21, tzinfo=timezone.utc)
    yahoo = install(
        monkeypatch,
        {"NVDA": frame(["2024-06-07", "2024-06-10"], [110, 120], [0, 10])},
    )["NVDA"]
    client = HistoricalMarketClient(path)
    first = client.get_history(
        ["NVDA"],
        date(2024, 6, 7),
        date(2024, 6, 7),
        split_start={"NVDA": date(2024, 6, 7)},
        now=intraday,
    )
    assert first.prices["NVDA"] == {date(2024, 6, 7): Decimal(110)}
    yahoo.result = frame(["2024-06-07", "2024-06-10"], [110, 125], [0, 10])
    second = client.get_history(
        ["NVDA"],
        date(2024, 6, 7),
        date(2024, 6, 10),
        split_start={"NVDA": date(2024, 6, 7)},
        now=after_close,
    )
    assert second.prices["NVDA"][date(2024, 6, 10)] == Decimal(125)
    assert len(yahoo.calls) == 2


def test_native_non_usd_close_never_replaces_cached_usd_series(tmp_path, monkeypatch):
    path = db(tmp_path)
    yahoo = install(monkeypatch, {"AAPL": frame(["2026-05-15", "2026-05-18"], [100, 110])})["AAPL"]
    yahoo.currency = "USD"
    client = HistoricalMarketClient(path)
    client.get_history(["AAPL"], FRIDAY := date(2026, 5, 15), date(2026, 5, 18), now=NOW)
    yahoo.currency = "CAD"
    yahoo.result = frame(["2026-05-15", "2026-05-18"], [50, 55])
    result = client.get_history(["AAPL"], FRIDAY, date(2026, 5, 18), force=True, now=NOW)
    assert result.prices["AAPL"][date(2026, 5, 18)] == Decimal(110)
    assert result.stale_tickers == ("AAPL",)
    assert any("CAD" in warning for warning in result.warnings)


def test_unsupported_historical_range_returns_unavailable_snapshot(tmp_path, monkeypatch):
    path = db(tmp_path)
    yahoo = install(monkeypatch, {"AAPL": frame(["2026-05-18"], [110])})["AAPL"]
    result = HistoricalMarketClient(path).get_history(
        ["AAPL"], date(1989, 12, 29), date(2026, 5, 18), now=NOW
    )
    assert result.prices["AAPL"] == {}
    assert "AAPL" in result.split_errors or result.warnings
    assert len(yahoo.calls) == 0
