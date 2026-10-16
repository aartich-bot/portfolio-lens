"""Deterministic contribution illustrations using historical rate inputs."""

from dataclasses import dataclass
from decimal import Decimal
from math import exp, expm1, isfinite, log1p
from sys import float_info


@dataclass(frozen=True)
class ProjectionPoint:
    month: int
    balance: float


@dataclass(frozen=True)
class ProjectionScenario:
    name: str
    annual_rate: float
    points: tuple[ProjectionPoint, ...]


@dataclass(frozen=True)
class ProjectionResult:
    scenarios: tuple[ProjectionScenario, ...]
    contributed: float | None
    reason: str | None


def project_outcomes(
    current_value: Decimal | float | None,
    monthly: Decimal | float,
    years: int,
    annual_return: float | None,
    annual_volatility: float | None,
) -> ProjectionResult:
    """Illustrate lower/base/higher log growth with end-of-month contributions.

    The base uses the supplied geometric historical annual return. Observed
    annualized daily volatility offsets log growth once in either direction.
    Scenarios are deterministic illustrations, not forecasts, probabilities or
    confidence bounds. Callers must only supply estimates from enough complete
    history (at least 30 daily return intervals). Contributions exclude the
    starting holdings value and scenarios exclude fees, taxes and inflation.
    """
    try:
        initial, contribution = float(current_value), float(monthly)
        rate, volatility = float(annual_return), float(annual_volatility)
    except (TypeError, ValueError, OverflowError):
        return ProjectionResult(
            (), None, "Complete historical return and volatility inputs are required."
        )
    if not all(isfinite(value) for value in (initial, contribution, rate, volatility)):
        return ProjectionResult((), None, "Projection inputs must be finite numbers.")
    if (
        (initial == 0 and current_value != 0)
        or (contribution == 0 and monthly != 0)
        or 0 < initial < float_info.min
        or 0 < contribution < float_info.min
    ):
        return ProjectionResult(
            (), None, "Projection inputs are outside the reliable numeric range."
        )
    if initial < 0 or not 0 <= contribution <= 2000:
        return ProjectionResult(
            (),
            None,
            "Starting value must be nonnegative and monthly contributions between 0 and 2000.",
        )
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= 30:
        return ProjectionResult((), None, "Choose a whole-number horizon from 1 to 30 years.")
    if rate <= -1 or volatility < 0:
        return ProjectionResult(
            (), None, "Annual return must exceed -100% and volatility must be nonnegative."
        )
    contributed = contribution * years * 12
    scenarios = []
    try:
        baseline = log1p(rate)
        for name, offset in (("lower", -volatility), ("base", 0.0), ("higher", volatility)):
            growth = baseline + offset
            annual_rate = expm1(growth)
            monthly_factor = exp(growth / 12)
            if not isfinite(annual_rate) or annual_rate <= -1 or not isfinite(monthly_factor):
                raise OverflowError
            balance = initial
            points = [ProjectionPoint(0, balance)]
            for month in range(1, years * 12 + 1):
                previous = balance
                balance = balance * monthly_factor + contribution
                if (
                    not isfinite(balance)
                    or 0 < balance < float_info.min
                    or (balance == 0 and previous > 0)
                ):
                    raise OverflowError
                points.append(ProjectionPoint(month, balance))
            scenarios.append(ProjectionScenario(name, annual_rate, tuple(points)))
    except (ValueError, OverflowError):
        return ProjectionResult(
            (), contributed, "Scenario arithmetic is outside the reliable numeric range."
        )
    return ProjectionResult(tuple(scenarios), contributed, None)
