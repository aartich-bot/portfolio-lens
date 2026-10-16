"""Value remaining FIFO positions against quotes supplied by the caller."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

from portfolio_lens.calculations.accounting import LedgerResult
from portfolio_lens.models import MarketQuote


@dataclass(frozen=True)
class Holding:
    ticker: str
    shares: Decimal
    average_cost: Decimal
    cost_basis: Decimal
    price: Decimal | None
    market_value: Decimal | None
    unrealized_gain: Decimal | None
    unrealized_pct: Decimal | None
    weight: Decimal | None
    sector: str


@dataclass(frozen=True)
class Allocation:
    name: str
    value: Decimal
    weight: Decimal


@dataclass(frozen=True)
class PortfolioValuation:
    holdings: tuple[Holding, ...]
    current_value: Decimal | None
    cost_basis: Decimal
    unrealized_gain: Decimal | None
    unrealized_pct: Decimal | None
    realized_gain: Decimal
    today_change: Decimal | None
    today_change_pct: Decimal | None
    complete: bool
    issues: tuple[str, ...]
    allocations: dict[str, tuple[Allocation, ...]]
    concentration: str | None
    top3_weight: Decimal | None
    as_of: datetime | None = None
    previous_as_of: datetime | None = None


def _positive(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def value_portfolio(
    ledger: LedgerResult,
    quotes: dict[str, MarketQuote],
    broker: str | None = None,
    single_threshold: Decimal = Decimal("0.25"),
    top3_threshold: Decimal = Decimal("0.60"),
) -> PortfolioValuation:
    """Return prices, gains, allocations and concentration with honest unknowns.

    Gain percentages and weights are fractions (0.25 means 25%). Today's change
    uses current held shares times the latest completed session's price change;
    its percentage divides by those shares valued at the preceding close.
    Missing prices or mixed current dates make all portfolio price totals and
    weights unavailable. Missing/mixed preceding dates disable only daily change.
    Realized gains include closed positions and obey the same broker filter.
    """
    if not ledger.valid:
        raise ValueError("Cannot value an invalid ledger.")
    for threshold in (single_threshold, top3_threshold):
        if not threshold.is_finite() or not Decimal("0") <= threshold <= Decimal("1"):
            raise ValueError("Concentration thresholds must be fractions between zero and one.")
    with localcontext() as context:
        context.prec = 50
        return _value(ledger, quotes, broker, single_threshold, top3_threshold)


def _value(ledger, quotes, broker, single_threshold, top3_threshold):
    positions = [
        position
        for position in ledger.positions
        if broker is None or position.broker.casefold() == broker.casefold()
    ]
    realized = sum((position.realized_gain for position in positions), Decimal("0"))
    open_positions = [position for position in positions if position.shares > 0]
    totals: dict[str, dict] = {}
    for position in open_positions:
        total = totals.setdefault(position.ticker, {"shares": Decimal("0"), "basis": Decimal("0")})
        total["shares"] += position.shares
        total["basis"] += position.cost_basis
    basis = sum((total["basis"] for total in totals.values()), Decimal("0"))
    prices, previous = {}, {}
    sectors = {}
    current_dates, previous_dates = set(), set()
    issues = []
    complete, daily_complete = True, True
    as_of, previous_as_of = None, None
    for ticker in totals:
        quote = quotes.get(ticker)
        prices[ticker] = _positive(quote.price) if quote else None
        previous[ticker] = _positive(quote.previous_close) if quote else None
        sectors[ticker] = (
            "ETF"
            if quote and quote.quote_type.upper() == "ETF"
            else (quote.sector or "Unknown")
            if quote
            else "Unknown"
        )
        if prices[ticker] is None:
            complete = False
            issues.append(f"{ticker}: current price unavailable or invalid.")
        if quote and quote.as_of:
            current_dates.add(quote.as_of.date())
            as_of = quote.as_of
        else:
            complete = False
            issues.append(f"{ticker}: current price session unavailable.")
        if previous[ticker] is None or not quote or not quote.previous_as_of:
            daily_complete = False
            issues.append(f"{ticker}: preceding close or session unavailable.")
        elif quote.as_of and quote.previous_as_of.date() >= quote.as_of.date():
            daily_complete = False
            issues.append(f"{ticker}: preceding close must precede the current session.")
        else:
            previous_dates.add(quote.previous_as_of.date())
            previous_as_of = quote.previous_as_of
    if len(current_dates) > 1:
        complete = False
        issues.append("Current prices belong to different completed sessions.")
    if len(previous_dates) > 1:
        daily_complete = False
        issues.append("Preceding closes belong to different completed sessions.")
    current_value = (
        sum((total["shares"] * prices[ticker] for ticker, total in totals.items()), Decimal("0"))
        if complete
        else None
    )
    gain = current_value - basis if current_value is not None else None
    gain_pct = gain / basis if gain is not None and basis else None
    daily = daily_pct = None
    if complete and daily_complete:
        preceding_value = sum(
            (total["shares"] * previous[ticker] for ticker, total in totals.items()), Decimal("0")
        )
        daily = current_value - preceding_value
        daily_pct = daily / preceding_value if preceding_value else None
    holdings = []
    for ticker, total in sorted(totals.items()):
        market_value = total["shares"] * prices[ticker] if prices[ticker] is not None else None
        unrealized = market_value - total["basis"] if market_value is not None else None
        holdings.append(
            Holding(
                ticker,
                total["shares"],
                total["basis"] / total["shares"],
                total["basis"],
                prices[ticker],
                market_value,
                unrealized,
                unrealized / total["basis"] if unrealized is not None and total["basis"] else None,
                market_value / current_value if complete and current_value else None,
                sectors[ticker],
            )
        )
    allocations = {}
    concentration = top3_weight = None
    if complete and holdings:
        stock_values = {holding.ticker: holding.market_value for holding in holdings}
        sector_values = {}
        broker_values = {}
        for holding in holdings:
            sector_values[holding.sector] = sector_values.get(holding.sector, Decimal("0")) + (
                holding.market_value
            )
        # The ledger already merged differently capitalized names for each ticker;
        # merge them across tickers as well while retaining the first display name.
        broker_names = {}
        for position in open_positions:
            name = broker_names.setdefault(position.broker.casefold(), position.broker)
            broker_values[name] = broker_values.get(name, Decimal("0")) + (
                position.shares * prices[position.ticker]
            )
        for category, values in (
            ("stock", stock_values),
            ("sector", sector_values),
            ("broker", broker_values),
        ):
            allocations[category] = tuple(
                Allocation(name, value, value / current_value)
                for name, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))
            )
        weights = sorted((holding.weight for holding in holdings), reverse=True)
        # Sum market values before dividing: adding rounded repeating weights
        # can incorrectly turn an exact 60% boundary into a strict breach.
        top3_value = sum(sorted(stock_values.values(), reverse=True)[:3], Decimal("0"))
        top3_weight = top3_value / current_value
        breaches = int(weights[0] > single_threshold) + int(top3_weight > top3_threshold)
        concentration = ("Low", "Moderate", "High")[breaches]
    return PortfolioValuation(
        tuple(holdings),
        current_value,
        basis,
        gain,
        gain_pct,
        realized,
        daily,
        daily_pct,
        complete,
        tuple(issues),
        allocations,
        concentration,
        top3_weight,
        as_of if complete else None,
        previous_as_of if complete and daily_complete else None,
    )
