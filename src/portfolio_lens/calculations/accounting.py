"""Replay exact transaction values into FIFO positions, separately per broker."""

from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext


@dataclass(frozen=True)
class AccountingIssue:
    ticker: str
    broker: str
    date: date
    source_row: int | None
    message: str


@dataclass(frozen=True)
class Position:
    ticker: str
    broker: str
    shares: Decimal
    cost_basis: Decimal
    realized_gain: Decimal

    @property
    def average_cost(self) -> Decimal | None:
        if not self.shares:
            return None
        with localcontext() as context:
            context.prec = 50
            return self.cost_basis / self.shares


@dataclass(frozen=True)
class LedgerResult:
    positions: tuple[Position, ...]
    issues: tuple[AccountingIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


def replay_ledger(records: list[dict]) -> LedgerResult:
    """Replay normalized rows by trade date, retaining input order within a day.

    Sales consume lots only within the same ticker and casefolded broker. An
    oversell produces an issue and leaves that account intact; callers must
    reject the entire invalid ledger rather than value or commit partial output.
    Closed positions remain present to preserve cumulative realized profit.
    """
    accounts: dict[tuple[str, str], dict] = {}
    issues = []
    with localcontext() as context:
        context.prec = 50
        for record in sorted(records, key=lambda row: date.fromisoformat(row["date"])):
            ticker, broker = record["ticker"], record["broker"]
            trade_date = date.fromisoformat(record["date"])

            def issue(message):
                issues.append(
                    AccountingIssue(ticker, broker, trade_date, record.get("source_row"), message)
                )

            try:
                quantity = Decimal(str(record["quantity"]))
                price = Decimal(str(record["price"]))
            except (InvalidOperation, ValueError):
                issue("Quantity and price must be finite positive numbers.")
                continue
            if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
                issue("Quantity and price must be finite positive numbers.")
                continue
            kind = record["transaction_type"]
            if kind not in ("BUY", "SELL"):
                issue(f"Unknown transaction type: {kind}.")
                continue
            key = (ticker, broker.casefold())
            account = accounts.setdefault(
                key,
                {
                    "broker": broker,
                    "lots": deque(),
                    "realized": Decimal("0"),
                },
            )
            lots = account["lots"]
            if kind == "BUY":
                lots.append([quantity, price])
                continue
            available = sum((lot[0] for lot in lots), Decimal("0"))
            if quantity > available:
                issue(
                    f"Sell of {quantity} shares exceeds {available} shares available "
                    f"in {broker} for {ticker}."
                )
                continue
            remaining = quantity
            while remaining:
                lot_quantity, lot_price = lots[0]
                consumed = min(remaining, lot_quantity)
                account["realized"] += consumed * (price - lot_price)
                remaining -= consumed
                if consumed == lot_quantity:
                    lots.popleft()
                else:
                    lots[0][0] -= consumed
        positions = tuple(
            Position(
                ticker,
                account["broker"],
                sum((lot[0] for lot in account["lots"]), Decimal("0")),
                sum((lot[0] * lot[1] for lot in account["lots"]), Decimal("0")),
                account["realized"],
            )
            for (ticker, _), account in accounts.items()
        )
    return LedgerResult(positions, tuple(issues))
