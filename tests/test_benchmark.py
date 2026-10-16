"""Same-dollar benchmark purchases and withdrawals on supplied sessions."""

from datetime import date
from decimal import Decimal

DAYS = (date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7))


def row(day, kind="BUY", qty="1", price="100"):
    return {
        "date": day,
        "transaction_type": kind,
        "quantity": qty,
        "price": price,
        "ticker": "AAPL",
        "broker": "A",
        "source_row": 2,
    }


def test_dated_buys_and_withdrawals_use_original_exact_cash_amounts():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [row("2025-01-03", qty="2"), row("2025-01-06", "SELL", price="120")],
        dict(zip(DAYS, map(Decimal, (100, 120, 150)))),
        DAYS,
        {},
    )
    assert [p.value for p in result.points] == [Decimal(200), Decimal(120), Decimal(150)]
    assert result.final_value == Decimal(150)
    assert result.complete and not result.infeasible


def test_infeasible_withdrawal_never_borrows_or_recovers_after_later_buy():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [
            row("2025-01-03"),
            row("2025-01-06", "SELL", price="110"),
            row("2025-01-07", price="1000"),
        ],
        {d: Decimal(100) for d in DAYS},
        DAYS,
        {},
    )
    assert [p.value for p in result.points] == [Decimal(100), None, None]
    assert result.final_value is None and result.infeasible
    assert not result.complete and result.issues
    assert "AAPL recorded 2025-01-06" in result.issues[0]
    assert "$110.00" in result.issues[0] and "$100.00" in result.issues[0]


def test_missing_execution_quote_permanently_blocks_simulation():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [row("2025-01-03")], {DAYS[1]: Decimal(110), DAYS[2]: Decimal(120)}, DAYS, {}
    )
    assert all(p.value is None for p in result.points)
    assert result.final_value is None and not result.complete
    assert not result.infeasible


def test_missing_mark_quote_is_gap_but_no_units_needs_no_quote():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [row("2025-01-03")], {DAYS[0]: Decimal(100), DAYS[2]: Decimal(120)}, DAYS, {}
    )
    assert [p.value for p in result.points] == [Decimal(100), None, Decimal(120)]
    assert result.final_value == Decimal(120) and not result.complete
    empty = simulate_benchmark([], {}, DAYS, {})
    assert [p.value for p in empty.points] == [Decimal(0)] * 3
    assert empty.complete


def test_weekend_mapping_uses_next_close_and_original_order():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    saturday, sunday = date(2025, 1, 4), date(2025, 1, 5)
    result = simulate_benchmark(
        [row("2025-01-05", "SELL", price="50"), row("2025-01-04")],
        {DAYS[1]: Decimal(200), DAYS[2]: Decimal(300)},
        DAYS,
        {saturday: DAYS[1], sunday: DAYS[1]},
    )
    assert [p.value for p in result.points] == [Decimal(0), Decimal(50), Decimal(75)]
    assert result.complete


def test_pending_trade_disables_terminal_benchmark_value():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [row("2025-01-03"), row("2025-01-08")], {d: Decimal(100) for d in DAYS}, DAYS, {}
    )
    assert [p.value for p in result.points] == [Decimal(100)] * 3
    assert result.final_value is None and not result.complete


def test_fractional_units_allow_exact_full_withdrawal_without_rounding_oversell():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [row("2025-01-03"), row("2025-01-06", "SELL")],
        {DAYS[0]: Decimal(3), DAYS[1]: Decimal(3)},
        DAYS,
        {},
    )
    assert [p.value for p in result.points] == [Decimal(100), Decimal(0), Decimal(0)]
    assert result.complete and not result.infeasible


def test_tiny_real_oversell_is_not_rounded_or_clamped():
    from portfolio_lens.calculations.benchmark import simulate_benchmark

    result = simulate_benchmark(
        [row("2025-01-03"), row("2025-01-06", "SELL", price="100.00000000000000000001")],
        {DAYS[0]: Decimal(3), DAYS[1]: Decimal(3)},
        DAYS,
        {},
    )
    assert result.infeasible and result.final_value is None
