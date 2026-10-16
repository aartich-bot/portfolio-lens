"""Pure session-by-session portfolio replay using current-share-basis Close."""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

from portfolio_lens.calculations.accounting import replay_ledger


@dataclass(frozen=True)
class HistoryPoint:
    day: date
    value: Decimal | None


@dataclass(frozen=True)
class TradeMarker:
    original_date: date
    effective_date: date
    ticker: str
    kind: str
    amount: Decimal
    value: Decimal | None
    source_row: int | None


@dataclass(frozen=True)
class PortfolioHistory:
    points: tuple[HistoryPoint, ...]
    markers: tuple[TradeMarker, ...]
    issues: tuple[str, ...]
    complete: bool


def trade_amount(record: dict) -> Decimal:
    """Return the original cash amount, preserving normalized rows' raw fields."""
    with localcontext() as context:
        context.prec = 50
        quantity = Decimal(str(record.get("raw_quantity", record["quantity"])))
        price = Decimal(str(record.get("raw_price", record["price"])))
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            raise ValueError("Trade quantity and price must be finite positive numbers.")
        if record["transaction_type"] not in ("BUY", "SELL"):
            raise ValueError("Trade type must be BUY or SELL.")
        return quantity * price


def valid_price(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def schedule_records(records, sessions, mapped_dates):
    """Validate the supplied next-session mapping; retain original stable order.

    Pending rows are disclosed and omitted only from past chart points. A mapping
    error invalidates replay, since silently skipping that row would invent units.
    """
    scheduled, issues = [], []
    pending = False
    if tuple(sorted(set(sessions))) != tuple(sessions):
        return [], ["Sessions must be unique and in chronological order."], False, False
    if not sessions:
        return [], ["No completed trading sessions are available."], bool(records), False
    try:
        for record in sorted(records, key=lambda row: date.fromisoformat(row["date"])):
            original = date.fromisoformat(record["date"])
            trade_amount(record)
            effective = mapped_dates.get(original, original)
            if original > sessions[-1] or effective > sessions[-1]:
                pending = True
                issues.append(f"Trade on {original} is pending after the valuation session.")
                continue
            offset = bisect_left(sessions, original)
            if offset == len(sessions) or effective != sessions[offset]:
                issues.append(f"Trade on {original} has no valid next-session mapping.")
                return [], issues, pending, False
            scheduled.append((effective, original, record))
    except (InvalidOperation, ValueError, KeyError, TypeError) as exc:
        return [], [*issues, f"Invalid trade: {exc}"], pending, False
    return scheduled, issues, pending, True


def build_history(
    normalized_records: Sequence[dict],
    prices: dict[str, dict[date, Decimal]],
    sessions: tuple[date, ...],
    mapped_dates: dict[date, date],
) -> PortfolioHistory:
    """Value every supplied session; complete requires every point and trade.

    Rows must already use the same split-adjusted share basis as historical Close.
    Each missing held quote creates a gap; zero holdings need no quote. FIFO
    validation is per broker and invalidates an oversold ledger in its entirety.
    """
    scheduled, issues, pending, valid = schedule_records(normalized_records, sessions, mapped_dates)
    if valid:
        ledger = replay_ledger(list(normalized_records))
        if not ledger.valid:
            issues.extend(issue.message for issue in ledger.issues)
            valid = False
    if not valid:
        return PortfolioHistory(
            tuple(HistoryPoint(day, None) for day in sessions), (), tuple(issues), False
        )
    by_day = defaultdict(list)
    for effective, original, record in scheduled:
        by_day[effective].append((original, record))
    holdings = defaultdict(lambda: Decimal(0))
    points, markers = [], []
    with localcontext() as context:
        context.prec = 50
        for day in sessions:
            for _, record in by_day[day]:
                quantity = Decimal(str(record["quantity"]))
                holdings[record["ticker"]] += (
                    quantity if record["transaction_type"] == "BUY" else -quantity
                )
            value = Decimal(0)
            for ticker, quantity in holdings.items():
                if not quantity:
                    continue
                close = prices.get(ticker, {}).get(day)
                if not valid_price(close):
                    value = None
                    issues.append(f"Missing held price for {ticker} on {day}.")
                    break
                value += quantity * close
            points.append(HistoryPoint(day, value))
            markers.extend(
                TradeMarker(
                    original,
                    day,
                    record["ticker"],
                    record["transaction_type"],
                    trade_amount(record),
                    value,
                    record.get("source_row"),
                )
                for original, record in by_day[day]
            )
    return PortfolioHistory(
        tuple(points),
        tuple(markers),
        tuple(issues),
        not pending and all(point.value is not None for point in points),
    )
