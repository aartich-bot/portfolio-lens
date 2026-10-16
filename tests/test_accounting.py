"""Financial invariants for splits and FIFO accounting, independent of market I/O."""

from copy import deepcopy
from csv import DictReader
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from portfolio_lens.models import ALREADY_ADJUSTED, AS_TRADED, SplitEvent


def record(
    ticker="AAPL",
    day="2025-01-02",
    kind="BUY",
    qty="10",
    price="100",
    broker="Fidelity",
    row=2,
    convention=ALREADY_ADJUSTED,
):
    return {
        "ticker": ticker,
        "date": day,
        "transaction_type": kind,
        "quantity": qty,
        "price": price,
        "broker": broker,
        "source_row": row,
        "split_convention": convention,
    }


def test_fifo_partial_sale_consumes_oldest_lots_and_retains_remaining_basis():
    from portfolio_lens.calculations.accounting import replay_ledger

    result = replay_ledger(
        [
            record(qty="10", price="100"),
            record(day="2025-01-03", qty="5", price="200"),
            record(day="2025-01-06", kind="SELL", qty="12", price="150"),
        ]
    )
    assert result.valid
    (position,) = result.positions
    assert position.shares == Decimal("3")
    assert position.cost_basis == Decimal("600")
    assert position.average_cost == Decimal("200")
    assert position.realized_gain == Decimal("400")


def test_broker_casefolds_without_pooling_other_broker_lots():
    from portfolio_lens.calculations.accounting import replay_ledger

    result = replay_ledger(
        [
            record(broker="Fidelity", qty="5", price="100"),
            record(broker="Robinhood", qty="10", price="200"),
            record(day="2025-01-03", kind="SELL", broker="FIDELITY", qty="3", price="150"),
        ]
    )
    assert result.valid
    positions = {p.broker: p for p in result.positions}
    assert positions["Fidelity"].shares == Decimal("2")
    assert positions["Fidelity"].realized_gain == Decimal("150")
    assert positions["Robinhood"].shares == Decimal("10")
    assert positions["Robinhood"].cost_basis == Decimal("2000")


def test_oversell_reports_source_and_leaves_lots_intact():
    from portfolio_lens.calculations.accounting import replay_ledger

    result = replay_ledger(
        [
            record(qty="2"),
            record(broker="Robinhood", qty="20"),
            record(day="2025-01-03", kind="SELL", qty="3", row=17),
        ]
    )
    assert not result.valid
    (issue,) = result.issues
    assert (issue.ticker, issue.broker, issue.date, issue.source_row) == (
        "AAPL",
        "Fidelity",
        date(2025, 1, 3),
        17,
    )
    assert "available" in issue.message.lower()
    fidelity = next(p for p in result.positions if p.broker == "Fidelity")
    assert fidelity.shares == Decimal("2")
    assert fidelity.realized_gain == 0


def test_closed_positions_keep_realized_gain_and_no_average_cost():
    from portfolio_lens.calculations.accounting import replay_ledger

    result = replay_ledger(
        [record(qty="4"), record(day="2025-01-03", kind="SELL", qty="4", price="120")]
    )
    (position,) = result.positions
    assert (position.shares, position.cost_basis, position.realized_gain) == (
        Decimal("0"),
        Decimal("0"),
        Decimal("80"),
    )
    assert position.average_cost is None


def test_dates_are_sorted_but_same_day_uses_input_order():
    from portfolio_lens.calculations.accounting import replay_ledger

    buy = record(qty="4")
    sale = record(kind="SELL", qty="4", price="110")
    assert replay_ledger([buy, sale]).valid
    assert not replay_ledger([sale, buy]).valid
    result = replay_ledger([record(day="2025-01-03", kind="SELL", qty="4", price="110"), buy])
    assert result.valid
    assert result.positions[0].realized_gain == Decimal("40")


