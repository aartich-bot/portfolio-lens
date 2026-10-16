"""Link daily returns using an explicitly approximate midpoint cash-flow weight."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from math import expm1, fsum, isfinite, log1p, sqrt
from statistics import stdev
from sys import float_info

from portfolio_lens.calculations.history import HistoryPoint


@dataclass(frozen=True)
class DrawdownPoint:
    day: date
    index: float
    drawdown: float
    daily_return: float | None


@dataclass(frozen=True)
class DrawdownResult:
    points: tuple[DrawdownPoint, ...]
    max_drawdown: float | None
    annual_return: float | None
    annual_volatility: float | None
    reason: str | None
    complete: bool


def _unavailable(reason: str) -> DrawdownResult:
    return DrawdownResult((), None, None, None, reason, False)


def analyze_drawdown(
    points: Sequence[HistoryPoint],
    net_flows: Mapping[date, Decimal],
    complete: bool = True,
) -> DrawdownResult:
    """Use (end - start - flow) / (start + 0.5 * flow) and a starting index of 100.

    The caller supplies original trade dollar amounts on mapped trading dates:
    BUY amounts positive, SELL amounts negative. First-day execution changes are
    excluded. This holdings-only midpoint approximation is not an exact
    time-weighted return and makes no claim of GIPS compliance. Leading zero
    holdings have no investment basis; later zero/zero/no-flow days carry the
    index without a return observation. Annualization uses 252 trading sessions
    and at least 30 observed return intervals, with sample daily volatility.
    """
    if not complete:
        return _unavailable("Flow-adjusted drawdown unavailable: portfolio history is incomplete.")
    if not points:
        return _unavailable("No completed portfolio valuations are available.")
    days = tuple(point.day for point in points)
    if tuple(sorted(set(days))) != days:
        return _unavailable("History sessions must be unique and in chronological order.")
    try:
        values = tuple(
            None if point.value is None else Decimal(str(point.value)) for point in points
        )
        flows = {day: Decimal(str(amount)) for day, amount in net_flows.items()}
    except (InvalidOperation, ValueError, TypeError):
        return _unavailable("Portfolio valuations and cash flows must be finite numbers.")
    if any(value is None or not value.is_finite() or value < 0 for value in values):
        return _unavailable("A missing or invalid portfolio valuation prevents linked returns.")
    if any(not flow.is_finite() for flow in flows.values()):
        return _unavailable("An invalid cash flow prevents linked returns.")
    start = next((i for i, value in enumerate(values) if value > 0), None)
    if start is None:
        return _unavailable("No positive portfolio valuation is available to start returns.")
    index = peak = 100.0
    result = [DrawdownPoint(days[start], index, 0.0, None)]
    returns = []
    with localcontext() as context:
        context.prec = 50
        for i in range(start + 1, len(points)):
            previous, value, flow = values[i - 1], values[i], flows.get(days[i], Decimal(0))
            daily_return = None
            if previous == value == flow == 0:
                result.append(DrawdownPoint(days[i], index, 1 - index / peak, None))
                continue
            try:
                denominator = previous + flow / 2
                if denominator <= 0:
                    return _unavailable(f"Nonpositive cash-flow-adjusted capital on {days[i]}.")
                change = (value - previous - flow) / denominator
                # This equivalent factor avoids losing a tiny remaining value
                # when subtraction rounds a large loss to exactly minus one.
                factor = (value - flow / 2) / denominator
            except ArithmeticError:
                return _unavailable(
                    "Linked return arithmetic is outside the reliable numeric range."
                )
            if factor < 0:
                return _unavailable(f"Flow-adjusted loss exceeds 100% on {days[i]}.")
            daily_return = float(change)
            preceding_index = index
            index *= float(factor)
            if (
                not isfinite(daily_return)
                or not isfinite(index)
                or (factor > 0 and daily_return <= -1)
                or 0 < index < float_info.min
                or (factor > 0 and preceding_index > 0 and index == 0)
            ):
                return _unavailable(
                    "Linked return arithmetic is outside the reliable numeric range."
                )
            returns.append(daily_return)
            peak = max(peak, index)
            result.append(DrawdownPoint(days[i], index, max(0.0, 1 - index / peak), daily_return))
    maximum = max(point.drawdown for point in result)
    if len(returns) < 30:
        return DrawdownResult(
            tuple(result),
            maximum,
            None,
            None,
            "At least 30 daily return intervals are required for annual estimates.",
            True,
        )
    try:
        annual = (
            -1.0
            if -1.0 in returns
            else expm1(fsum(log1p(value) for value in returns) * 252 / len(returns))
        )
        volatility = stdev(returns) * sqrt(252)
        if not isfinite(annual) or not isfinite(volatility):
            raise OverflowError
    except (OverflowError, ValueError):
        return DrawdownResult(
            tuple(result),
            maximum,
            None,
            None,
            "Annual estimates are outside the reliable numeric range.",
            True,
        )
    return DrawdownResult(tuple(result), maximum, annual, volatility, None, True)
