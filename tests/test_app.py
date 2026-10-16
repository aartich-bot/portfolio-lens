"""Exercise the actual Streamlit forms without touching the personal database."""

from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from portfolio_lens.services.transactions import TransactionService

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
CSV_HEADER = "ticker,date,transaction_type,quantity,price,broker\n"


def widget(elements, label):
    return next(element for element in elements if element.label == label)


def click(app, label):
    widget(app.button, label).click().run()
    assert not app.exception


@pytest.fixture
def app_and_service(tmp_path, monkeypatch):
    db_path = tmp_path / "portfolio.db"
    monkeypatch.setenv("PORTFOLIO_LENS_DB", str(db_path))
    monkeypatch.setattr("portfolio_lens.config.local_today", lambda: date(2026, 10, 12))
    monkeypatch.setattr("portfolio_lens.validation.local_today", lambda: date(2026, 10, 12))
    monkeypatch.setattr(
        "portfolio_lens.ui.add_transactions.local_today", lambda: date(2026, 10, 12)
    )
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    assert not app.exception
    return app, TransactionService(db_path)


def test_sample_review_save_repeat_and_restart(app_and_service):
    app, service = app_and_service
    assert service.list_transactions() == []

    click(app, "Load Sample Data")
    assert service.list_transactions() == []
    assert any("2 warning(s)" in notice.value for notice in app.warning)
    issues = app.dataframe[0].value
    assert issues.iloc[0]["CSV / entry row"] == "27"
    assert "2026-04-19 falls on a weekend" in issues.iloc[0]["Message"]
    preview_table = app.dataframe[1].value
    sunday = preview_table.loc[preview_table["row"] == 27].iloc[0]
    assert sunday["ticker"] == "MSFT"
    assert sunday["date"] == "2026-04-19"

    click(app, "Save transactions")
    assert len(service.list_transactions()) == 28
    assert any("Saved 28 transaction(s)" in notice.value for notice in app.success)

    click(app, "Load Sample Data")
    assert app.session_state["import_preview"].duplicate_count == 28
    assert widget(app.radio, "Duplicate rows").value == "Skip duplicate rows"
    click(app, "Save transactions")
    assert len(service.list_transactions()) == 28
    assert any("skipped 28 duplicate(s)" in notice.value for notice in app.success)

    restarted = AppTest.from_file(APP_PATH, default_timeout=30).run()
    assert not restarted.exception
    assert widget(restarted.metric, "Saved transactions").value == "28"
    assert len(service.list_transactions()) == 28


def test_manual_entry_saves_valid_input_and_blocks_invalid_input(app_and_service):
    app, service = app_and_service
    widget(app.radio, "Add transactions using").set_value("Manual entry").run()
    widget(app.text_input, "Ticker").set_value(" aapl ")
    widget(app.date_input, "Transaction date").set_value(date(2026, 1, 2))
    widget(app.text_input, "Quantity").set_value("0.125000000001")
    widget(app.text_input, "Price per share (USD)").set_value("195.58")
    widget(app.text_input, "Broker").set_value(" fidelity ")
    click(app, "Preview transaction")
    assert service.list_transactions() == []
    assert not widget(app.button, "Save transactions").disabled
    click(app, "Save transactions")
    records = service.list_transactions()
    assert len(records) == 1
    assert records[0]["quantity"] == "0.125000000001"
    assert records[0]["ticker"] == "AAPL"
    assert records[0]["broker"] == "Fidelity"
    assert records[0]["source_name"] == "Manual entry"

    widget(app.text_input, "Quantity").set_value("-1")
    click(app, "Preview transaction")
    assert any("validation error" in notice.value for notice in app.error)
    assert widget(app.button, "Save transactions").disabled
    assert service.list_transactions() == records


def test_csv_replace_requires_fresh_confirmation_and_replaces_saved_rows(app_and_service):
    app, service = app_and_service
    click(app, "Load Sample Data")
    click(app, "Save transactions")
    widget(app.radio, "Add transactions using").set_value("CSV upload").run()
    widget(app.radio, "When you save this batch").set_value("Replace saved transactions").run()
    content = (
        CSV_HEADER + "MSFT,2026-04-17,BUY,5,400,Fidelity\nMSFT,2026-04-19,SELL,5,425.82,Fidelity\n"
    ).encode()
    app.file_uploader[0].set_value(("replacement.csv", content, "text/csv"))
    click(app, "Preview CSV")
    assert len(service.list_transactions()) == 28
    assert widget(app.button, "Save transactions").disabled
    assert not app.checkbox[0].value

    app.checkbox[0].check().run()
    assert not widget(app.button, "Save transactions").disabled
    click(app, "Preview CSV")
    assert not app.checkbox[0].value
    assert widget(app.button, "Save transactions").disabled
    app.checkbox[0].check().run()
    click(app, "Save transactions")
    records = service.list_transactions()
    assert len(records) == 2
    assert records[1]["ticker"] == "MSFT"
    assert records[1]["date"] == "2026-04-19"
    assert records[1]["source_name"] == "replacement.csv"


def test_invalid_csv_disables_save_for_entire_batch(app_and_service):
    app, service = app_and_service
    click(app, "Load Sample Data")
    click(app, "Save transactions")
    before = service.list_transactions()
    widget(app.radio, "Add transactions using").set_value("CSV upload").run()
    content = (
        CSV_HEADER + "AAPL,2026-01-02,BUY,1,195.58,Fidelity\nMSFT,2026-01-02,BUY,NaN,400,Fidelity\n"
    ).encode()
    app.file_uploader[0].set_value(("invalid.csv", content, "text/csv"))
    click(app, "Preview CSV")
    assert any("validation error" in notice.value for notice in app.error)
    assert widget(app.button, "Save transactions").disabled
    assert service.list_transactions() == before


def test_changing_source_discards_preview_and_missing_upload_warns(app_and_service):
    app, service = app_and_service
    click(app, "Load Sample Data")
    assert any(button.label == "Save transactions" for button in app.button)
    widget(app.radio, "Add transactions using").set_value("CSV upload").run()
    assert not any(button.label == "Save transactions" for button in app.button)
    click(app, "Preview CSV")
    assert any("Choose a CSV file" in notice.value for notice in app.warning)
    assert service.list_transactions() == []
