"""Exports preserve stored facts, reject spreadsheet formulas, and fit one PDF page."""

import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO, StringIO

import pytest
from pypdf import PdfReader

from portfolio_lens.calculations.valuation import Holding, PortfolioValuation
from portfolio_lens.models import Transaction


def csv_rows(payload):
    return list(csv.reader(StringIO(payload.decode("utf-8-sig"))))


def stored_record(**changes):
    record = {
        "id": 19,
        "ticker": "AAPL",
        "date": "2025-01-02",
        "transaction_type": "BUY",
        "quantity": "1.2300",
        "price": "1E+2",
        "broker": 'Long, "Broker" name',
        "source_row": 4,
        "source_name": "private-source.csv",
        "split_convention": "as_traded",
        "imported_at": "private-timestamp",
    }
    return record | changes


def valuation(complete=True):
    return PortfolioValuation(
        holdings=(
            Holding(
                "AAPL",
                Decimal("1.2300"),
                Decimal("100.00"),
                Decimal("123.0000"),
                Decimal("120.00") if complete else None,
                Decimal("147.6000") if complete else None,
                Decimal("24.6000") if complete else None,
                Decimal("0.20") if complete else None,
                Decimal("1") if complete else None,
                "Technology",
            ),
        ),
        current_value=Decimal("147.6000") if complete else None,
        cost_basis=Decimal("123.0000"),
        unrealized_gain=None,
        unrealized_pct=None,
        realized_gain=Decimal("0"),
        today_change=None,
        today_change_pct=None,
        complete=complete,
        issues=(),
        allocations={},
        concentration=None,
        top3_weight=None,
        as_of=datetime(2025, 1, 6, 21, tzinfo=timezone.utc) if complete else None,
    )


def test_transactions_preserve_exact_original_fields_order_and_exclude_metadata():
    from portfolio_lens.exports import transactions_csv

    records = [stored_record(), stored_record(date="2024-12-30", quantity="2.000")]
    result = csv_rows(transactions_csv(records))
    assert result == [
        ["ticker", "date", "transaction_type", "quantity", "price", "broker"],
        ["AAPL", "2025-01-02", "BUY", "1.2300", "1E+2", 'Long, "Broker" name'],
        ["AAPL", "2024-12-30", "BUY", "2.000", "1E+2", 'Long, "Broker" name'],
    ]
    assert records[0]["id"] == 19


def test_transaction_models_keep_decimal_precision_and_iso_dates():
    from portfolio_lens.exports import transactions_csv

    transaction = Transaction(
        "MSFT", date(2025, 2, 3), "BUY", Decimal("0.00000001"), Decimal("234.5600"), "Broker", 22
    )
    assert csv_rows(transactions_csv([transaction]))[1] == [
        "MSFT",
        "2025-02-03",
        "BUY",
        "0.00000001",
        "234.5600",
        "Broker",
    ]


@pytest.mark.parametrize(
    "unsafe", ["=SUM(1,2)", "+cmd", "-cmd", "@SUM(1,2)", "  =cmd", "\t=cmd", "\r@cmd", "\x00=cmd"]
)
def test_transaction_export_rejects_formulas_without_rewriting_broker(unsafe):
    from portfolio_lens.exports import ExportError, transactions_csv

    record = stored_record(broker=unsafe)
    with pytest.raises(ExportError, match="(?i)broker.*spreadsheet|spreadsheet.*broker"):
        transactions_csv([record])
    assert record["broker"] == unsafe


def test_missing_transaction_original_field_is_explained():
    from portfolio_lens.exports import ExportError, transactions_csv

    record = stored_record()
    del record["price"]
    with pytest.raises(ExportError, match="price"):
        transactions_csv([record])


def test_holdings_export_uses_supplied_values_and_explicit_units():
    from portfolio_lens.exports import holdings_csv

    rows = csv_rows(holdings_csv(valuation()))
    assert rows == [
        [
            "ticker",
            "shares",
            "average_cost",
            "cost_basis",
            "price",
            "market_value",
            "unrealized_gain",
            "unrealized_pct",
            "weight",
            "sector",
            "as_of",
            "status",
        ],
        [
            "AAPL",
            "1.2300",
            "100.00",
            "123.0000",
            "120.00",
            "147.6000",
            "24.6000",
            "0.20",
            "1",
            "Technology",
            "2025-01-06T21:00:00+00:00",
            "complete",
        ],
    ]


