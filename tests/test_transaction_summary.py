"""Read-only dashboard aggregation, independent of Streamlit and persistence."""

from copy import deepcopy
from decimal import Decimal

from portfolio_lens.transaction_summary import summarize_transactions, transaction_rows


def trade(day="2026-01-02", kind="BUY", quantity="2", price="10", broker="Fidelity"):
    return {
        "ticker": "AAPL",
        "date": day,
        "transaction_type": kind,
        "quantity": quantity,
        "price": price,
        "broker": broker,
    }


def test_empty_history_has_zero_totals_and_empty_series():
    assert transaction_rows([]) == []
    assert summarize_transactions([]) == {
        "count": 0,
        "bought": Decimal(0),
        "sold": Decimal(0),
        "net_invested": Decimal(0),
        "daily": [],
        "monthly": [],
        "brokers": [],
    }


def test_exact_fractional_values_weekend_flags_and_no_input_mutation():
    records = [
        trade(
            "2026-04-19", quantity="999999999999.123456789123", price="999999999999.987654321987"
        ),
        trade(),
    ]
    original = deepcopy(records)
    rows = transaction_rows(records)
    assert rows[0]["total_value"] == Decimal("999999999999111111111110.010821520245968601347401")
    assert rows[0]["weekend"] is True
    assert rows[1]["weekend"] is False
    rows[0]["ticker"] = "CHANGED"
    assert records == original


def test_daily_series_orders_trades_and_combines_same_day_buys_and_sells():
    records = [
        trade("2026-01-04", "SELL", "5", "10"),
        trade("2026-01-02", "BUY", "0.1", "0.2"),
        trade("2026-01-04", "BUY", "2", "10"),
    ]
    original = deepcopy(records)
    summary = summarize_transactions(records)
    assert summary["count"] == 3
    assert summary["bought"] == Decimal("20.02")
    assert summary["sold"] == Decimal("50")
    assert summary["net_invested"] == Decimal("-29.98")
    assert summary["daily"] == [
        {"date": "2026-01-02", "net_invested": Decimal("0.02")},
        {"date": "2026-01-04", "net_invested": Decimal("-29.98")},
    ]
    assert records == original


def test_monthly_series_fills_gaps_and_crosses_year_boundary():
    summary = summarize_transactions(
        [trade("2026-02-01", "SELL", "1", "5"), trade("2025-12-01"), trade("2025-12-30")]
    )
    assert summary["monthly"] == [
        {"month": "2025-12", "BUY": Decimal("40"), "SELL": Decimal(0)},
        {"month": "2026-01", "BUY": Decimal(0), "SELL": Decimal(0)},
        {"month": "2026-02", "BUY": Decimal(0), "SELL": Decimal("5")},
    ]


def test_broker_activity_counts_trades_and_groups_names_case_insensitively():
    summary = summarize_transactions(
        [
            trade(broker="Fidelity"),
            trade(broker="fidelity"),
            trade(broker="Vanguard", quantity="1000"),
        ]
    )
    assert summary["brokers"] == [
        {"broker": "Fidelity", "count": 2},
        {"broker": "Vanguard", "count": 1},
    ]


def test_sell_only_history_has_negative_net_investment_and_zero_buys():
    summary = summarize_transactions([trade(kind="SELL", quantity="0.125", price="8")])
    assert summary["bought"] == 0
    assert summary["sold"] == Decimal("1.000")
    assert summary["net_invested"] == Decimal("-1.000")
    assert summary["monthly"][0]["BUY"] == 0
