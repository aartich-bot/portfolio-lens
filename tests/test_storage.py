"""Check duplicate decisions, durable metadata, and atomic database changes."""

import sqlite3
from datetime import date

import pytest

from portfolio_lens.models import ALREADY_ADJUSTED, AS_TRADED, CSV_COLUMNS
from portfolio_lens.services.transactions import TransactionService

HEADER = ",".join(CSV_COLUMNS)
APPLE = "AAPL,2024-01-03,BUY,1.2500,10.10,Fidelity\n"
MICROSOFT = "MSFT,2024-02-01,BUY,2,20,Vanguard\n"


@pytest.fixture
def service(tmp_path):
    return TransactionService(tmp_path / "portfolio.db", today=date(2026, 10, 12))


def preview(service, rows=APPLE, mode="append", split=ALREADY_ADJUSTED):
    return service.preview_csv((HEADER + "\n" + rows).encode(), "upload.csv", split, mode)


def test_persistence_preserves_values_and_import_provenance(service):
    result = service.commit(preview(service, split=AS_TRADED))
    assert result.inserted_count == result.total_count == 1
    reopened = TransactionService(service.store.db_path)
    rows = reopened.list_transactions()
    assert rows[0]["quantity"] == "1.2500"
    assert rows[0]["price"] == "10.10"
    assert rows[0]["source_name"] == "upload.csv"
    assert rows[0]["source_row"] == 2
    assert rows[0]["split_convention"] == AS_TRADED
    assert rows[0]["imported_at"].endswith("+00:00")


def test_repeated_sample_load_skips_existing_and_batch_duplicates(service):
    first = service.commit(preview(service, APPLE + APPLE))
    assert first.inserted_count == 1
    assert first.skipped_count == 1
    repeated = preview(service, APPLE + MICROSOFT + MICROSOFT)
    assert repeated.duplicate_rows == (2, 4)
    second = service.commit(repeated)
    assert second.inserted_count == 1
    assert second.skipped_count == 2
    assert second.total_count == 2


def test_keep_intentional_duplicates_is_explicit(service):
    service.commit(preview(service))
    result = service.commit(preview(service, APPLE + APPLE), duplicate_policy="keep")
    assert result.inserted_count == 2
    assert result.skipped_count == 0
    assert result.total_count == 3


def test_replace_only_compares_duplicates_within_new_batch(service):
    service.commit(preview(service, APPLE + MICROSOFT))
    replacement = preview(service, APPLE + APPLE, mode="replace")
    assert replacement.duplicate_rows == (3,)
    result = service.commit(replacement, mode="replace")
    assert result.total_count == 1
    assert result.skipped_count == 1
    assert service.list_transactions()[0]["ticker"] == "AAPL"


def test_invalid_replace_leaves_saved_data_untouched(service):
    service.commit(preview(service))
    before = service.list_transactions()
    invalid = preview(service, "MSFT,2024-02-01,BUY,-2,20,Vanguard\n", mode="replace")
    with pytest.raises(ValueError, match="validation errors"):
        service.commit(invalid, mode="replace")
    assert service.list_transactions() == before


def test_failed_replacement_rolls_back_deletion_and_revision(service):
    service.commit(preview(service))
    before = service.store.snapshot()
    replacement = preview(service, MICROSOFT, mode="replace")
    with sqlite3.connect(service.store.db_path) as connection:
        connection.execute("""
            CREATE TRIGGER simulate_write_failure BEFORE INSERT ON transactions
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        service.commit(replacement, mode="replace")
    assert service.store.snapshot() == before


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_other_session_change_invalidates_preview(service, mode):
    old_preview = preview(service, APPLE, mode=mode)
    other_session = TransactionService(service.store.db_path)
    other_session.commit(preview(other_session, MICROSOFT))
    with pytest.raises(ValueError, match="Preview again"):
        service.commit(old_preview, mode=mode)
    assert [record["ticker"] for record in service.list_transactions()] == ["MSFT"]


def test_changing_mode_or_unknown_duplicate_policy_requires_new_preview(service):
    initial = preview(service)
    with pytest.raises(ValueError, match="mode changed"):
        service.commit(initial, mode="replace")
    with pytest.raises(ValueError, match="Duplicate policy"):
        service.commit(initial, duplicate_policy="guess")
    assert service.list_transactions() == []
