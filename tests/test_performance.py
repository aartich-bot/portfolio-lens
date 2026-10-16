"""Independent cash-flow fixtures and unavailable-rate safeguards."""

from datetime import date
from decimal import Decimal, localcontext

import pytest


def row(day, kind="BUY", qty="1", price="1000"):
    return {"date": day, "transaction_type": kind, "quantity": qty, "price": price}


def flows(*amounts):
    return [(date(2021 + i, 1, 1), Decimal(str(amount))) for i, amount in enumerate(amounts)]


def test_one_year_cash_flow_is_ten_percent():
    from portfolio_lens.calculations.performance import solve_xirr

    result = solve_xirr(flows(-1000, 1100))
    assert result.reason is None
    assert result.rate == pytest.approx(0.1, abs=1e-10)


def test_irregular_dates_use_actual_365_basis():
    from portfolio_lens.calculations.performance import solve_xirr

    result = solve_xirr(
        [
            (date(2008, 1, 1), Decimal(-10000)),
            (date(2008, 3, 1), Decimal(2750)),
            (date(2008, 10, 30), Decimal(4250)),
            (date(2009, 2, 15), Decimal(3250)),
            (date(2009, 4, 1), Decimal(2750)),
        ]
    )
    assert result.rate == pytest.approx(0.3733625335, abs=1e-8)


@pytest.mark.parametrize(
    "cashflows",
    [
        [],
        flows(10, 20),
        flows(-100, 0),
        flows(-100, "NaN"),
        flows(-100, "Infinity"),
        [(date(2021, 1, 1), Decimal(-100)), (date(2021, 1, 1), Decimal(100))],
        flows(-100, 50, -100),
    ],
)
def test_invalid_or_rootless_flows_have_reason(cashflows):
    from portfolio_lens.calculations.performance import solve_xirr

    result = solve_xirr(cashflows)
    assert result.rate is None
    assert result.reason


def test_multiple_positive_roots_are_ambiguous():
    from portfolio_lens.calculations.performance import solve_xirr

    result = solve_xirr(flows(-100, 230, -132))  # 10% and 20%.
    assert result.rate is None
    assert "ambig" in result.reason.lower() or "multiple" in result.reason.lower()


def test_alternating_cashflows_with_one_real_root_are_allowed():
    from portfolio_lens.calculations.performance import solve_xirr

    # In q=1/(1+r): (q-1)*(q*q+1) => unique q=1, despite three sign changes.
    result = solve_xirr(flows(-100, 100, -100, 100))
    assert result.reason is None
    assert result.rate == pytest.approx(0, abs=1e-10)


def test_negative_return_and_same_date_aggregation():
    from portfolio_lens.calculations.performance import solve_xirr

    result = solve_xirr(flows(-1000, 500) + [(date(2021, 1, 1), Decimal(-1000))])
    assert result.rate == pytest.approx(-0.75, abs=1e-10)


def test_totals_use_all_original_amounts_and_simple_not_annualized_return():
    from portfolio_lens.calculations.performance import calculate_returns

    result = calculate_returns(
        [
            row("2021-01-01"),
            row("2021-07-01", "SELL", price="200"),
        ],
        Decimal(1000),
        date(2022, 1, 1),
    )
    assert (
        result.invested,
        result.sold,
        result.current_value,
        result.total_gain,
        result.total_return,
    ) == (Decimal(1000), Decimal(200), Decimal(1000), Decimal(200), Decimal("0.2"))
    assert result.xirr.rate is not None


def test_pending_trades_retain_totals_but_disable_lifetime_valuation():
    from portfolio_lens.calculations.performance import calculate_returns

    result = calculate_returns(
        [row("2021-01-01"), row("2023-01-01")], Decimal(1100), date(2022, 1, 1)
    )
    assert result.invested == Decimal(2000)
    assert result.current_value is None
    assert result.total_return is None
    assert result.xirr.rate is None
    assert "after" in result.xirr.reason.lower()


def test_missing_terminal_disables_derived_metrics_but_zero_terminal_is_real():
    from portfolio_lens.calculations.performance import calculate_returns

    rows = [row("2021-01-01"), row("2022-01-01", "SELL", price="1100")]
    missing = calculate_returns(rows, None, date(2022, 1, 1))
    closed = calculate_returns(rows, Decimal(0), date(2022, 1, 1))
    assert missing.total_gain is None and missing.xirr.rate is None
    assert closed.total_gain == Decimal(100)
    assert closed.xirr.rate == pytest.approx(0.1, abs=1e-10)


def test_trade_amount_multiplication_does_not_use_ambient_precision():
    from portfolio_lens.calculations.performance import calculate_returns

    with localcontext() as context:
        context.prec = 6
        result = calculate_returns(
            [row("2021-01-01", qty="1.123456789", price="100")], Decimal(120), date(2022, 1, 1)
        )
    assert result.invested == Decimal("112.345678900")


@pytest.mark.parametrize(
    "amounts", [(-100, 350, -350, 100), (-100, 200, -100), (-100, 1001000, -10000000)]
)
def test_negative_distant_and_repeated_roots_do_not_pick_an_arbitrary_rate(amounts):
    from portfolio_lens.calculations.performance import solve_xirr

    # Respectively roots -50%/0%/100%; repeated 0%; and 900%/999900%.
    result = solve_xirr(flows(*amounts))
    assert result.rate is None
    assert "ambig" in result.reason.lower() or "multiple" in result.reason.lower()


def test_nonconventional_irregular_dates_have_unique_rate_with_known_npv():
    from portfolio_lens.calculations.performance import solve_xirr

    # These amounts sum to zero, and the modest early withdrawal does not create
    # a second root: -100 + 10*q^.25 - 10*q^.75 + 100*q = 0 only at q=1.
    result = solve_xirr(
        [
            (date(2021, 1, 1), Decimal(-100)),
            (date(2021, 4, 2), Decimal(10)),
            (date(2021, 10, 1), Decimal(-10)),
            (date(2022, 1, 1), Decimal(100)),
        ]
    )
    assert result.rate == pytest.approx(0, abs=1e-10)


def test_extreme_cashflow_magnitudes_do_not_create_spurious_root():
    from portfolio_lens.calculations.performance import solve_xirr

    # All positive q satisfy 1e-180 - 2e-180*q + q*q > 0.
    result = solve_xirr(flows("1e-180", "-2e-180", 1))
    assert result.rate is None
    assert result.reason


def test_ten_thousand_cashflows_aggregate_same_dates_without_losing_amounts():
    from portfolio_lens.calculations.performance import solve_xirr

    result = solve_xirr(
        [(date(2021, 1, 1), Decimal(-1))] * 5000 + [(date(2022, 1, 1), Decimal("1.1"))] * 5000
    )
    assert result.rate == pytest.approx(0.1, abs=1e-10)
