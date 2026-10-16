"""Read CSV bytes into rows while keeping their original file locations."""

import csv
import io
from dataclasses import dataclass

from portfolio_lens.models import CSV_COLUMNS, ValidationIssue

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000


@dataclass(frozen=True)
class RawRow:
    source_row: int
    values: dict[str, str]


def parse_csv(data: bytes) -> tuple[list[RawRow], list[ValidationIssue]]:
    """Parse the exact six-column CSV format; invalid files remain uncommittable."""
    rows: list[RawRow] = []
    issues: list[ValidationIssue] = []

    def error(message: str, row: int | None = None) -> None:
        issues.append(ValidationIssue("error", row, None, message))

    if len(data) > MAX_UPLOAD_BYTES:
        error("The CSV is too large; upload a file of 5 MB or less.")
        return rows, issues
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        error("The CSV must use UTF-8 encoding; save it as a UTF-8 CSV and retry.")
        return rows, issues
    if not text.strip():
        error("The CSV is empty; include the header and at least one transaction.")
        return rows, issues
    if "\x00" in text:
        error("The file contains invalid null characters; upload a plain UTF-8 CSV.")
        return rows, issues

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = [column.strip().lower() for column in next(reader)]
        if len(header) != len(CSV_COLUMNS) or set(header) != set(CSV_COLUMNS):
            error("Use exactly these six columns, once each: " + ", ".join(CSV_COLUMNS), 1)
            return rows, issues
        while True:
            line_number = reader.line_num + 1
            try:
                values = next(reader)
            except StopIteration:
                break
            if not values or all(not value.strip() for value in values):
                continue
            if len(rows) >= MAX_ROWS:
                error(
                    "The CSV contains too many rows; upload at most 10,000 transactions.",
                    line_number,
                )
                break
            if len(values) != len(CSV_COLUMNS):
                error(f"Expected six fields but found {len(values)}.", line_number)
                continue
            rows.append(RawRow(line_number, dict(zip(header, values, strict=True))))
    except csv.Error as exc:
        error(f"The CSV is malformed: {exc}.", reader.line_num or 1)

    if not rows and not issues:
        error("The CSV has a header but no transactions.")
    return rows, issues