def test_missing_holdings_values_are_blank_and_status_unavailable():
    from portfolio_lens.exports import holdings_csv

    rows = csv_rows(holdings_csv(valuation(False)))
    assert rows[1][4:9] == ["", "", "", "", ""]
    assert rows[1][-2:] == ["", "unavailable"]
    assert len(csv_rows(holdings_csv(None))) == 1


def test_partial_valuation_labels_known_position_incomplete_and_catches_sector_formula():
    from portfolio_lens.exports import ExportError, holdings_csv

    partial = replace(valuation(), complete=False)
    assert csv_rows(holdings_csv(partial))[1][-1] == "incomplete"
    malicious = replace(partial, holdings=(replace(partial.holdings[0], sector="=CMD()"),))
    with pytest.raises(ExportError, match="sector"):
        holdings_csv(malicious)


def report_data(**changes):
    from portfolio_lens.exports import ReportData

    return ReportData(
        as_of="January 6, 2025 at 21:00 UTC (completed session)",
        metrics=(("Current value", "$147.60"), ("Cost basis", "$123.00")),
        alerts=("Holding concentration exceeds the selected 25% product threshold.",),
        max_drawdown="-12.34% (January 2-6, 2025)",
        projection=(
            "Lower: $150; Base: $200; Higher: $250 after 5 years.",
            "End-of-month contribution: $100; rates: -2%, 3%, 8%.",
        ),
        summary="AAPL & MSFT <sample> verified holdings.",
        notices=(
            "Prices are cached; verify freshness.",
            "Sector data is incomplete.",
        ),
        **changes,
    )


def test_pdf_contains_supplied_facts_required_assumptions_and_escaped_text_on_one_page():
    from portfolio_lens.exports import report_pdf

    result = report_pdf(report_data())
    reader = PdfReader(BytesIO(result))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    for expected in [
        "Vamshi's Portfolio Lens",
        "January 6, 2025 at 21:00 UTC",
        "Current value: $147.60",
        "Cost basis: $123.00",
        "-12.34%",
        "Lower: $150; Base: $200; Higher: $250",
        "AAPL & MSFT <sample>",
        "Prices are cached",
        "Sector data is incomplete",
        "FIFO",
        "Dividends are excluded",
        "product thresholds",
        "not forecasts",
        "midpoint cash-flow approximation",
    ]:
        assert expected in text


def test_long_pdf_content_is_explicitly_truncated_and_never_adds_pages():
    from portfolio_lens.exports import report_pdf

    huge = "<b>unsafe XML & endless text</b> " * 10000
    data = replace(
        report_data(),
        as_of=huge,
        metrics=tuple((huge, huge) for _ in range(25)),
        alerts=(huge,) * 25,
        max_drawdown=huge,
        projection=(huge,) * 25,
        summary=huge,
        notices=(huge,) * 25,
    )
    reader = PdfReader(BytesIO(report_pdf(data)))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "truncated" in text.lower()
    assert "FIFO" in text and "not forecasts" in text


def test_pdf_transliterates_unicode_symbols_readably_and_discloses_fallback():
    from portfolio_lens.exports import report_pdf

    data = replace(report_data(), summary="Café — gain ≥ 25%; São Paulo; 漢字.")
    reader = PdfReader(BytesIO(report_pdf(data)))
    text = reader.pages[0].extract_text()
    assert "Cafe - gain >= 25%; Sao Paulo; ??." in text
    assert "ASCII transliteration" in text


@pytest.mark.parametrize("unsafe", ["\u00a0=cmd", "\u200b=cmd", "\x01@cmd"])
def test_formula_detection_handles_hidden_unicode_and_control_prefixes(unsafe):
    from portfolio_lens.exports import ExportError, transactions_csv

    with pytest.raises(ExportError, match="broker"):
        transactions_csv([stored_record(broker=unsafe)])


@pytest.mark.parametrize("field", ["ticker", "date", "transaction_type", "quantity", "price"])
def test_formula_detection_covers_every_original_text_cell(field):
    from portfolio_lens.exports import ExportError, transactions_csv

    with pytest.raises(ExportError, match=field):
        transactions_csv([stored_record(**{field: "=unsafe()"})])


def test_holdings_loss_is_preserved_as_numeric_negative_gain():
    from portfolio_lens.exports import holdings_csv

    value = valuation()
    loss = replace(
        value,
        holdings=(
            replace(
                value.holdings[0], unrealized_gain=Decimal("-1.25"), unrealized_pct=Decimal("-0.1")
            ),
        ),
    )
    assert csv_rows(holdings_csv(loss))[1][6:8] == ["-1.25", "-0.1"]
