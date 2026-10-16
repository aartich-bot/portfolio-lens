"""Present historical performance and explain the matched S&P 500 comparison."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from portfolio_lens.services.performance import PerformanceAnalysis, analyze_performance
from portfolio_lens.ui.styles import GREEN, INK, PURPLE, RED


def _money(value: Decimal | None) -> str:
    return "Unavailable" if value is None else f"${value:,.2f}"


def _percent(value: Decimal | float | None) -> str:
    return "Unavailable" if value is None else f"{value * 100:+.2f}%"


def _cards(analysis: PerformanceAnalysis) -> None:
    totals = analysis.totals
    cards = [
        ("Total invested", _money(totals.invested), "All the money spent buying shares."),
        ("Total sold", _money(totals.sold), "All the money received from selling shares."),
        (
            "Current value",
            _money(totals.current_value),
            "What your remaining shares were worth at the latest completed market close.",
        ),
        (
            "XIRR",
            _percent(totals.xirr.rate),
            "Your yearly return, accounting for how much you invested and when.",
        ),
        (
            "Total return",
            _percent(totals.total_return),
            f"{_money(totals.total_gain)} profit or loss ÷ all purchases; not a yearly rate.",
        ),
    ]
    for column, (label, value, explanation) in zip(st.columns(5), cards, strict=True):
        with column:
            st.metric(label, value, border=True)
            st.caption(explanation)
    if totals.xirr.reason:
        st.info(f"XIRR unavailable: {totals.xirr.reason}")


def _verdict(analysis: PerformanceAnalysis) -> None:
    spread = analysis.spread  # Service expresses the annualized difference in percentage points.
    if spread is None:
        st.info(
            "We can’t make a reliable S&P 500 comparison yet. Both histories need complete "
            "prices, feasible sales, and a valid yearly return. See the notices below."
        )
    elif abs(spread) < 1e-8:
        st.info("You’re keeping pace with the S&P 500 on a yearly, money-weighted basis.")
    else:
        difference = f"{abs(spread):.2f}" if abs(spread) >= 0.01 else "less than 0.01"
        if spread > 0:
            st.success(f"You’re ahead of the S&P 500 by {difference} percentage points a year.")
        else:
            st.warning(f"You’re behind the S&P 500 by {difference} percentage points a year.")
    st.caption(
        "S&P 500 · A widely used index of large US companies. "
        "Percentage points · The difference between two percentage returns."
    )
    if analysis.benchmark_returns.xirr.rate is not None:
        st.caption(
            f"Your XIRR: {_percent(analysis.totals.xirr.rate)} · "
            f"Simulated S&P 500 XIRR: {_percent(analysis.benchmark_returns.xirr.rate)} per year · "
            f"Simulated ending value: {_money(analysis.benchmark.final_value)}."
        )
    elif analysis.benchmark_returns.xirr.reason:
        st.caption(f"Benchmark XIRR unavailable: {analysis.benchmark_returns.xirr.reason}")


def _notices(analysis: PerformanceAnalysis) -> None:
    for error in analysis.errors:
        st.error(error)
    notices = list(analysis.warnings)
    if not analysis.portfolio.complete or not analysis.benchmark.complete:
        notices.insert(
            0,
            "Incomplete data: unavailable daily values remain gaps, never zero. "
            "The comparison verdict is withheld until both histories can be compared reliably.",
        )
    for mapping in analysis.mappings:
        notices.append(
            f"{mapping.ticker}: recorded {mapping.original.isoformat()} → "
            f"next trading day {mapping.effective.isoformat()} for the chart and benchmark. "
            "The saved date and XIRR cash-flow date stay unchanged."
        )
    unique = list(dict.fromkeys(notices))
    for notice in unique[:5]:
        st.warning(notice)
    if len(unique) > 5:
        with st.expander(f"All {len(unique)} data and date notices"):
            for notice in unique:
                st.warning(notice)


def _layout(figure: go.Figure, height: int) -> None:
    figure.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Arial, sans-serif", "color": INK},
        margin={"l": 10, "r": 20, "t": 15, "b": 10},
        legend={"orientation": "h", "y": 1.13, "x": 0},
        yaxis={
            "tickprefix": "$",
            "tickformat": ",.0f",
            "gridcolor": "#EEE9F8",
            "zerolinecolor": "#C4B5FD",
        },
        xaxis={"showgrid": False},
    )


def _growth_chart(analysis: PerformanceAnalysis) -> None:
    with st.container(border=True, key="performance_growth"):
        st.subheader("How your holdings grew")
        st.caption(
            "Portfolio value · The dollar value of shares held each day. "
            "Buys add money and sells remove it, so this line is not a percentage-return chart."
        )
        left, right = st.columns([2, 1])
        with left:
            period = (
                st.segmented_control(
                    "Chart range", ["1M", "6M", "1Y", "All"], default="All", key="performance_range"
                )
                or "All"
            )
        with right:
            show_benchmark = st.checkbox("Show S&P 500 benchmark", value=True)
        st.caption(
            "The range changes this chart only. Cards and comparison always use your full history."
        )
        months = {"1M": 1, "6M": 6, "1Y": 12}.get(period)
        first = (
            (pd.Timestamp(analysis.as_of) - pd.DateOffset(months=months)).date()
            if months
            else date.min
        )
        figure = go.Figure()
        series = [("Your portfolio", analysis.portfolio.points, PURPLE)]
        if show_benchmark:
            series.append(("Simulated S&P 500", analysis.benchmark.points, "#2B9389"))
        for name, points, color in series:
            visible = [point for point in points if point.day >= first]
            figure.add_trace(
                go.Scatter(
                    x=[point.day.isoformat() for point in visible],
                    y=[None if point.value is None else float(point.value) for point in visible],
                    name=name,
                    mode="lines",
                    line={"color": color, "width": 3},
                    connectgaps=False,
                    hovertemplate="%{x}<br>%{y:$,.2f}<extra>%{fullData.name}</extra>",
                )
            )
        for kind, label, symbol, color in [
            ("BUY", "Buys", "triangle-up", GREEN),
            ("SELL", "Sells", "triangle-down", RED),
        ]:
            markers = [
                marker
                for marker in analysis.portfolio.markers
                if marker.kind == kind
                and marker.effective_date >= first
                and marker.value is not None
            ]
            figure.add_trace(
                go.Scatter(
                    x=[marker.effective_date.isoformat() for marker in markers],
                    y=[float(marker.value) for marker in markers],
                    mode="markers",
                    name=label,
                    marker={
                        "symbol": symbol,
                        "color": color,
                        "size": 11,
                        "line": {"color": "white", "width": 1},
                    },
                    customdata=[
                        [marker.ticker, marker.original_date.isoformat(), float(marker.amount)]
                        for marker in markers
                    ],
                    hovertemplate=(
                        "%{customdata[0]} · %{customdata[2]:$,.2f}<br>Recorded: %{customdata[1]}"
                        "<br>Chart date: %{x}<br>Portfolio: %{y:$,.2f}<extra>%{fullData.name}</extra>"
                    ),
                )
            )
        _layout(figure, 430)
        figure.update_layout(hovermode="x unified", yaxis_title="Holdings value (USD)")
        st.plotly_chart(figure, width="stretch", key="performance_curve")


def _gains_chart(analysis: PerformanceAnalysis) -> None:
    with st.container(border=True, key="performance_gains"):
        st.subheader("Where your gains come from")
        st.caption(
            "Realized gain/loss · Sale proceeds minus the cost of shares sold. "
            "Unrealized gain/loss · The gain or loss on shares you still hold."
        )
        if not analysis.gains:
            st.info("Gains by ticker are unavailable until the transaction history can be valued.")
            return
        missing = [gain.ticker for gain in analysis.gains if gain.unrealized is None]
        if missing:
            st.warning(
                "Unrealized gains are unavailable for "
                + ", ".join(missing)
                + "; those bars are omitted."
            )
        figure = go.Figure()
        for label, field, color in [
            ("Realized", "realized", PURPLE),
            ("Unrealized", "unrealized", "#A78BFA"),
        ]:
            available = [gain for gain in analysis.gains if getattr(gain, field) is not None]
            figure.add_trace(
                go.Bar(
                    x=[gain.ticker for gain in available],
                    y=[float(getattr(gain, field)) for gain in available],
                    name=label,
                    marker_color=color,
                    hovertemplate="%{x}<br>%{y:$,.2f}<extra>%{fullData.name}</extra>",
                )
            )
        _layout(figure, 310)
        figure.update_layout(barmode="group", yaxis_title="Gain / loss (USD)")
        st.plotly_chart(figure, width="stretch", key="performance_gains_chart")
        st.caption(
            "Includes fully sold tickers and all brokers. FIFO is an app convention, not tax advice."
        )


def _assumptions(analysis: PerformanceAnalysis) -> None:
    with st.expander("Assumptions"):
        st.markdown(
            "- **Money-weighted return (XIRR):** the annual rate that balances your dated cash flows. "
            "Buys are money out, sells are money in, and remaining holdings are a final inflow. "
            "It uses actual elapsed days divided by 365; short histories can produce large yearly rates. "
            "No number is shown if there is no unique, numerically reliable rate.\n"
            "- **Cash-flow dates:** both XIRRs use your original recorded dates. Non-trading dates "
            "map forward only for daily holdings and benchmark trades; saved rows stay unchanged.\n"
            "- **Benchmark method:** each purchase buys fractional units of the S&P 500 index "
            "(`^GSPC`) at that day's close; each sale withdraws the same dollar amount. "
            "A sale exceeding the simulated holdings stops the comparison. This is a hypothetical "
            "index simulation, not a tradable fund.\n"
            "- **Dividends excluded:** neither side reinvests or adds dividend payments. "
            "Fees and taxes are also excluded because they are not in the transaction file.\n"
            "- **FIFO:** first-in, first-out means the earliest purchased shares are sold first, "
            "separately in each broker account. This is an app convention, not tax advice.\n"
            "- **Total return:** (current value + total sold − total invested) ÷ total invested. "
            "This simple gain percentage is not annualized and does not account for timing.\n"
            "- **Price basis:** yfinance daily Close prices are adjusted for stock splits, "
            "not dividends. The sample is already split-adjusted; other raw trades are adjusted "
            "only in calculation copies. Both lines end at the same completed US market session.\n"
            "- **Missing prices and cache:** gaps stay unavailable. A complete ending value may "
            "still support your XIRR despite earlier gaps, but the comparison verdict waits for "
            "complete histories. Cached prices carry their download timestamps and refresh warnings."
        )
        if analysis.mappings:
            st.dataframe(
                [
                    {
                        "Ticker": item.ticker,
                        "Recorded date": item.original.isoformat(),
                        "Chart / benchmark date": item.effective.isoformat(),
                    }
                    for item in analysis.mappings
                ],
                hide_index=True,
                width="stretch",
            )


def render(records: list[dict], db_path: str | Path) -> None:
    st.markdown('<div class="eyebrow">YOUR PROGRESS</div>', unsafe_allow_html=True)
    title, refresh = st.columns([4, 1])
    with title:
        st.title("Performance")
        st.write("Am I beating the S&P 500? See how your investing has worked out over time.")
    if not records:
        st.info(
            "Start in Add Transactions and load the sample or save your own trades to see performance."
        )
        return
    with refresh:
        force = st.button("Refresh history", icon=":material/refresh:", width="stretch")
    with st.spinner("Building your portfolio and matched S&P 500 history…"):
        analysis = analyze_performance(records, db_path, force=force)
    st.caption(
        f"Values as of {analysis.as_of.isoformat()} market close · All brokers · USD · "
        "Dividends excluded on both sides."
    )
    timestamps = sorted((analysis.market.fetched_at or {}).values())
    if timestamps:

        def stamp(value):
            return value.astimezone(ZoneInfo("America/Los_Angeles")).strftime(
                "%Y-%m-%d %I:%M %p %Z"
            )

        downloaded = stamp(timestamps[0])
        if timestamps[-1] != timestamps[0]:
            downloaded += " to " + stamp(timestamps[-1])
        st.caption(
            f"Price history last downloaded: {downloaded}. Cached daily; refresh to retry missing data."
        )
    _cards(analysis)
    _verdict(analysis)
    _notices(analysis)
    _growth_chart(analysis)
    _gains_chart(analysis)
    _assumptions(analysis)
