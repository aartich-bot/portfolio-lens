"""Completed Yahoo daily closes, metadata, and split coverage cached in local SQLite."""

import queue
import sqlite3
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import yfinance as yf

from portfolio_lens.models import MarketQuote, MarketSnapshot, SplitEvent
from portfolio_lens.trading_calendar import (
    latest_completed_session,
    non_trading_reason,
    session_close,
)

_FALLBACK_SECTORS = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "JNJ": "Healthcare",
    "JPM": "Financial Services",
    "TSLA": "Consumer Cyclical",
    "VOO": "ETF",
}
_YAHOO_CACHE_LOCATION: Path | None = None
_METADATA_TIMEOUT = 2.0


def _positive_decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Close must be a finite positive number.") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("Close must be a finite positive number.")
    return result


def _metadata(ticker, symbol: str):
    """Bound optional metadata independently of the essential price request."""
    outcome = queue.Queue(maxsize=1)

    def fetch():
        try:
            outcome.put((ticker.get_info(), None))
        except Exception as exc:
            outcome.put(({}, exc))

    threading.Thread(target=fetch, daemon=True).start()
    try:
        info, error = outcome.get(timeout=_METADATA_TIMEOUT)
    except queue.Empty:
        info, error = {}, TimeoutError("metadata request timed out")
    if not isinstance(info, dict):
        info, error = {}, ValueError("invalid metadata response")
    quote_type = str(info.get("quoteType") or ("ETF" if symbol == "VOO" else "EQUITY"))
    sector = (
        "ETF"
        if quote_type.upper() == "ETF"
        else (info.get("sector") or _FALLBACK_SECTORS.get(symbol, "Unknown"))
    )
    warning = f"{symbol}: metadata unavailable ({error}); using sector fallback." if error else None
    return str(sector), quote_type.upper(), info.get("currency"), warning


