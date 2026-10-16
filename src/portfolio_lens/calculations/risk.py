"""Pure concentration checks on consolidated, completely priced holdings."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from portfolio_lens.calculations.accounting import LedgerResult
from portfolio_lens.calculations.valuation import PortfolioValuation


@dataclass(frozen=True)
class RiskThresholds:
    single: Decimal = Decimal("0.25")
    top3: Decimal = Decimal("0.60")
    sector: Decimal = Decimal("0.50")

    def __post_init__(self):
        for field in ("single", "top3", "sector"):
            try:
                number = Decimal(str(getattr(self, field)))
            except (InvalidOperation, ValueError):
                raise ValueError(
                    "Risk thresholds must be fractions between zero and one."
                ) from None
            if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
                raise ValueError("Risk thresholds must be fractions between zero and one.")
            object.__setattr__(self, field, number)


@dataclass(frozen=True)
class RiskAlert:
    kind: str
    severity: str
    title: str
    message: str
    weight: Decimal
    threshold: Decimal


@dataclass(frozen=True)
class RiskAnalysis:
    alerts: tuple[RiskAlert, ...]
    complete: bool
    issues: tuple[str, ...]
    sector_coverage: Decimal | None
    top3_weight: Decimal | None


def analyze_risk(
    valuation: PortfolioValuation,
    ledger: LedgerResult,
    thresholds: RiskThresholds = RiskThresholds(),
) -> RiskAnalysis:
    """Return strict threshold breaches; ETFs have no sector look-through.

    Complete means that holding values are available. Sector coverage and its
    issues disclose whether a sector without an alert is fully assessed.
    Known sector weights still use total portfolio value as the denominator.
    """
    if not ledger.valid or not valuation.complete or valuation.current_value is None:
        issues = tuple(issue.message for issue in ledger.issues) + valuation.issues
        return RiskAnalysis(
            (),
            False,
            issues or ("Risk analysis unavailable: valuation is incomplete.",),
            None,
            None,
        )
    total = valuation.current_value
    if (
        not total.is_finite()
        or total < 0
        or any(
            holding.market_value is None
            or not holding.market_value.is_finite()
            or holding.market_value < 0
            for holding in valuation.holdings
        )
    ):
        return RiskAnalysis((), False, ("Risk analysis requires valid market values.",), None, None)
    if not total or not valuation.holdings:
        return RiskAnalysis(
            (), True, ("No open holdings to assess for concentration.",), None, None
        )
    with localcontext() as context:
        context.prec = 50
        alerts, sectors, excluded = [], {}, {}
        for holding in valuation.holdings:
            weight = holding.market_value / total
            if weight > thresholds.single:
                brokers = {
                    position.broker.casefold()
                    for position in ledger.positions
                    if position.ticker == holding.ticker and position.shares > 0
                }
                count = len(brokers)
                alerts.append(
                    RiskAlert(
                        "holding",
                        "warning",
                        f"{holding.ticker} concentration",
                        f"{holding.ticker} is {weight:.1%} of holdings across {count} "
                        f"{'broker' if count == 1 else 'brokers'}, above the "
                        f"{thresholds.single:.1%} holding limit.",
                        weight,
                        thresholds.single,
                    )
                )
            sector = holding.sector.strip() or "Unknown"
            groups = excluded if sector.casefold() in ("etf", "unknown") else sectors
            groups[sector] = groups.get(sector, Decimal(0)) + holding.market_value
        top3 = (
            sum(sorted((h.market_value for h in valuation.holdings), reverse=True)[:3], Decimal(0))
            / total
        )
        if top3 > thresholds.top3:
            alerts.append(
                RiskAlert(
                    "top3",
                    "warning",
                    "Top three holding concentration",
                    f"The top three holdings account for {top3:.1%}, above the "
                    f"{thresholds.top3:.1%} limit.",
                    top3,
                    thresholds.top3,
                )
            )
        for sector, amount in sorted(sectors.items()):
            weight = amount / total
            if weight > thresholds.sector:
                alerts.append(
                    RiskAlert(
                        "sector",
                        "warning",
                        f"{sector} sector concentration",
                        f"Known {sector} holdings account for {weight:.1%} of the portfolio, "
                        f"above the {thresholds.sector:.1%} sector limit.",
                        weight,
                        thresholds.sector,
                    )
                )
        coverage = sum(sectors.values(), Decimal(0)) / total
        issues = tuple(
            f"{name} holdings ({amount / total:.1%}) are excluded from sector coverage; "
            + (
                "ETF holdings have no sector look-through."
                if name.casefold() == "etf"
                else "sector metadata is unavailable."
            )
            for name, amount in sorted(excluded.items())
        )
        return RiskAnalysis(tuple(alerts), True, issues, coverage, top3)
