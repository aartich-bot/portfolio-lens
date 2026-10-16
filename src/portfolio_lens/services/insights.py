"""Compose risk, flow-adjusted history, projections, and aggregate-only explanations."""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from portfolio_lens.calculations.accounting import replay_ledger
from portfolio_lens.calculations.corporate_actions import normalize_records
from portfolio_lens.calculations.drawdown import DrawdownResult, analyze_drawdown
from portfolio_lens.calculations.projections import ProjectionResult, project_outcomes
from portfolio_lens.calculations.risk import RiskAnalysis, RiskThresholds, analyze_risk
from portfolio_lens.calculations.valuation import PortfolioValuation, value_portfolio
from portfolio_lens.exports import ReportData
from portfolio_lens.market_data import MarketDataClient
from portfolio_lens.models import AS_TRADED, MarketQuote, MarketSnapshot
from portfolio_lens.services.performance import PerformanceAnalysis, analyze_performance
from portfolio_lens.services.summary import AggregateFact, SummaryResult, rule_based_summary
from portfolio_lens.trading_calendar import next_trading_day, session_close


@dataclass(frozen=True)
class InsightsAnalysis:
    valuation: PortfolioValuation | None
    risk: RiskAnalysis
    drawdown: DrawdownResult
    projection: ProjectionResult
    performance: PerformanceAnalysis
    thresholds: RiskThresholds
    monthly: Decimal
    years: int
    facts: tuple[AggregateFact, ...]
    warnings: tuple[str, ...]
    fingerprint: str


def money(value) -> str:
    return "Unavailable" if value is None else f"${value:,.2f}"


def percent(value) -> str:
    return "Unavailable" if value is None else f"{value * 100:.2f}%"


def _facts(valuation, risk, drawdown, performance, projection):
    facts = []
    if valuation is None or not valuation.complete or not drawdown.complete:
        facts.append(
            AggregateFact(
                "data_quality",
                "Data quality",
                "Incomplete",
                "Some calculations are unavailable because the required data is incomplete.",
                ("Review the data notices before comparing results or exploring scenarios.",),
            )
        )
    for index, alert in enumerate(risk.alerts[:5]):
        facts.append(
            AggregateFact(
                f"concentration_{index}",
                alert.title,
                percent(alert.weight),
                alert.message,
                (
                    "This crosses a configured product threshold; it is a prompt to review concentration, not a universal risk judgment.",
                ),
            )
        )
    if not risk.alerts and risk.complete:
        facts.append(
            AggregateFact(
                "concentration_clear",
                "Configured concentration checks",
                "No breaches",
                "No configured concentration limit is exceeded in the holdings and known sectors checked."
                + (
                    " Some sector exposure remains unassessed."
                    if risk.sector_coverage is not None and risk.sector_coverage < 1
                    else ""
                ),
                ("Passing these checks does not measure every kind of investment risk.",),
            )
        )
    if risk.sector_coverage is not None and risk.sector_coverage < 1:
        facts.append(
            AggregateFact(
                "sector_coverage",
                "Known stock-sector coverage",
                percent(risk.sector_coverage),
                f"Known stock sectors cover {percent(risk.sector_coverage)} of portfolio value.",
                (
                    "ETF holdings and unknown sectors are not looked through, so sector exposure may be understated.",
                ),
            )
        )
    current = valuation.current_value if valuation else None
    facts.append(
        AggregateFact(
            "current_value",
            "Current value",
            money(current),
            f"Your remaining holdings are worth {money(current)}."
            if current is not None
            else "Your remaining holdings value is unavailable.",
            ("This is the value of shares held, without a cash balance or dividends.",),
        )
    )
    if performance.totals.xirr.rate is not None:
        facts.append(
            AggregateFact(
                "xirr",
                "Money-weighted yearly return",
                percent(performance.totals.xirr.rate),
                f"Your money-weighted yearly return is {percent(performance.totals.xirr.rate)}.",
                ("It reflects both how much you invested and when; it is not a prediction.",),
            )
        )
    if drawdown.max_drawdown is not None:
        facts.append(
            AggregateFact(
                "drawdown",
                "Maximum drawdown",
                percent(drawdown.max_drawdown),
                f"Your largest flow-adjusted fall from a previous peak was {percent(drawdown.max_drawdown)}.",
                (
                    "Purchases and sales are adjusted out using a daily timing approximation; future falls can be larger.",
                ),
            )
        )
    if projection.scenarios:
        scenario = next(item for item in projection.scenarios if item.name == "base")
        facts.append(
            AggregateFact(
                "illustration",
                "Base illustrative ending value",
                money(scenario.points[-1].balance),
                f"The base contribution illustration ends at {money(scenario.points[-1].balance)}.",
                (
                    "This repeats a historical rate with the selected contributions; it is illustrative, not a forecast or guarantee.",
                ),
            )
        )
    return tuple(facts)