class MarketDataClient:
    """Persist market data in the portfolio database without changing import revision."""

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
                CREATE TABLE IF NOT EXISTS market_quotes (
                    ticker TEXT PRIMARY KEY, price TEXT NOT NULL, previous_close TEXT,
                    as_of TEXT NOT NULL, previous_as_of TEXT, sector TEXT NOT NULL,
                    quote_type TEXT NOT NULL, fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_splits (
                    ticker TEXT NOT NULL, event_date TEXT NOT NULL, ratio TEXT NOT NULL,
                    PRIMARY KEY(ticker, event_date)
                );
                CREATE TABLE IF NOT EXISTS market_split_coverage (
                    ticker TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL
                );
            """)

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _cached_quote(self, symbol: str) -> MarketQuote | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_quotes WHERE ticker = ?", (symbol,)
            ).fetchone()
        if row is None:
            return None
        return MarketQuote(
            ticker=symbol,
            price=_positive_decimal(row["price"]),
            previous_close=_positive_decimal(row["previous_close"])
            if row["previous_close"]
            else None,
            as_of=datetime.fromisoformat(row["as_of"]),
            previous_as_of=datetime.fromisoformat(row["previous_as_of"])
            if row["previous_as_of"]
            else None,
            sector=row["sector"],
            quote_type=row["quote_type"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
        )

    def _coverage(self, symbol: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT start_date, end_date FROM market_split_coverage WHERE ticker = ?", (symbol,)
            ).fetchone()
        return (date.fromisoformat(row[0]), date.fromisoformat(row[1])) if row else None

    def _cached_splits(self, symbol: str, start: date, end: date) -> tuple[SplitEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_date, ratio FROM market_splits WHERE ticker = ? "
                "AND event_date >= ? AND event_date <= ? ORDER BY event_date",
                (symbol, start.isoformat(), end.isoformat()),
            ).fetchall()
        return tuple(
            SplitEvent(symbol, date.fromisoformat(row[0]), Decimal(row[1])) for row in rows
        )

    def _save_quote(self, quote: MarketQuote):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO market_quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    quote.ticker,
                    str(quote.price),
                    str(quote.previous_close) if quote.previous_close is not None else None,
                    quote.as_of.isoformat(),
                    quote.previous_as_of.isoformat() if quote.previous_as_of is not None else None,
                    quote.sector,
                    quote.quote_type,
                    quote.fetched_at.isoformat(),
                ),
            )

    def _save_splits(self, symbol: str, events, start: date, end: date, complete: bool):
        with self._connect() as connection:
            if complete:
                connection.execute(
                    "DELETE FROM market_splits WHERE ticker = ? AND event_date >= ? AND event_date <= ?",
                    (symbol, start.isoformat(), end.isoformat()),
                )
            connection.executemany(
                "INSERT OR REPLACE INTO market_splits VALUES (?, ?, ?)",
                [(symbol, event.date.isoformat(), str(event.ratio)) for event in events],
            )
            if complete:
                old = connection.execute(
                    "SELECT start_date, end_date FROM market_split_coverage WHERE ticker = ?",
                    (symbol,),
                ).fetchone()
                if old and date.fromisoformat(old[1]) >= start:
                    start = min(start, date.fromisoformat(old[0]))
                    end = max(end, date.fromisoformat(old[1]))
                connection.execute(
                    "INSERT OR REPLACE INTO market_split_coverage VALUES (?, ?, ?)",
                    (symbol, start.isoformat(), end.isoformat()),
                )

    def get_snapshot(
        self,
        tickers: Iterable[str],
        *,
        split_start: dict[str, date] | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        """Read a daily cache, refresh needed sessions, and report missing raw split coverage."""
        instant = now if now is not None else datetime.now(timezone.utc)
        target = latest_completed_session(instant)
        instant = instant.astimezone(timezone.utc)
        previous_day = latest_completed_session(session_close(target) - timedelta(microseconds=1))
        raw_starts = {key.strip().upper(): value for key, value in (split_start or {}).items()}
        quotes, splits, split_errors, warnings = {}, {}, {}, []
        for symbol in dict.fromkeys(ticker.strip().upper() for ticker in tickers):
            cached = self._cached_quote(symbol)
            quote = cached or MarketQuote(
                symbol,
                sector=_FALLBACK_SECTORS.get(symbol, "Unknown"),
                quote_type="ETF" if symbol == "VOO" else "EQUITY",
            )
            start = min(raw_starts.get(symbol, target - timedelta(days=30)), previous_day)
            requires_splits = symbol in raw_starts
            # Raw shares need splits already effective today, even when the
            # latest completed price still belongs to the preceding session.
            action_end = (
                instant.astimezone(ZoneInfo("America/New_York")).date()
                if requires_splits
                else target
            )
            try:
                non_trading_reason(start)
            except ValueError as exc:
                message = str(exc)
                quotes[symbol] = replace(quote, error=message)
                splits[symbol] = ()
                if requires_splits:
                    split_errors[symbol] = message
                warnings.append(f"{symbol}: {message}")
                continue
            coverage = self._coverage(symbol)
            has_coverage = bool(
                coverage
                and coverage[0] <= raw_starts.get(symbol, start)
                and coverage[1] >= action_end
            )
            refresh_quote = force or cached is None or cached.as_of.date() != target
            fetch_needed = refresh_quote or (requires_splits and not has_coverage)
            fetch_error = None
            if fetch_needed:
                try:
                    provider = yf.Ticker(symbol)
                    frame = provider.history(
                        start=start,
                        end=action_end + timedelta(days=1),
                        interval="1d",
                        auto_adjust=False,
                        actions=True,
                        timeout=10,
                        raise_errors=True,
                    )
                    if frame.empty or "Close" not in frame:
                        raise ValueError("No completed daily prices returned.")
                    rows = {}
                    for index, row in frame.iterrows():
                        day = index.date()
                        if start <= day <= action_end and non_trading_reason(day) is None:
                            rows[day] = row
                    if target not in rows:
                        raise ValueError(f"No completed close for {target.isoformat()}.")
                    price = _positive_decimal(rows[target]["Close"])
                    previous = None
                    if previous_day in rows:
                        try:
                            previous = _positive_decimal(rows[previous_day]["Close"])
                        except ValueError:
                            pass
                    if previous is None:
                        warnings.append(
                            f"{symbol}: preceding session close unavailable; daily P&L is incomplete."
                        )
                    sector, quote_type, currency, metadata_warning = _metadata(provider, symbol)
                    if metadata_warning:
                        warnings.append(metadata_warning)
                    if currency and str(currency).upper() != "USD":
                        raise ValueError(
                            f"Quote currency {currency} cannot be used in this USD portfolio."
                        )
                    quote = MarketQuote(
                        symbol,
                        price,
                        previous,
                        session_close(target),
                        session_close(previous_day) if previous is not None else None,
                        sector,
                        quote_type,
                        instant,
                    )
                    self._save_quote(quote)
                    try:
                        if "Stock Splits" not in frame:
                            raise ValueError("Split actions were not returned.")
                        events = []
                        for day, row in sorted(rows.items()):
                            ratio = Decimal(str(row["Stock Splits"]))
                            if not ratio.is_finite() or ratio < 0:
                                raise ValueError("Invalid split ratio returned.")
                            if ratio:
                                events.append(SplitEvent(symbol, day, ratio))
                        required_sessions = set()
                        checked_day = start
                        while checked_day <= action_end:
                            if non_trading_reason(checked_day) is None:
                                required_sessions.add(checked_day)
                            checked_day += timedelta(days=1)
                        complete = required_sessions.issubset(rows)
                        self._save_splits(symbol, events, start, action_end, complete)
                        if requires_splits and not complete:
                            fetch_error = "Provider history has gaps or does not cover the earliest raw trade date."
                    except (ValueError, InvalidOperation) as exc:
                        fetch_error = str(exc)
                except Exception as exc:
                    fetch_error = str(exc) or type(exc).__name__
                    if refresh_quote:
                        quote = replace(quote, stale=True, error=fetch_error)
                    warnings.append(
                        f"{symbol}: market refresh failed ({fetch_error}); "
                        + (
                            "using cached price."
                            if quote.price is not None
                            else "price unavailable."
                        )
                    )
            quotes[symbol] = quote
            splits[symbol] = self._cached_splits(symbol, raw_starts.get(symbol, start), action_end)
            coverage = self._coverage(symbol)
            if requires_splits and not (
                coverage and coverage[0] <= raw_starts[symbol] and coverage[1] >= action_end
            ):
                error = fetch_error or "Full split history coverage is unavailable for raw trades."
                split_errors[symbol] = error
                warnings.append(f"{symbol}: split adjustment unavailable ({error}).")
        return MarketSnapshot(quotes, splits, split_errors, tuple(dict.fromkeys(warnings)))
