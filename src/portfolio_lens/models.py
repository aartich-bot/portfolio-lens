"""Plain data shared by the application, without any UI dependencies."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

CSV_COLUMNS = ("ticker", "date", "transaction_type", "quantity", "price", "broker")
ALREADY_ADJUSTED = "already_adjusted"
AS_TRADED = "as_traded"
SPLIT_CONVENTIONS = (ALREADY_ADJUSTED, AS_TRADED)


@dataclass(frozen=True)
class Transaction:
    ticker: str
    date: date
    transaction_type: str
    quantity: Decimal
    price: Decimal
    broker: str
    source_row: int

    @property
    def duplicate_key(self) -> tuple:
        """Compare equivalent decimals and broker capitalization equally."""
        return (
            self.ticker,
            self.date,
            self.transaction_type,
            self.quantity,
            self.price,
            self.broker.casefold(),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "ticker": self.ticker,
            "date": self.date.isoformat(),
            "transaction_type": self.transaction_type,
            "quantity": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "broker": self.broker,
        }


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    row: int | None
    field: str | None
    message: str


@dataclass(frozen=True)
class ImportPreview:
    transactions: tuple[Transaction, ...]
    issues: tuple[ValidationIssue, ...]
    source_name: str
    split_convention: str
    mode: str
    duplicate_rows: tuple[int, ...]
    storage_revision: int

    @property
    def is_valid(self) -> bool:
        return bool(self.transactions) and not any(
            issue.severity == "error" for issue in self.issues
        )

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_rows)


@dataclass(frozen=True)
class CommitResult:
    inserted_count: int
    skipped_count: int
    total_count: int
    mode: str


@dataclass(frozen=True)
class SplitEvent:
    ticker: str
    date: date
    ratio: Decimal


@dataclass(frozen=True)
class MarketQuote:
    ticker: str
    price: Decimal | None = None
    previous_close: Decimal | None = None
    as_of: datetime | None = None
    previous_as_of: datetime | None = None
    sector: str = "Unknown"
    quote_type: str = "EQUITY"
    fetched_at: datetime | None = None
    stale: bool = False
    error: str | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    quotes: dict[str, MarketQuote]
    splits: dict[str, tuple[SplitEvent, ...]]
    split_errors: dict[str, str]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoricalSnapshot:
    prices: dict[str, dict[date, Decimal]]
    splits: dict[str, tuple[SplitEvent, ...]]
    split_errors: dict[str, str]
    warnings: tuple[str, ...] = ()
    fetched_at: dict[str, datetime] | None = None
    stale_tickers: tuple[str, ...] = ()
