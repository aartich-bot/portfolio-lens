"""Daily unadjusted closes and verified split actions cached per ticker in SQLite."""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import yfinance as yf

from portfolio_lens.models import HistoricalSnapshot, SplitEvent
from portfolio_lens.trading_calendar import latest_completed_session, non_trading_reason

_YAHOO_CACHE_LOCATION: Path | None = None
_NEW_YORK = ZoneInfo("America/New_York")


def _positive_close(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _sessions(start: date, end: date) -> set[date]:
    if start > end:
        return set()
    sessions = set()
    day = start
    while day <= end:
        if non_trading_reason(day) is None:
            sessions.add(day)
        day += timedelta(days=1)
    return sessions


class HistoricalMarketClient:
    """Keep each ticker's price series and split basis in one persistent cache unit."""

    def __init__(self, db_path: str | Path):
        global _YAHOO_CACHE_LOCATION
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        location = self.db_path.parent / "yfinance-cache"
        if location != _YAHOO_CACHE_LOCATION:
            yf.set_tz_cache_location(str(location))
            _YAHOO_CACHE_LOCATION = location
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS historical_coverage (
                    ticker TEXT PRIMARY KEY,
                    price_start TEXT NOT NULL, price_end TEXT NOT NULL,
                    action_start TEXT NOT NULL, action_end TEXT NOT NULL,
                    actions_complete INTEGER NOT NULL, fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS historical_prices (
                    ticker TEXT NOT NULL, day TEXT NOT NULL, close TEXT NOT NULL,
                    PRIMARY KEY(ticker, day)
                );
                CREATE TABLE IF NOT EXISTS historical_splits (
                    ticker TEXT NOT NULL, day TEXT NOT NULL, ratio TEXT NOT NULL,
                    PRIMARY KEY(ticker, day)
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _cached(self, symbol: str):
        with self._connect() as connection:
            # A read transaction pins all three tables to the same committed
            # generation while another client refreshes this ticker.
            connection.execute("BEGIN")
            coverage = connection.execute(
                "SELECT * FROM historical_coverage WHERE ticker = ?", (symbol,)
            ).fetchone()
            prices = {
                date.fromisoformat(row["day"]): Decimal(row["close"])
                for row in connection.execute(
                    "SELECT day, close FROM historical_prices WHERE ticker = ?", (symbol,)
                )
            }
            splits = tuple(
                SplitEvent(symbol, date.fromisoformat(row["day"]), Decimal(row["ratio"]))
                for row in connection.execute(
                    "SELECT day, ratio FROM historical_splits WHERE ticker = ? ORDER BY day",
                    (symbol,),
                )
            )
        return coverage, prices, splits

    def _replace(
        self,
        symbol: str,
        prices: dict[date, Decimal],
        splits: tuple[SplitEvent, ...],
        price_start: date,
        price_end: date,
        action_start: date,
        action_end: date,
        actions_complete: bool,
        fetched_at: datetime,
    ) -> None:
        # A single transaction prevents readers from observing prices on one
        # split basis and split events on another after a refresh.
        with self._connect() as connection:
            connection.execute("DELETE FROM historical_prices WHERE ticker = ?", (symbol,))
            connection.execute("DELETE FROM historical_splits WHERE ticker = ?", (symbol,))
            connection.executemany(
                "INSERT INTO historical_prices VALUES (?, ?, ?)",
                [(symbol, day.isoformat(), str(close)) for day, close in prices.items()],
            )
            connection.executemany(
                "INSERT INTO historical_splits VALUES (?, ?, ?)",
                [(symbol, event.date.isoformat(), str(event.ratio)) for event in splits],
            )
            connection.execute(
                "INSERT OR REPLACE INTO historical_coverage VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    price_start.isoformat(),
                    price_end.isoformat(),
                    action_start.isoformat(),
                    action_end.isoformat(),
                    int(actions_complete),
                    fetched_at.isoformat(),
                ),
            )

    @staticmethod
    def _parse_frame(symbol: str, frame, start: date, end: date):
        if frame is None or frame.empty or "Close" not in frame:
            raise ValueError("No daily Close history returned.")
        prices: dict[date, Decimal] = {}
        events: dict[date, SplitEvent] = {}
        action_days: set[date] = set()
        actions_present = "Stock Splits" in frame
        for index, row in frame.iterrows():
            day = index.date()
            if not start <= day <= end or non_trading_reason(day) is not None:
                continue
            close = _positive_close(row["Close"])
            if close is not None:
                prices[day] = close
            if not actions_present or close is None:
                continue
            try:
                ratio = Decimal(str(row["Stock Splits"]))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not ratio.is_finite() or ratio < 0:
                continue
            action_days.add(day)
            if ratio:
                events[day] = SplitEvent(symbol, day, ratio)
        if not prices:
            raise ValueError("No valid daily Close history returned.")
        return prices, tuple(events[day] for day in sorted(events)), action_days

    def get_history(
        self,
        tickers: Iterable[str],
        start: date,
        end: date,
        *,
        split_start: dict[str, date] | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> HistoricalSnapshot:
        """Return inclusive daily closes, gaps, and verified raw-date split coverage."""
        if start > end:
            raise ValueError("Historical start must not be after end.")
        instant = now if now is not None else datetime.now(timezone.utc)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Market timestamps must include a timezone.")
        instant = instant.astimezone(timezone.utc)
        today = instant.astimezone(_NEW_YORK).date()
        completed_end = latest_completed_session(instant)
        raw_starts = {key.strip().upper(): day for key, day in (split_start or {}).items()}
        symbols = tuple(dict.fromkeys(str(ticker).strip().upper() for ticker in tickers))
        try:
            non_trading_reason(start)
            non_trading_reason(end)
        except ValueError as exc:
            message = str(exc)
            return HistoricalSnapshot(
                {symbol: {} for symbol in symbols if symbol},
                {symbol: () for symbol in symbols if symbol},
                {symbol: message for symbol in symbols if symbol in raw_starts},
                tuple(f"{symbol}: {message}" for symbol in symbols if symbol),
                {},
                (),
            )
        prices_out: dict[str, dict[date, Decimal]] = {}
        splits_out: dict[str, tuple[SplitEvent, ...]] = {}
        split_errors: dict[str, str] = {}
        fetched_at: dict[str, datetime] = {}
        stale: list[str] = []
        warnings: list[str] = []
        for symbol in symbols:
            if not symbol:
                continue
            raw_start = raw_starts.get(symbol)
            action_start = min(start, raw_start) if raw_start else start
            action_end = max(end, today) if raw_start else end
            coverage, all_prices, all_splits = self._cached(symbol)
            if coverage is not None:
                fetch_time = datetime.fromisoformat(coverage["fetched_at"])
                fetched_at[symbol] = fetch_time
            else:
                fetch_time = None
            has_prices = bool(
                coverage
                and date.fromisoformat(coverage["price_start"]) <= start
                and date.fromisoformat(coverage["price_end"]) >= end
            )
            has_actions = bool(
                coverage
                and coverage["actions_complete"]
                and date.fromisoformat(coverage["action_start"]) <= action_start
                and date.fromisoformat(coverage["action_end"]) >= action_end
            )
            relevant_today = end >= completed_end
            age_refresh = bool(
                fetch_time and relevant_today and fetch_time.astimezone(_NEW_YORK).date() < today
            )
            refresh = force or not has_prices or (raw_start is not None and not has_actions)
            refresh = refresh or age_refresh
            if refresh:
                fetch_start = min(
                    action_start,
                    date.fromisoformat(coverage["price_start"]) if coverage else action_start,
                    date.fromisoformat(coverage["action_start"]) if coverage else action_start,
                )
                fetch_end = max(
                    action_end,
                    date.fromisoformat(coverage["price_end"]) if coverage else action_end,
                    date.fromisoformat(coverage["action_end"]) if coverage else action_end,
                )
                try:
                    provider = yf.Ticker(symbol)
                    frame = provider.history(
                        start=fetch_start,
                        end=fetch_end + timedelta(days=1),
                        interval="1d",
                        auto_adjust=False,
                        actions=True,
                        timeout=10,
                        raise_errors=True,
                    )
                    new_prices, new_splits, action_days = self._parse_frame(
                        symbol, frame, fetch_start, fetch_end
                    )
                    metadata_method = getattr(provider, "get_history_metadata", None)
                    if metadata_method is not None:
                        currency = str((metadata_method() or {}).get("currency") or "").upper()
                        if currency and currency != "USD":
                            raise ValueError(
                                f"Quote currency {currency} cannot be used in this USD portfolio."
                            )
                    action_complete = _sessions(fetch_start, fetch_end).issubset(action_days)
                    # Action rows may include today's live candle so a split is
                    # visible before close; only completed candles become prices.
                    price_end = min(fetch_end, completed_end)
                    completed_prices = {
                        day: close for day, close in new_prices.items() if day <= price_end
                    }
                    self._replace(
                        symbol,
                        completed_prices,
                        new_splits,
                        fetch_start,
                        price_end,
                        fetch_start,
                        fetch_end,
                        action_complete,
                        instant,
                    )
                    coverage, all_prices, all_splits = self._cached(symbol)
                    fetched_at[symbol] = instant
                except Exception as exc:
                    message = str(exc) or type(exc).__name__
                    warnings.append(
                        f"{symbol}: historical refresh failed ({message}); "
                        + ("using cached history." if coverage else "history unavailable.")
                    )
                    if coverage:
                        stale.append(symbol)
            prices_out[symbol] = {
                day: close for day, close in all_prices.items() if start <= day <= end
            }
            split_from = raw_start if raw_start else start
            splits_out[symbol] = tuple(
                event for event in all_splits if split_from <= event.date <= action_end
            )
            missing = sorted(_sessions(start, end) - prices_out[symbol].keys())
            if missing:
                example = ", ".join(day.isoformat() for day in missing[:3])
                warnings.append(
                    f"{symbol}: historical price gaps on {len(missing)} session(s), including {example}."
                )
            if raw_start is not None:
                has_actions = bool(
                    coverage
                    and coverage["actions_complete"]
                    and date.fromisoformat(coverage["action_start"]) <= raw_start
                    and date.fromisoformat(coverage["action_end"]) >= action_end
                )
                if not has_actions:
                    message = "Full split action coverage is unavailable for raw trades."
                    split_errors[symbol] = message
                    warnings.append(f"{symbol}: {message}")
        return HistoricalSnapshot(
            prices_out,
            splits_out,
            split_errors,
            tuple(dict.fromkeys(warnings)),
            fetched_at,
            tuple(stale),
        )
