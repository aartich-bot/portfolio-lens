"""Linked midpoint flow-adjusted returns distinguish money flows from losses."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_lens.calculations.history import HistoryPoint


def history(values):
    return tuple(
        HistoryPoint(
            date(2025, 1, 1) + timedelta(days=i), None if value is None else Decimal(str(value))
        )
        for i, value in enumerate(values)
    )


def test_known_drawdown_is_peak_to_trough_and_first_point_has_no_return():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    result = analyze_drawdown(history([100, 120, 90, 108]), {})
    assert result.complete and result.max_drawdown == pytest.approx(0.25)
    assert [point.index for point in result.points] == pytest.approx([100, 120, 90, 108])
    assert [point.drawdown for point in result.points] == pytest.approx([0, 0, 0.25, 0.10])
    assert result.points[0].daily_return is None
    assert result.annual_return is None and result.annual_volatility is None
    assert "30" in result.reason


def test_purchase_and_sale_cash_flows_do_not_create_returns():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history([100, 200, 150])
    result = analyze_drawdown(
        points,
        {points[0].day: Decimal(80), points[1].day: Decimal(100), points[2].day: Decimal(-50)},
    )
    assert [point.index for point in result.points] == [100, 100, 100]
    assert result.max_drawdown == 0


def test_midpoint_flow_formula_uses_capital_exposure():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history([100, 220])
    result = analyze_drawdown(points, {points[1].day: Decimal(100)})
    assert result.points[1].daily_return == pytest.approx(20 / 150)
    assert result.points[1].index == pytest.approx(113.33333333333333)


@pytest.mark.parametrize(
    "values,flows,complete",
    [
        ([100, None, 120], {}, True),
        ([100, 110], {}, False),
        ([100, -1], {}, True),
        ([100, 0], {1: -250}, True),
        ([100, 1], {1: 1000}, True),
        ([100, "NaN"], {}, True),
    ],
)
def test_invalid_or_incomplete_history_has_no_connected_curve(values, flows, complete):
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history(values)
    result = analyze_drawdown(
        points, {points[i].day: Decimal(n) for i, n in flows.items()}, complete=complete
    )
    assert not result.complete and result.points == ()
    assert result.max_drawdown is None and result.annual_return is None
    assert result.annual_volatility is None and result.reason


def test_empty_and_all_zero_history_do_not_invent_a_starting_investment():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    for points in ((), history([0, 0])):
        result = analyze_drawdown(points, {})
        assert not result.complete and result.max_drawdown is None
        assert result.points == () and result.reason


def test_leading_zero_and_uninvested_days_do_not_count_as_return_observations():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history([0, 100, 0, 0, 100, 110])
    result = analyze_drawdown(
        points,
        {points[1].day: Decimal(100), points[2].day: Decimal(-100), points[4].day: Decimal(100)},
    )
    assert result.complete and result.points[0].day == points[1].day
    assert [point.index for point in result.points] == pytest.approx([100, 100, 100, 100, 110])
    assert result.points[2].daily_return is None


def test_annual_estimates_require_thirty_return_intervals():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    assert analyze_drawdown(history([100] * 30), {}).annual_return is None
    result = analyze_drawdown(history([100] * 31), {})
    assert result.annual_return == 0 and result.annual_volatility == 0
    assert result.reason is None


def test_geometric_annual_return_and_sample_daily_volatility():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history([Decimal(100) * Decimal("1.001") ** i for i in range(31)])
    result = analyze_drawdown(points, {})
    assert result.annual_return == pytest.approx(0.2864340443761522)
    assert result.annual_volatility == pytest.approx(0, abs=1e-12)


def test_out_of_order_or_duplicate_sessions_are_unavailable():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history([100, 110])
    assert not analyze_drawdown(tuple(reversed(points)), {}).complete
    assert not analyze_drawdown((points[0], points[0]), {}).complete


def test_sample_volatility_uses_varying_flow_adjusted_daily_returns():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    values = [Decimal(100)]
    for i in range(30):
        values.append(values[-1] * (Decimal("1.1") if i % 2 == 0 else Decimal(".9")))
    result = analyze_drawdown(history(values), {})
    assert result.annual_return == pytest.approx(-0.7181393044595366)
    assert result.annual_volatility == pytest.approx(1.614588663320828)


def test_invalid_cash_flow_cannot_be_ignored():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history([100, 100])
    result = analyze_drawdown(points, {points[1].day: Decimal("NaN")})
    assert not result.complete and result.points == () and result.reason


def test_total_loss_is_one_hundred_percent_drawdown():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    result = analyze_drawdown(history([100, 0, 0]), {})
    assert result.complete and result.max_drawdown == 1
    assert result.points[1].daily_return == -1


def test_annual_overflow_keeps_valid_drawdown_but_no_annual_estimates():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    result = analyze_drawdown(history([Decimal(100) * Decimal(1000) ** i for i in range(31)]), {})
    assert result.complete and result.max_drawdown == 0
    assert result.annual_return is None and result.annual_volatility is None
    assert result.reason


def test_numeric_underflow_is_unavailable_instead_of_a_fabricated_total_loss():
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    result = analyze_drawdown(history([Decimal("1e300"), 1]), {})
    assert not result.complete and result.points == () and result.reason


@pytest.mark.parametrize(
    "values,flow",
    [
        ([100, 100], Decimal("1e1000000")),
        ([Decimal("1e1000000"), Decimal("1e1000000")], Decimal(0)),
    ],
)
def test_finite_decimal_arithmetic_overflow_returns_unavailable(values, flow):
    from portfolio_lens.calculations.drawdown import analyze_drawdown

    points = history(values)
    result = analyze_drawdown(points, {points[1].day: flow})
    assert not result.complete and result.points == ()
    assert result.max_drawdown is None and result.annual_return is None
    assert result.annual_volatility is None and result.reason
