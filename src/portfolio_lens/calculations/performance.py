"""Exact cash totals and ACT/365 money-weighted return, without market I/O."""

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

from portfolio_lens.calculations.history import trade_amount


@dataclass(frozen=True)
class XirrResult:
    rate: float | None
    reason: str | None


@dataclass(frozen=True)
class PerformanceTotals:
    invested: Decimal
    sold: Decimal
    current_value: Decimal | None
    total_gain: Decimal | None
    total_return: Decimal | None
    xirr: XirrResult


def _scaled_npv(terms, x):
    logs = [math.log(abs(coefficient)) - exponent * x for exponent, coefficient in terms]
    maximum = max(logs)
    return math.fsum(
        math.copysign(math.exp(log_value - maximum), coefficient)
        for log_value, (_, coefficient) in zip(logs, terms)
    )


def _bisect_root(terms, low, high, low_sign, high_sign):
    """Bracket unbounded intervals by their known asymptotic signs."""
    if low == -math.inf:
        low = min(0.0, high - 1) if math.isfinite(high) else -1.0
        step = 1.0
        for _ in range(60):
            if _scaled_npv(terms, low) * low_sign > 0:
                break
            low -= step
            step *= 2
        else:
            raise ArithmeticError("Could not bracket the XIRR root.")
    if high == math.inf:
        high = max(0.0, low + 1)
        step = 1.0
        for _ in range(60):
            if _scaled_npv(terms, high) * high_sign > 0:
                break
            high += step
            step *= 2
        else:
            raise ArithmeticError("Could not bracket the XIRR root.")
    for _ in range(200):
        middle = (low + high) / 2
        value = _scaled_npv(terms, middle)
        if value == 0 or high - low <= 1e-13 * max(1, abs(middle)):
            return middle
        if value * low_sign > 0:
            low = middle
        else:
            high = middle
    raise ArithmeticError("XIRR root did not converge.")


def _isolate_roots(terms):
    """Isolate all roots on the entire log-rate line using Rolle's theorem.

    Factoring the first exponential leaves a constant. Differentiating removes
    that term, so recursion terminates. Derivative roots partition the function
    into monotone intervals, each containing at most one simple root. This is
    not a finite grid/multiple-guess search: even distant roots are considered.
    Near-zero extrema are rejected conservatively as numerically ambiguous.
    """
    first = terms[0][0]
    scale = max(abs(coefficient) for _, coefficient in terms)
    terms = [(exponent - first, coefficient / scale) for exponent, coefficient in terms]
    if any(coefficient == 0 for _, coefficient in terms):
        raise ArithmeticError("Cash-flow magnitudes exceed reliable numerical precision.")
    changes = sum((a[1] < 0) != (b[1] < 0) for a, b in zip(terms, terms[1:]))
    if changes == 0:
        return []
    left_sign, right_sign = math.copysign(1, terms[-1][1]), math.copysign(1, terms[0][1])
    if changes == 1:
        return [_bisect_root(terms, -math.inf, math.inf, left_sign, right_sign)]
    critical = _isolate_roots(
        [(exponent, -exponent * coefficient) for exponent, coefficient in terms[1:]]
    )
    values = [_scaled_npv(terms, point) for point in critical]
    if any(abs(value) < 1e-10 for value in values):
        raise ArithmeticError("Ambiguous XIRR: repeated or numerically indistinguishable roots.")
    bounds = [-math.inf, *critical, math.inf]
    signs = [left_sign, *values, right_sign]
    return [
        _bisect_root(terms, low, high, a, b)
        for low, high, a, b in zip(bounds, bounds[1:], signs, signs[1:])
        if a * b < 0
    ]


def solve_xirr(flows: Sequence[tuple[date, Decimal]]) -> XirrResult:
    """Find a unique finite r > -1 satisfying dated NPV=0 on ACT/365.

    Dates are original cash dates. Multiple roots, degenerate dates, no root,
    or numerical ill-conditioning return a reason rather than an arbitrary rate.
    The solver uses float root isolation after exact same-day aggregation.
    """
    by_date = defaultdict(lambda: Decimal(0))
    try:
        with localcontext() as context:
            context.prec = 50
            for day, amount in flows:
                if not amount.is_finite():
                    return XirrResult(None, "Cash flows must be finite.")
                by_date[day] += amount
            ordered = sorted((day, amount) for day, amount in by_date.items() if amount)
            if len(ordered) < 2:
                return XirrResult(None, "XIRR needs nonzero cash flows on distinct dates.")
            if not any(amount < 0 for _, amount in ordered) or not any(
                amount > 0 for _, amount in ordered
            ):
                return XirrResult(None, "XIRR needs both negative and positive cash flows.")
            scale = max(abs(amount) for _, amount in ordered)
            start = ordered[0][0]
            terms = [((day - start).days / 365, float(amount / scale)) for day, amount in ordered]
        roots = _isolate_roots(terms)
        if not roots:
            return XirrResult(None, "No XIRR root exists for these cash flows.")
        if len(roots) != 1:
            return XirrResult(None, "Ambiguous XIRR: multiple rates satisfy these cash flows.")
        rate = math.expm1(roots[0])
        if not math.isfinite(rate) or rate <= -1:
            return XirrResult(None, "XIRR is outside representable numerical precision.")
        return XirrResult(rate, None)
    except RecursionError:
        return XirrResult(
            None,
            "XIRR unavailable: too many alternating cash-flow dates "
            "to reliably rule out multiple rates.",
        )
    except (ArithmeticError, ValueError) as exc:
        return XirrResult(None, f"XIRR unavailable: {exc}")


def calculate_returns(
    original_records: Sequence[dict], terminal_value: Decimal | None, terminal_date: date
) -> PerformanceTotals:
    """Lifetime totals include every trade; future trades disable valuation.

    Total return is (terminal holdings + sales - buys) / buys, not annualized.
    Dividends and fees are excluded. A genuine zero terminal value is supported.
    """
    invested = sold = Decimal(0)
    flows = []
    problem = None
    with localcontext() as context:
        context.prec = 50
        for record in original_records:
            try:
                amount = trade_amount(record)
                day = date.fromisoformat(record["date"])
                if record["transaction_type"] == "BUY":
                    invested += amount
                    flows.append((day, -amount))
                else:
                    sold += amount
                    flows.append((day, amount))
                if day > terminal_date:
                    problem = (
                        "Trades occur after the common valuation date; lifetime return unavailable."
                    )
            except (InvalidOperation, ValueError, KeyError, TypeError) as exc:
                problem = f"Invalid trade cash flow: {exc}"
        if terminal_value is None:
            problem = problem or "Terminal holdings value is unavailable."
        elif not terminal_value.is_finite() or terminal_value < 0:
            problem = problem or "Terminal holdings value must be finite and nonnegative."
        if problem:
            return PerformanceTotals(invested, sold, None, None, None, XirrResult(None, problem))
        gain = terminal_value + sold - invested
        total_return = gain / invested if invested else None
        flows.append((terminal_date, terminal_value))
        return PerformanceTotals(
            invested, sold, terminal_value, gain, total_return, solve_xirr(flows)
        )
