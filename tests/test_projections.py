"""Deterministic scenarios compound holdings and end-of-month contributions."""

from decimal import Decimal

import pytest


def test_zero_growth_adds_end_of_month_contributions():
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(Decimal(1000), 100, 1, 0, 0)
    assert result.reason is None and result.contributed == 1200
    assert [scenario.name for scenario in result.scenarios] == ["lower", "base", "higher"]
    for scenario in result.scenarios:
        assert scenario.annual_rate == 0
        assert scenario.points[0].month == 0 and scenario.points[0].balance == 1000
        assert scenario.points[1].balance == 1100
        assert scenario.points[-1].month == 12 and scenario.points[-1].balance == 2200


def test_monthly_compounding_and_contribution_timing():
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(1000, 100, 1, 0.12, 0)
    base = result.scenarios[1]
    assert base.points[-1].balance == pytest.approx(2384.6497908353094)
    assert project_outcomes(1000, 0, 1, 0.12, 0).scenarios[1].points[-1].balance == pytest.approx(
        1120
    )


def test_volatility_spreads_log_growth_with_rates_above_total_loss():
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(1000, 0, 1, 0.1, 0.2)
    lower, base, higher = result.scenarios
    assert lower.annual_rate == pytest.approx(-0.0993961716142199)
    assert base.annual_rate == pytest.approx(0.1)
    assert higher.annual_rate == pytest.approx(0.3435430339761869)
    assert lower.points[-1].balance < base.points[-1].balance < higher.points[-1].balance
    assert lower.annual_rate > -1


def test_sustained_contributions_and_longer_horizon_increase_balance():
    from portfolio_lens.calculations.projections import project_outcomes

    short = project_outcomes(0, 2000, 1, 0, 0)
    long = project_outcomes(0, 2000, 30, 0, 0)
    assert short.scenarios[1].points[-1].balance == 24000
    assert long.scenarios[1].points[-1].balance == 720000


@pytest.mark.parametrize(
    "current,monthly,years,rate,vol",
    [
        (100, 100, 1, None, None),
        (100, 100, 1, -1, 0.1),
        (100, 100, 1, float("nan"), 0.1),
        (100, 100, 1, 0.1, -1),
        (None, 100, 1, 0.1, 0.1),
        (-1, 100, 1, 0.1, 0.1),
        (100, -1, 1, 0.1, 0.1),
        (100, 2001, 1, 0.1, 0.1),
        (100, 100, 0, 0.1, 0.1),
        (100, 100, 31, 0.1, 0.1),
        (100, 100, 1.5, 0.1, 0.1),
        (100, 100, 1, 0.1, 1000),
        (1e308, 100, 30, 100, 1),
        (100, 100, 1, float("inf"), 0.1),
    ],
)
def test_missing_invalid_or_overflow_inputs_are_explanatory_unavailable(
    current, monthly, years, rate, vol
):
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(current, monthly, years, rate, vol)
    assert result.scenarios == () and result.reason


def test_negative_return_scenario_preserves_nonnegative_balances():
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(1000, 0, 30, -0.9, 0)
    assert result.reason is None
    assert result.scenarios[1].points[-1].balance == pytest.approx(1e-27)


def test_underflow_does_not_silently_turn_a_positive_balance_into_zero():
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(1e-300, 0, 30, -0.99, 0)
    assert result.scenarios == () and result.reason


def test_decimal_input_underflow_cannot_be_reported_as_a_zero_start():
    from portfolio_lens.calculations.projections import project_outcomes

    result = project_outcomes(Decimal("1e-999"), 0, 1, 0, 0)
    assert result.scenarios == () and result.reason
