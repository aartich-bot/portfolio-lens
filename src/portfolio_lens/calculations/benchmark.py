"""Pure same-cash-flow simulation of a fractional-unit price index."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction

from portfolio_lens.calculations.history import (
    HistoryPoint,
    schedule_records,
    trade_amount,
    valid_price,
)


@dataclass(frozen=True)
class BenchmarkResult:
    points: tuple[HistoryPoint, ...]
    final_value: Decimal | None
    issues: tuple[str, ...]
    infeasible: bool
    complete: bool


def simulate_benchmark(
    original_records: Sequence[dict],
    index_prices: dict[date, Decimal],
    sessions: tuple[date, ...],
    mapped_dates: dict[date, date],
) -> BenchmarkResult:
    """Buy/withdraw original dollar amounts at each effective-session Close.

    Missing execution prices or infeasible withdrawals invalidate all later
    values. A missing quote between executions creates only that day's gap.
    Complete requires all points and trades; pending rows disable final_value.
    """
    scheduled, issues, pending, valid = schedule_records(original_records, sessions, mapped_dates)
    by_day = defaultdict(list)
    for effective, _, record in scheduled:
        by_day[effective].append(record)
    points = []
    # Rational units make exact full withdrawals possible even for 100 / 3.
    # A tolerance would hide small real oversells; decimal division alone can
    # instead invent an oversell when repeating fractions round downward.
    units = Fraction(0)
    infeasible = False
    with localcontext() as context:
        context.prec = 50
        for day in sessions:
            close = index_prices.get(day)
            for record in by_day[day]:
                if not valid:
                    break
                if not valid_price(close):
                    issues.append(f"Missing benchmark execution price on {day}.")
                    valid = False
                    break
                amount = Fraction(trade_amount(record))
                execution_close = Fraction(close)
                if record["transaction_type"] == "BUY":
                    units += amount / execution_close
                else:
                    available = units * execution_close
                    if amount > available:
                        dollars = Decimal(amount.numerator) / Decimal(amount.denominator)
                        held = Decimal(available.numerator) / Decimal(available.denominator)
                        issues.append(
                            f"Infeasible benchmark sale: {record['ticker']} recorded "
                            f"{record['date']} maps to {day}; the ${dollars:,.2f} withdrawal "
                            f"exceeds ${held:,.2f} available. Amounts are displayed rounded to cents."
                        )
                        infeasible = True
                        valid = False
                        break
                    units -= amount / execution_close
            if not valid:
                value = None
            elif not units:
                value = Decimal(0)
            elif not valid_price(close):
                value = None
                issues.append(f"Missing held benchmark price on {day}.")
            else:
                marked = units * Fraction(close)
                value = Decimal(marked.numerator) / Decimal(marked.denominator)
            points.append(HistoryPoint(day, value))
    complete = valid and not pending and all(point.value is not None for point in points)
    terminal = points[-1].value if points and not pending else None
    return BenchmarkResult(tuple(points), terminal, tuple(issues), infeasible, complete)
