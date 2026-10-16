"""Combine saved trades, market data, and pure calculations into one portfolio view."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from portfolio_lens.calculations.accounting import replay_ledger
from portfolio_lens.calculations.corporate_actions import normalize_records
from portfolio_lens.calculations.valuation import PortfolioValuation, value_portfolio
from portfolio_lens.market_data import MarketDataClient
from portfolio_lens.models import AS_TRADED, MarketSnapshot
from portfolio_lens.trading_calendar import non_trading_reason


@dataclass(frozen=True)
class PortfolioAnalysis:
    valuation: PortfolioValuation | None
    market: MarketSnapshot
    date_flags: tuple[str, ...]
    errors: tuple[str, ...] = ()


def analyze_portfolio(
    records: list[dict],
    db_path: str | Path,
    *,
    broker: str | None = None,
    single_threshold: Decimal = Decimal("0.25"),
    top3_threshold: Decimal = Decimal("0.60"),
    force: bool = False,
    market_client=None,
) -> PortfolioAnalysis:
    selected = [
        row for row in records if broker is None or row["broker"].casefold() == broker.casefold()
    ]
    flags = []
    split_start: dict[str, date] = {}
    for row in selected:
        day = date.fromisoformat(row["date"])
        try:
            reason = non_trading_reason(day)
        except ValueError:
            reason = "Trading calendar does not cover this date."
        if reason:
            flags.append(f"{row['ticker']} on {row['date']} ({row['broker']}): {reason}")
        if row["split_convention"] == AS_TRADED:
            split_start[row["ticker"]] = min(day, split_start.get(row["ticker"], day))
    client = market_client or MarketDataClient(db_path)
    market = client.get_snapshot(
        sorted({row["ticker"] for row in selected}), split_start=split_start, force=force
    )
    if market.split_errors:
        return PortfolioAnalysis(
            None,
            market,
            tuple(flags),
            tuple(
                f"Cannot calculate {ticker} holdings until split history is available: {message}"
                for ticker, message in market.split_errors.items()
            ),
        )
    ledger = replay_ledger(normalize_records(selected, market.splits))
    if not ledger.valid:
        return PortfolioAnalysis(
            None, market, tuple(flags), tuple(issue.message for issue in ledger.issues)
        )
    valuation = value_portfolio(
        ledger, market.quotes, single_threshold=single_threshold, top3_threshold=top3_threshold
    )
    return PortfolioAnalysis(valuation, market, tuple(flags))
