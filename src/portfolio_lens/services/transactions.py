"""Coordinate transaction previews and safe saves without importing Streamlit."""

from dataclasses import replace
from datetime import date
from pathlib import Path

from portfolio_lens.calculations.accounting import replay_ledger
from portfolio_lens.calculations.corporate_actions import normalize_records
from portfolio_lens.ingestion import RawRow, parse_csv
from portfolio_lens.market_data import MarketDataClient
from portfolio_lens.models import (
    AS_TRADED,
    SPLIT_CONVENTIONS,
    CommitResult,
    ImportPreview,
    ValidationIssue,
)
from portfolio_lens.storage import TransactionStore, record_key
from portfolio_lens.validation import find_duplicates, validate_rows


class TransactionService:
    def __init__(self, db_path: str | Path, *, today: date | None = None, market_client=None):
        self.store = TransactionStore(db_path)
        self.today = today
        self.market_client = market_client

    def _preview(
        self,
        rows: list[RawRow],
        issues: list[ValidationIssue],
        source_name: str,
        split_convention: str,
        mode: str,
    ) -> ImportPreview:
        if mode not in {"append", "replace"}:
            raise ValueError("Import mode must be append or replace.")
        if split_convention not in SPLIT_CONVENTIONS:
            issues.append(
                ValidationIssue(
                    "error", None, "split_convention", "Choose a valid stock-split convention."
                )
            )
        transactions, field_issues = validate_rows(rows, today=self.today)
        issues.extend(field_issues)
        revision, existing = self.store.snapshot()
        existing_keys = {record_key(record) for record in existing} if mode == "append" else set()
        duplicates, duplicate_issues = find_duplicates(transactions, existing_keys)
        issues.extend(duplicate_issues)
        preview = ImportPreview(
            tuple(transactions),
            tuple(issues),
            source_name,
            split_convention,
            mode,
            duplicates,
            revision,
        )
        return self.review_preview(preview)

    def review_preview(
        self, preview: ImportPreview, duplicate_policy: str = "skip"
    ) -> ImportPreview:
        """Validate exactly the history that the selected save options would produce."""
        if duplicate_policy not in {"skip", "keep"}:
            raise ValueError("Duplicate policy must be skip or keep.")
        # Field/CSV errors remain; holdings errors can change when duplicate choices change.
        issues = [issue for issue in preview.issues if issue.field != "holdings"]
        if any(issue.severity == "error" for issue in issues):
            return replace(preview, issues=tuple(issues))
        revision, saved = self.store.snapshot()
        if revision != preview.storage_revision:
            issues.append(
                ValidationIssue(
                    "error",
                    None,
                    "holdings",
                    "Saved transactions changed after this preview. Preview again before saving.",
                )
            )
            return replace(preview, issues=tuple(issues))
        records = list(saved) if preview.mode == "append" else []
        seen = {record_key(record) for record in records}
        for transaction in preview.transactions:
            if duplicate_policy == "skip" and transaction.duplicate_key in seen:
                continue
            seen.add(transaction.duplicate_key)
            records.append(
                {
                    **transaction.as_dict(),
                    "source_row": transaction.source_row,
                    "source_name": preview.source_name,
                    "split_convention": preview.split_convention,
                }
            )
        sold_tickers = {
            record["ticker"] for record in records if record["transaction_type"] == "SELL"
        }
        split_start: dict[str, date] = {}
        for record in records:
            ticker = record["ticker"]
            if record["split_convention"] == AS_TRADED and ticker in sold_tickers:
                day = date.fromisoformat(record["date"])
                split_start[ticker] = min(day, split_start.get(ticker, day))
        splits = {}
        if split_start:
            client = self.market_client or MarketDataClient(self.store.db_path)
            market = client.get_snapshot(split_start, split_start=split_start)
            if market.split_errors:
                for ticker, message in market.split_errors.items():
                    issues.append(
                        ValidationIssue(
                            "error",
                            None,
                            "holdings",
                            f"Cannot verify {ticker} shares until stock-split history is available: {message}",
                        )
                    )
                return replace(preview, issues=tuple(issues))
            splits = market.splits
        # Buys without any sales do not need market data merely to be saved.
        if sold_tickers:
            ledger = replay_ledger(normalize_records(records, splits))
            issues.extend(
                ValidationIssue("error", issue.source_row, "holdings", issue.message)
                for issue in ledger.issues
            )
        return replace(preview, issues=tuple(issues))

    def preview_csv(
        self,
        data: bytes,
        source_name: str,
        split_convention: str,
        mode: str = "append",
    ) -> ImportPreview:
        rows, issues = parse_csv(data)
        return self._preview(rows, issues, source_name, split_convention, mode)

    def preview_manual(
        self,
        values: dict,
        split_convention: str,
        mode: str = "append",
    ) -> ImportPreview:
        return self._preview([RawRow(1, values)], [], "Manual entry", split_convention, mode)

    def commit(
        self,
        preview: ImportPreview,
        mode: str = "append",
        duplicate_policy: str = "skip",
    ) -> CommitResult:
        if mode != preview.mode:
            raise ValueError("The import mode changed after preview. Preview again before saving.")
        if duplicate_policy not in {"skip", "keep"}:
            raise ValueError("Duplicate policy must be skip or keep.")
        checked = self.review_preview(preview, duplicate_policy)
        if not checked.is_valid:
            errors = " ".join(
                issue.message for issue in checked.issues if issue.severity == "error"
            )
            raise ValueError(
                f"Fix all validation errors before saving; no transactions were changed. {errors}"
            )
        # Store.commit checks this revision again under its write lock, closing the race.
        return self.store.commit(checked, duplicate_policy)

    def list_transactions(self) -> list[dict]:
        return self.store.snapshot()[1]