def test_small_fractional_lots_do_not_depend_on_ambient_decimal_precision():
    from portfolio_lens.calculations.accounting import replay_ledger

    with localcontext() as context:
        context.prec = 6
        result = replay_ledger(
            [
                record(qty="0.123456789123456789", price="100.123456789"),
                record(day="2025-01-03", kind="SELL", qty="0.023456789123456789", price="120"),
            ]
        )
    (position,) = result.positions
    assert position.shares == Decimal("0.100000000000000000")
    assert position.cost_basis == Decimal("10.012345678900000000000000000")


@pytest.mark.parametrize(
    "quantity,price", [("NaN", "100"), ("0", "100"), ("1", "Infinity"), ("1", "0")]
)
def test_invalid_transaction_numbers_become_explicit_issues(quantity, price):
    from portfolio_lens.calculations.accounting import replay_ledger

    result = replay_ledger([record(qty=quantity, price=price)])
    assert not result.valid
    assert result.issues[0].source_row == 2


def test_forward_reverse_splits_apply_strictly_after_trade_and_preserve_raw_values():
    from portfolio_lens.calculations.corporate_actions import normalize_records

    source = [
        record(qty="3", price="120", convention=AS_TRADED),
        record(day="2025-01-03", qty="2", price="30", convention=AS_TRADED),
    ]
    original = deepcopy(source)
    events = {
        "AAPL": (
            SplitEvent("AAPL", date(2025, 1, 3), Decimal("4")),
            SplitEvent("AAPL", date(2025, 1, 6), Decimal("0.5")),
        )
    }
    adjusted = normalize_records(source, events)
    assert source == original
    assert Decimal(adjusted[0]["quantity"]) == Decimal("6")
    assert Decimal(adjusted[0]["price"]) == Decimal("60")
    assert Decimal(adjusted[1]["quantity"]) == Decimal("1")
    assert Decimal(adjusted[1]["price"]) == Decimal("60")
    assert adjusted[0]["raw_quantity"] == "3"
    assert adjusted[0]["raw_price"] == "120"
    assert normalize_records(adjusted, events) == adjusted


def test_already_adjusted_rows_are_not_split_again():
    from portfolio_lens.calculations.corporate_actions import normalize_records

    result = normalize_records(
        [record(qty="15", price="85.5")],
        {
            "AAPL": (SplitEvent("AAPL", date(2025, 1, 3), Decimal("10")),),
        },
    )
    assert Decimal(result[0]["quantity"]) == Decimal("15")
    assert Decimal(result[0]["price"]) == Decimal("85.5")


def test_split_normalized_fifo_conserves_basis_and_sale_proceeds():
    from portfolio_lens.calculations.accounting import replay_ledger
    from portfolio_lens.calculations.corporate_actions import normalize_records

    rows = [
        record(qty="2", price="400", convention=AS_TRADED),
        record(day="2025-01-06", kind="SELL", qty="3", price="150", convention=AS_TRADED),
    ]
    result = replay_ledger(
        normalize_records(
            rows,
            {
                "AAPL": (SplitEvent("AAPL", date(2025, 1, 3), Decimal("4")),),
            },
        )
    )
    assert result.valid
    (position,) = result.positions
    assert (position.shares, position.cost_basis, position.realized_gain) == (
        Decimal("5"),
        Decimal("500"),
        Decimal("150"),
    )


def test_bundled_sample_default_already_adjusted_holdings_and_realized_total():
    from portfolio_lens.calculations.accounting import replay_ledger

    path = Path(__file__).parents[1] / "transactions.csv"
    with path.open(newline="") as source:
        records = list(DictReader(source))
    ledger = replay_ledger(records)
    assert ledger.valid
    assert {(p.ticker, p.broker): p.shares for p in ledger.positions} == {
        ("AAPL", "Robinhood"): Decimal("10"),
        ("VOO", "Vanguard"): Decimal("23"),
        ("NVDA", "Robinhood"): Decimal("55"),
        ("NVDA", "Fidelity"): Decimal("25"),
        ("MSFT", "Fidelity"): Decimal("9"),
        ("JNJ", "Fidelity"): Decimal("15"),
        ("TSLA", "Robinhood"): Decimal("4"),
        ("JPM", "Vanguard"): Decimal("12"),
    }
    assert sum(p.realized_gain for p in ledger.positions) == Decimal("1298.22")
