"""Catch sales funded by the wrong broker, future purchases, or skipped duplicates."""

from datetime import date

import pytest

from portfolio_lens.models import (
    ALREADY_ADJUSTED,
    AS_TRADED,
    CSV_COLUMNS,
    MarketSnapshot,
    SplitEvent,
)
from portfolio_lens.services.transactions import TransactionService

HEADER = ",".join(CSV_COLUMNS) + "\n"
BUY = "AAPL,2026-01-02,BUY,1,100,Fidelity\n"


@pytest.fixture
def service(tmp_path):
    return TransactionService(tmp_path / "portfolio.db", today=date(2026, 10, 13))


def preview(service, text, mode="append"):
    return service.preview_csv((HEADER + text).encode(), "trades.csv", ALREADY_ADJUSTED, mode)


def test_overselling_blocks_entire_import_and_preserves_history(service):
    service.commit(preview(service, BUY))
    before = service.list_transactions()
    result = preview(service, "AAPL,2026-02-02,SELL,2,120,Fidelity\n")
    assert not result.is_valid
    assert any(issue.field == "holdings" and issue.row == 2 for issue in result.issues)
    with pytest.raises(ValueError):
        service.commit(result)
    assert service.list_transactions() == before


def test_other_broker_or_later_purchase_cannot_fund_a_sale(service):
    cross_broker = preview(service, BUY + "AAPL,2026-02-02,SELL,1,120,Robinhood\n")
    backdated = preview(service, BUY + "AAPL,2025-12-01,SELL,1,120,Fidelity\n")
    assert not cross_broker.is_valid
    assert not backdated.is_valid


def test_duplicate_choice_changes_the_effective_ledger(service):
    service.commit(preview(service, BUY))
    result = preview(service, BUY + "AAPL,2026-02-02,SELL,2,120,Fidelity\n")
    assert result.duplicate_count == 1
    assert not result.is_valid  # The repeated buy is skipped by default.
    checked = service.review_preview(result, duplicate_policy="keep")
    assert checked.is_valid
    saved = service.commit(result, duplicate_policy="keep")
    assert saved.inserted_count == 2
    assert saved.total_count == 3


def test_replacement_cannot_use_holdings_it_is_about_to_delete(service):
    service.commit(preview(service, BUY))
    result = preview(service, "AAPL,2026-02-02,SELL,1,120,Fidelity\n", mode="replace")
    assert not result.is_valid
    with pytest.raises(ValueError):
        service.commit(result, mode="replace")
    assert len(service.list_transactions()) == 1


def test_holidays_warn_without_rewriting_transaction_dates(service):
    result = preview(service, "AAPL,2026-05-25,BUY,1,100,Fidelity\n")
    assert result.is_valid
    assert result.transactions[0].date == date(2026, 5, 25)
    assert any(
        issue.severity == "warning" and "holiday" in issue.message for issue in result.issues
    )


def test_raw_split_history_is_applied_before_oversell_check_without_rewriting_rows(tmp_path):
    from decimal import Decimal

    class SplitClient:
        def get_snapshot(self, tickers, **kwargs):
            assert kwargs["split_start"] == {"NVDA": date(2024, 6, 7)}
            return MarketSnapshot(
                {}, {"NVDA": (SplitEvent("NVDA", date(2024, 6, 10), Decimal("10")),)}, {}
            )

    service = TransactionService(
        tmp_path / "raw.db", today=date(2026, 10, 13), market_client=SplitClient()
    )
    body = HEADER + "NVDA,2024-06-07,BUY,1,1000,Fidelity\nNVDA,2024-06-11,SELL,5,150,Fidelity\n"
    result = service.preview_csv(body.encode(), "raw.csv", AS_TRADED)
    assert result.is_valid  # The single original share became ten before the sale.
    service.commit(result)
    saved = service.list_transactions()
    assert saved[0]["quantity"] == "1"
    assert saved[0]["price"] == "1000"
    assert saved[0]["split_convention"] == AS_TRADED


def test_unknown_split_history_blocks_raw_sale_instead_of_guessing(tmp_path):
    class OfflineClient:
        def get_snapshot(self, tickers, **kwargs):
            return MarketSnapshot({}, {}, {"NVDA": "Split history unavailable"})

    service = TransactionService(
        tmp_path / "raw.db", today=date(2026, 10, 13), market_client=OfflineClient()
    )
    body = HEADER + "NVDA,2024-06-07,BUY,1,1000,Fidelity\nNVDA,2024-06-11,SELL,5,150,Fidelity\n"
    result = service.preview_csv(body.encode(), "raw.csv", AS_TRADED)
    assert not result.is_valid
    assert any("split" in issue.message.lower() for issue in result.issues)
    with pytest.raises(ValueError):
        service.commit(result)
    assert service.list_transactions() == []
