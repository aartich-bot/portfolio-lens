"""Validate transaction fields and flag closed-market dates and possible duplicates."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from portfolio_lens.ingestion import RawRow
from portfolio_lens.models import CSV_COLUMNS, Transaction, ValidationIssue
from portfolio_lens.trading_calendar import non_trading_reason

_BROKERS = {name.casefold(): name for name in ("Robinhood", "Fidelity", "Vanguard")}
_TICKER = re.compile(r"[A-Z0-9^][A-Z0-9.^=\-]{0,19}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def local_today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def validate_rows(
    rows: list[RawRow],
    *,
    today: date | None = None,
) -> tuple[list[Transaction], list[ValidationIssue]]:
    transactions: list[Transaction] = []
    issues: list[ValidationIssue] = []
    today = today or local_today()

    for row in rows:
        row_issues: list[ValidationIssue] = []

        def error(field: str, message: str) -> None:
            row_issues.append(ValidationIssue("error", row.source_row, field, message))

        values = {
            key: "" if row.values.get(key) is None else str(row.values[key]).strip()
            for key in CSV_COLUMNS
        }
        ticker = values["ticker"].upper()
        if not _TICKER.fullmatch(ticker):
            error("ticker", "Enter a ticker of 1–20 characters using letters, numbers, or . - ^ =.")
        transaction_type = values["transaction_type"].upper()
        if transaction_type not in {"BUY", "SELL"}:
            error("transaction_type", "Transaction type must be BUY or SELL.")

        transaction_date = None
        try:
            if not _DATE.fullmatch(values["date"]):
                raise ValueError
            transaction_date = date.fromisoformat(values["date"])
            if transaction_date > today:
                error("date", "Transaction date cannot be in the future (America/Los_Angeles).")
            else:
                try:
                    reason = non_trading_reason(transaction_date)
                except ValueError:
                    reason = "Trading-calendar coverage is unavailable for this date; it is kept as entered."
                if reason:
                    row_issues.append(ValidationIssue("warning", row.source_row, "date", reason))
        except ValueError:
            error("date", "Enter a real date in YYYY-MM-DD format.")

        numbers: dict[str, Decimal] = {}
        for field in ("quantity", "price"):
            try:
                # Limits keep accidental exponent-sized values out of later calculations.
                if not values[field] or len(values[field]) > 50 or "_" in values[field]:
                    raise InvalidOperation
                number = Decimal(values[field])
                if not number.is_finite() or number <= 0:
                    raise InvalidOperation
                if number > Decimal("1000000000000") or number.as_tuple().exponent < -12:
                    error(
                        field,
                        "Use at most 12 decimal places and a value no greater than 1 trillion.",
                    )
                else:
                    numbers[field] = number
            except InvalidOperation:
                error(
                    field,
                    "Enter a finite number greater than zero, without currency symbols or commas.",
                )

        broker = " ".join(values["broker"].split())
        broker = _BROKERS.get(broker.casefold(), broker)
        if not broker or len(broker) > 100 or any(ord(char) < 32 for char in values["broker"]):
            error("broker", "Enter a broker name of 1–100 characters without control characters.")

        issues.extend(row_issues)
        if not any(issue.severity == "error" for issue in row_issues):
            assert transaction_date is not None
            transactions.append(
                Transaction(
                    ticker,
                    transaction_date,
                    transaction_type,
                    numbers["quantity"],
                    numbers["price"],
                    broker,
                    row.source_row,
                )
            )
    return transactions, issues


def find_duplicates(
    transactions: list[Transaction] | tuple[Transaction, ...],
    existing_keys: set[tuple],
) -> tuple[tuple[int, ...], list[ValidationIssue]]:
    seen = set(existing_keys)
    duplicate_rows: list[int] = []
    issues: list[ValidationIssue] = []
    for transaction in transactions:
        key = transaction.duplicate_key
        if key in seen:
            duplicate_rows.append(transaction.source_row)
            location = "saved transactions" if key in existing_keys else "this batch"
            issues.append(
                ValidationIssue(
                    "warning",
                    transaction.source_row,
                    None,
                    f"Possible duplicate of a transaction in {location}; skipped by default unless you choose to keep duplicates.",
                )
            )
        seen.add(key)
    return tuple(duplicate_rows), issues