def analyze_insights(
    records: list[dict],
    db_path: str | Path,
    *,
    thresholds: RiskThresholds = RiskThresholds(),
    monthly: Decimal = Decimal(500),
    years: int = 10,
    force: bool = False,
    history_client=None,
    quote_client=None,
    now: datetime | None = None,
) -> InsightsAnalysis:
    """Use the same completed historical price basis for risk and starting balances."""
    performance = analyze_performance(
        records, db_path, force=force, market_client=history_client, now=now
    )
    warnings = list(performance.warnings) + list(performance.errors)
    client = quote_client if quote_client is not None else MarketDataClient(db_path)
    tickers = sorted({row["ticker"] for row in records})
    try:
        metadata = client.get_snapshot(tickers, force=force, now=now)
        warnings.extend(metadata.warnings)
    except Exception:
        metadata = MarketSnapshot({}, {}, {})
        warnings.append("Sector metadata is unavailable; unknown sectors are disclosed separately.")
    valuation, ledger = None, None
    risk = RiskAnalysis(
        (), False, ("Complete holdings are unavailable for concentration checks.",), None, None
    )
    try:
        pending = any(
            next_trading_day(date.fromisoformat(row["date"])) > performance.as_of for row in records
        )
        raw_tickers = {row["ticker"] for row in records if row.get("split_convention") == AS_TRADED}
        missing_split_coverage = raw_tickers - performance.market.splits.keys()
        if missing_split_coverage:
            warnings.append(
                "Raw trade split coverage is unavailable; current share quantities are withheld."
            )
        if not pending and not performance.market.split_errors and not missing_split_coverage:
            ledger = replay_ledger(normalize_records(records, performance.market.splits))
            if ledger.valid:
                end = performance.as_of
                previous = (
                    performance.portfolio.points[-2].day
                    if len(performance.portfolio.points) > 1
                    else None
                )
                quotes = {}
                for ticker in tickers:
                    sector = metadata.quotes.get(ticker, MarketQuote(ticker))
                    prices = performance.market.prices.get(ticker, {})
                    quotes[ticker] = MarketQuote(
                        ticker,
                        prices.get(end),
                        prices.get(previous),
                        session_close(end),
                        session_close(previous) if previous else None,
                        sector.sector,
                        sector.quote_type,
                    )
                valuation = value_portfolio(
                    ledger,
                    quotes,
                    single_threshold=thresholds.single,
                    top3_threshold=thresholds.top3,
                )
                risk = analyze_risk(valuation, ledger, thresholds)
    except (ValueError, KeyError, TypeError) as exc:
        warnings.append(f"Concentration checks are unavailable: {exc}")
    flows = defaultdict(lambda: Decimal(0))
    with localcontext() as context:
        context.prec = 50
        for marker in performance.portfolio.markers:
            flows[marker.effective_date] += (
                marker.amount if marker.kind == "BUY" else -marker.amount
            )
    drawdown = analyze_drawdown(
        performance.portfolio.points, flows, complete=performance.portfolio.complete
    )
    projection = project_outcomes(
        valuation.current_value if valuation else None,
        monthly,
        years,
        drawdown.annual_return,
        drawdown.annual_volatility,
    )
    warnings.extend(risk.issues)
    facts = _facts(valuation, risk, drawdown, performance, projection)
    warnings = tuple(dict.fromkeys(warnings))
    # The digest stays local; summaries cannot be reused after holdings, prices, or sliders change.
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "valuation": asdict(valuation) if valuation else None,
                "drawdown": asdict(drawdown),
                "projection": asdict(projection),
                "thresholds": asdict(thresholds),
                "monthly": str(monthly),
                "years": years,
                "facts": [asdict(fact) for fact in facts],
                "warnings": warnings,
                "as_of": str(performance.as_of),
            },
            default=str,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return InsightsAnalysis(
        valuation,
        risk,
        drawdown,
        projection,
        performance,
        thresholds,
        monthly,
        years,
        facts,
        warnings,
        fingerprint,
    )


def estimation_period(analysis: InsightsAnalysis) -> str:
    """Describe the exact observed period used by the historical rate estimates."""
    points = analysis.drawdown.points
    if not points:
        return "Historical estimation period unavailable."
    count = sum(point.daily_return is not None for point in points)
    return f"History: {points[0].day} to {points[-1].day}; {count} daily returns."


def make_report(analysis: InsightsAnalysis, summary: SummaryResult | None = None) -> ReportData:
    """Build report text from the same verified results and current simulator settings."""
    if summary is None or summary.fingerprint != analysis.fingerprint:
        summary = rule_based_summary(analysis.facts, fingerprint=analysis.fingerprint)
    totals = analysis.performance.totals
    projection = [
        f"Illustrative only: {money(analysis.monthly)} at each month-end for {analysis.years} years.",
        estimation_period(analysis),
    ]
    projection.extend(
        f"{item.name.title()}: {money(item.points[-1].balance)} at {percent(item.annual_rate)} a year."
        for item in analysis.projection.scenarios
    )
    if analysis.projection.reason:
        projection.append(analysis.projection.reason)
    notices = list(analysis.warnings)
    if not analysis.risk.complete:
        notices.insert(
            0, "Some concentration checks are incomplete; missing data does not mean low risk."
        )
    if analysis.drawdown.reason:
        notices.insert(0, analysis.drawdown.reason)
    notices.append(
        "Drawdown uses daily midpoint cash-flow adjustment, starting at the first completed valuation."
    )
    return ReportData(
        as_of=f"{analysis.performance.as_of.isoformat()} completed market close (USD)",
        metrics=(
            (
                "Current value",
                money(analysis.valuation.current_value if analysis.valuation else None),
            ),
            ("Total invested", money(totals.invested)),
            ("Total sold", money(totals.sold)),
            ("XIRR (yearly)", percent(totals.xirr.rate)),
            ("Total return (not yearly)", percent(totals.total_return)),
        ),
        alerts=tuple(alert.message for alert in analysis.risk.alerts),
        max_drawdown=percent(analysis.drawdown.max_drawdown),
        projection=tuple(projection),
        summary=summary.text,
        notices=tuple(notices),
    )
