"""Coordinate historical closes, split-safe replay, and comparable cash returns."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from portfolio_lens.calculations.accounting import replay_ledger
from portfolio_lens.calculations.benchmark import BenchmarkResult, simulate_benchmark
from portfolio_lens.calculations.corporate_actions import normalize_records
from portfolio_lens.calculations.history import HistoryPoint, PortfolioHistory, build_history
from portfolio_lens.calculations.performance import PerformanceTotals, calculate_returns
from portfolio_lens.historical_market_data import HistoricalMarketClient
from portfolio_lens.models import AS_TRADED, HistoricalSnapshot
from portfolio_lens.trading_calendar import (
    latest_completed_session,
    next_trading_day,
    sessions_between,
)


@dataclass(frozen=True)
class TickerGain:
    ticker: str
    realized: Decimal
    unrealized: Decimal | None


@dataclass(frozen=True)
class DateMapping:
    ticker: str
    original: date
    effective: date


@dataclass(frozen=True)
class PerformanceAnalysis:
    totals: PerformanceTotals
    benchmark_returns: PerformanceTotals
    portfolio: PortfolioHistory
    benchmark: BenchmarkResult
    gains: tuple[TickerGain, ...]
    mappings: tuple[DateMapping, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    as_of: date
    market: HistoricalSnapshot
    spread: float | None


def _missing_history(end: date, message: str) -> PortfolioHistory:
    return PortfolioHistory((HistoryPoint(end, None),), (), (message,), False)


def _missing_benchmark(end: date, message: str) -> BenchmarkResult:
    return BenchmarkResult((HistoryPoint(end, None),), None, (message,), False, False)


def _unavailable(
    records: list[dict], end: date, errors: list[str], mappings: tuple[DateMapping, ...] = ()
) -> PerformanceAnalysis:
    reason = "; ".join(errors)
    return PerformanceAnalysis(
        calculate_returns(records, None, end),
        calculate_returns(records, None, end),
        _missing_history(end, reason),
        _missing_benchmark(end, reason),
        (),
        mappings,
        (),
        tuple(dict.fromkeys(errors)),
        end,
        HistoricalSnapshot({}, {}, {}),
        None,
    )


def _ticker_gains(ledger, prices: dict[str, dict[date, Decimal]], end: date):
    grouped: dict[str, dict[str, Decimal]] = {}
    with localcontext() as context:
        context.prec = 50
        for position in ledger.positions:
            total = grouped.setdefault(
                position.ticker,
                {"realized": Decimal(0), "shares": Decimal(0), "basis": Decimal(0)},
            )
            total["realized"] += position.realized_gain
            total["shares"] += position.shares
            total["basis"] += position.cost_basis
        gains = []
        for ticker, total in sorted(grouped.items()):
            if not total["shares"]:
                unrealized = Decimal(0)
            else:
                close = prices.get(ticker, {}).get(end)
                unrealized = total["shares"] * close - total["basis"] if close is not None else None
            gains.append(TickerGain(ticker, total["realized"], unrealized))
    return tuple(gains)


def analyze_performance(
    records: list[dict],
    db_path: str | Path,
    *,
    force: bool = False,
    market_client=None,
    now: datetime | None = None,
) -> PerformanceAnalysis:
    """Value one complete US session with original cash dates and mapped executions."""
    end = latest_completed_session(now)
    warnings: list[str] = []
    errors: list[str] = []
    mapping: dict[date, date] = {}
    disclosed: list[DateMapping] = []
    raw_starts: dict[str, date] = {}
    eligible: list[date] = []
    for record in records:
        try:
            original = date.fromisoformat(record["date"])
            effective = next_trading_day(original)
            ticker = record["ticker"]
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"Trade date cannot be mapped to a US session: {exc}")
            continue
        mapping[original] = effective
        if effective != original:
            disclosed.append(DateMapping(ticker, original, effective))
        if effective <= end:
            eligible.append(effective)
        if record.get("split_convention") == AS_TRADED:
            raw_starts[ticker] = min(original, raw_starts.get(ticker, original))
    if errors:
        return _unavailable(records, end, errors, tuple(disclosed))
    start = min(eligible, default=end)
    try:
        sessions = sessions_between(start, end)
    except ValueError as exc:
        return _unavailable(records, end, [str(exc)], tuple(disclosed))
    client = market_client if market_client is not None else HistoricalMarketClient(db_path)
    tickers = sorted({record["ticker"] for record in records} | {"^GSPC"})
    try:
        market = client.get_history(
            tickers, start, end, split_start=raw_starts, force=force, now=now
        )
    except Exception as exc:
        return _unavailable(
            records, end, [f"Historical market data unavailable: {exc}"], tuple(disclosed)
        )
    warnings.extend(market.warnings)
    for ticker in market.stale_tickers:
        fetched = (market.fetched_at or {}).get(ticker)
        timestamp = f" (last fetched {fetched.isoformat()})" if fetched else ""
        warnings.append(f"{ticker}: using cached historical prices{timestamp}.")
    benchmark = simulate_benchmark(records, market.prices.get("^GSPC", {}), sessions, mapping)
    if benchmark.infeasible:
        errors.extend(benchmark.issues)
    else:
        warnings.extend(benchmark.issues)
    benchmark_returns = calculate_returns(records, benchmark.final_value, end)
    if market.split_errors:
        errors.extend(
            f"Cannot calculate {ticker} holdings until split history is available: {message}"
            for ticker, message in market.split_errors.items()
        )
        portfolio = _missing_history(end, "Raw split history is unavailable.")
        ledger = None
    else:
        try:
            normalized = normalize_records(records, market.splits)
            ledger = replay_ledger(normalized)
            if not ledger.valid:
                errors.extend(issue.message for issue in ledger.issues)
            portfolio = build_history(normalized, market.prices, sessions, mapping)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"Portfolio history unavailable: {exc}")
            portfolio = _missing_history(end, str(exc))
            ledger = None
    if ledger is not None and ledger.valid:
        warnings.extend(portfolio.issues)
    pending = any(effective > end for effective in mapping.values())
    if pending:
        warnings.append(
            "Trades after the latest completed session are pending; lifetime values unavailable."
        )
    terminal = (
        portfolio.points[-1].value
        if portfolio.points and ledger is not None and ledger.valid and not pending
        else None
    )
    totals = calculate_returns(records, terminal, end)
    gains = (
        _ticker_gains(ledger, market.prices, end)
        if ledger is not None and ledger.valid and not pending
        else ()
    )
    if terminal is None and not pending and not errors:
        warnings.append(
            "Latest completed portfolio value is unavailable because a held close is missing."
        )
    if benchmark.final_value is None and not pending and not benchmark.infeasible:
        warnings.append("Latest completed benchmark value is unavailable.")
    spread = None
    if (
        portfolio.complete
        and benchmark.complete
        and not errors
        and totals.xirr.rate is not None
        and benchmark_returns.xirr.rate is not None
    ):
        spread = (totals.xirr.rate - benchmark_returns.xirr.rate) * 100
    return PerformanceAnalysis(
        totals,
        benchmark_returns,
        portfolio,
        benchmark,
        gains,
        tuple(disclosed),
        tuple(dict.fromkeys(warnings)),
        tuple(dict.fromkeys(errors)),
        end,
        market,
        spread,
    )
