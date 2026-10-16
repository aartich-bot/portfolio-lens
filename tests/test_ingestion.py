"""Check real CSV inputs, field errors, and non-blocking weekend warnings."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_lens.ingestion import MAX_UPLOAD_BYTES
from portfolio_lens.models import ALREADY_ADJUSTED, CSV_COLUMNS
from portfolio_lens.services.transactions import TransactionService

HEADER = ",".join(CSV_COLUMNS)


@pytest.fixture
def service(tmp_path):
    return TransactionService(tmp_path / "portfolio.db", today=date(2026, 10, 12))


def preview(service, body):
    return service.preview_csv(body.encode("utf-8"), "test.csv", ALREADY_ADJUSTED)


def test_sample_keeps_sunday_and_flags_market_holiday(service):
    sample = Path(__file__).resolve().parents[1] / "transactions.csv"
    result = service.preview_csv(sample.read_bytes(), sample.name, ALREADY_ADJUSTED)
    assert result.is_valid
    assert len(result.transactions) == 28
    sunday = next(
        transaction for transaction in result.transactions if transaction.source_row == 27
    )
    assert sunday.date == date(2026, 4, 19)
    assert sunday.ticker == "MSFT"
    warnings = [issue for issue in result.issues if issue.severity == "warning"]
    assert len(warnings) == 2
    assert warnings[0].row == 27
    assert "weekend" in warnings[0].message
    assert warnings[1].row == 29
    assert "holiday" in warnings[1].message


def test_bom_and_reordered_headers_normalize_without_losing_precision(service):
    result = preview(
        service,
        "\ufeff Price , BROKER , TICKER , Quantity , DATE , TRANSACTION_TYPE\r\n"
        "85.5000, fidelity , nvda ,0.123456789012,2026-02-02,buy\r\n",
    )
    assert result.is_valid
    transaction = result.transactions[0]
    assert transaction.ticker == "NVDA"
    assert transaction.broker == "Fidelity"
    assert transaction.price == Decimal("85.5000")
    assert transaction.quantity == Decimal("0.123456789012")
    assert transaction.as_dict()["quantity"] == "0.123456789012"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "  \n",
        HEADER + "\n",
        HEADER.replace("broker", "ticker") + "\n",
        HEADER + ",fees\nAAPL,2024-01-03,BUY,1,10,Fidelity,1",
        HEADER + '\n"unclosed,2024-01-03,BUY,1,10,Fidelity',
        HEADER + "\nAAPL,2024-01-03,BUY,1,10",
        HEADER + "\n\x00",
    ],
)
def test_bad_files_are_blocked(service, body):
    result = preview(service, body)
    assert not result.is_valid
    assert any(issue.severity == "error" for issue in result.issues)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticker", ""),
        ("ticker", "AAPL MSFT"),
        ("transaction_type", "DIVIDEND"),
        ("date", "2026-02-30"),
        ("date", "2026-2-02"),
        ("date", "2026-10-13"),
        ("date", "2026-02-02T12:00:00"),
        ("quantity", "0"),
        ("quantity", "-3"),
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("price", "-Infinity"),
        ("price", "$10"),
        ("price", "1_000"),
        ("price", "1e9999"),
        ("quantity", "0.0000000000001"),
        ("broker", ""),
    ],
)
def test_invalid_fields_report_the_source_row(service, field, value):
    values = dict(zip(CSV_COLUMNS, ["AAPL", "2026-02-02", "BUY", "1", "10", "Fidelity"]))
    values[field] = value
    result = service.preview_manual(values, ALREADY_ADJUSTED)
    assert not result.is_valid
    assert any(issue.field == field and issue.row == 1 for issue in result.issues)


def test_valid_rows_do_not_hide_errors_elsewhere_in_batch(service):
    result = preview(
        service,
        HEADER + "\nAAPL,2024-01-03,BUY,1,10,Fidelity\nMSFT,2024-01-03,BUY,-5,20,Fidelity\n",
    )
    assert len(result.transactions) == 1
    assert not result.is_valid
    with pytest.raises(ValueError, match="validation errors"):
        service.commit(result)
    assert service.list_transactions() == []


def test_duplicates_match_normalized_values_and_allow_other_brokers(service):
    result = preview(
        service,
        HEADER + "\naapl,2024-01-03,buy,1.0,10,Other Broker\n"
        "AAPL,2024-01-03,BUY,1,10.00,other broker\n",
    )
    assert result.is_valid
    assert result.duplicate_rows == (3,)
    assert result.duplicate_count == 1
    assert result.transactions[0].broker == "Other Broker"


def test_invalid_encoding_oversized_file_and_row_limit(service):
    invalid_utf8 = service.preview_csv(b"\xff", "test.csv", ALREADY_ADJUSTED)
    assert not invalid_utf8.is_valid
    oversized = service.preview_csv(b"x" * (MAX_UPLOAD_BYTES + 1), "test.csv", ALREADY_ADJUSTED)
    assert not oversized.is_valid
    assert "5 MB" in oversized.issues[0].message
    over_rows = preview(service, HEADER + "\n" + "AAPL,2024-01-03,BUY,1,10,Fidelity\n" * 10_001)
    assert not over_rows.is_valid
    assert any("10,000" in issue.message for issue in over_rows.issues)


def test_blank_lines_preserve_physical_row_numbers(service):
    result = preview(
        service,
        HEADER + "\n\nMSFT,2024-01-03,BUY,5,10,Fidelity\n\n"
        "MSFT,2026-04-19,SELL,5,425.82,Fidelity\n",
    )
    assert result.is_valid
    assert result.transactions[1].source_row == 5
    assert result.issues[0].row == 5
