"""Explain current holdings using the shared purple design and calculated results."""

from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from portfolio_lens.services.analysis import analyze_portfolio
from portfolio_lens.ui.styles import CHART_COLORS, INK


def _money(value: Decimal | None) -> str:
    return "Unavailable" if value is None else f"${value:,.2f}"


def _percent(value: Decimal | None) -> str:
    return "Unavailable" if value is None else f"{value * 100:+.2f}%"


def _cards(valuation) -> None:
    cards = [
        (
            "Current value",
            _money(valuation.current_value),
            "What your remaining shares are worth at the quoted prices.",
        ),
        (
            "Cost basis",
            _money(valuation.cost_basis),
            "What you paid for the shares you still hold.",
        ),
        (
            "Unrealized gain/loss",
            _money(valuation.unrealized_gain),
            f"{_percent(valuation.unrealized_pct)} · The gain or loss on shares you still hold.",
        ),
        (
            "Realized gain/loss",
            _money(valuation.realized_gain),
            "Sale proceeds minus the cost of the shares sold.",
        ),
        (
            "Today's change",
            _money(valuation.today_change),
            f"{_percent(valuation.today_change_pct)} · Latest session’s price move on your current shares.",
        ),
    ]
    for column, (label, amount, explanation) in zip(st.columns(5), cards, strict=True):
        with column:
            st.metric(label, amount, border=True)
            st.caption(explanation)


def _allocation(valuation) -> None:
    with st.container(border=True, key="portfolio_allocation"):
        heading, badge = st.columns([2, 1])
        with heading:
            st.subheader("Where your money sits")
            st.caption("Allocation · How your current holdings value is divided.")
        with badge:
            rating = valuation.concentration or "Unavailable"
            style = rating.lower() if valuation.concentration else "unavailable"
            st.markdown(
                f'<div class="risk-badge {style}">{escape(rating)} concentration risk</div>',
                unsafe_allow_html=True,
            )
        st.caption(
            "Concentration · How much of your portfolio rests in just a few holdings; "
            "this badge does not measure every kind of investment risk."
        )
        group = st.radio("Allocation view", ["By Stock", "By Sector", "By Broker"], horizontal=True)
        st.caption(
            "Stock · One company's shares. Sector · A business category. "
            "Broker · The account provider holding your shares."
        )
        if not valuation.complete:
            st.info(
                "Allocation and concentration are unavailable until all holdings have comparable prices."
            )
            return
        allocations = valuation.allocations.get(
            {"By Stock": "stock", "By Sector": "sector", "By Broker": "broker"}[group], ()
        )
        if not allocations:
            st.info(
                "You have no open holdings to allocate. Realized gains above still include completed sales."
            )
            return
        figure = go.Figure(
            go.Pie(
                labels=[item.name for item in allocations],
                values=[float(item.value) for item in allocations],
                hole=0.72,
                sort=False,
                marker={"colors": CHART_COLORS, "line": {"color": "white", "width": 3}},
                textinfo="percent",
                textposition="outside",
                hovertemplate="%{label}<br>$%{value:,.2f} · %{percent}<extra></extra>",
            )
        )
        figure.update_layout(
            height=340,
            paper_bgcolor="rgba(0,0,0,0)",
            font={"color": INK, "family": "Arial, sans-serif"},
            margin={"l": 35, "r": 35, "t": 20, "b": 20},
            legend={"orientation": "h", "y": -0.12, "x": 0},
            annotations=[
                {
                    "text": "Your holdings",
                    "x": 0.5,
                    "y": 0.5,
                    "showarrow": False,
                    "font": {"size": 17},
                }
            ],
        )
        st.plotly_chart(figure, width="stretch", theme=None, config={"displayModeBar": False})
        st.caption(
            "ETF · A fund that holds a basket of investments; grouped as ETF rather than a single sector. "
            "Unknown sectors stay labeled Unknown. The seven sample tickers have a built-in sector fallback."
        )


def _holdings(valuation) -> None:
    with st.container(border=True, key="portfolio_holdings"):
        st.subheader("Your holdings")
        st.caption(
            "Holding · An investment you still own. Ticker · Its trading symbol. Shares · How many you own."
        )
        st.caption(
            "Avg cost · Remaining cost basis per share. Price · Latest completed-session close. "
            "Value · Shares multiplied by price. Gain/loss % · Change relative to remaining cost basis. "
            "Weight · Share of total holdings value."
        )
        rows = [
            {
                "Ticker": holding.ticker,
                "Shares": float(holding.shares),
                "Avg cost ($)": float(holding.average_cost),
                "Price ($)": float(holding.price) if holding.price is not None else None,
                "Value ($)": float(holding.market_value)
                if holding.market_value is not None
                else None,
                "Gain/loss (%)": float(holding.unrealized_pct * 100)
                if holding.unrealized_pct is not None
                else None,
                "Weight (%)": float(holding.weight * 100) if holding.weight is not None else None,
            }
            for holding in valuation.holdings
        ]
        if not rows:
            st.info("No remaining shares in this selection.")
            return
        st.dataframe(
            pd.DataFrame(rows),
            hide_index=True,
            width="stretch",
            column_config={
                "Shares": st.column_config.NumberColumn(format="%.12g"),
                "Avg cost ($)": st.column_config.NumberColumn(format="$%.2f"),
                "Price ($)": st.column_config.NumberColumn(format="$%.2f"),
                "Value ($)": st.column_config.NumberColumn(format="$%.2f"),
                "Gain/loss (%)": st.column_config.NumberColumn(format="%.2f%%"),
                "Weight (%)": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%.1f%%"
                ),
            },
        )
        st.caption("A blank price, value, or percentage means it is unavailable, not zero.")


def render(records: list[dict], db_path: str | Path) -> None:
    st.markdown('<div class="eyebrow">WHAT YOU OWN</div>', unsafe_allow_html=True)
    st.title("Your portfolio, in perspective")
    st.caption("Your shares across brokers, brought together in one clear view.")
    if not records:
        st.info("Add or load transactions in Add Transactions to see your portfolio here.")
        return
    broker_labels = {}
    for row in records:
        broker_labels.setdefault(row["broker"].casefold(), row["broker"])
    filters, refresh = st.columns([3, 1], vertical_alignment="bottom")
    brokers = ["All brokers", *sorted(broker_labels.values())]
    if st.session_state.get("portfolio_broker") not in brokers:
        st.session_state.pop("portfolio_broker", None)
    selected = filters.selectbox("Broker", brokers, key="portfolio_broker")
    force = refresh.button("Refresh prices", icon=":material/refresh:", width="stretch")
    st.caption("Broker filter applies to every card, chart, and holding on this page.")
    with st.expander("Adjust concentration thresholds"):
        left, right = st.columns(2)
        single = left.number_input(
            "Single holding above (%)", min_value=1, max_value=100, value=25, step=1
        )
        top3 = right.number_input(
            "Top three holdings above (%)", min_value=1, max_value=100, value=60, step=1
        )
        st.caption(
            "Low: neither limit exceeded. Moderate: one exceeded. High: both exceeded. "
            "Limits are adjustable app rules, not a personal recommendation."
        )
    with st.spinner("Bringing your holdings and latest closing prices together…"):
        analysis = analyze_portfolio(
            records,
            db_path,
            broker=None if selected == "All brokers" else selected,
            single_threshold=Decimal(single) / 100,
            top3_threshold=Decimal(top3) / 100,
            force=force,
        )
    for warning in dict.fromkeys(analysis.market.warnings):
        st.warning(warning)
    if analysis.date_flags:
        st.warning("Non-trading dates to review — " + " ".join(analysis.date_flags))
    for error in analysis.errors:
        st.error(error)
    if analysis.valuation is None:
        st.info(
            "Resolve the transaction or split-history issues above to calculate holdings. Original records are unchanged."
        )
        return
    value = analysis.valuation
    held = {holding.ticker for holding in value.holdings}
    quotes = [quote for ticker, quote in analysis.market.quotes.items() if ticker in held]
    dates = sorted({quote.as_of for quote in quotes if quote.as_of is not None})
    if dates:
        labels = [
            day.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %Y · %I:%M %p %Z")
            for day in dates
        ]
        st.caption(
            "Prices as of "
            + " / ".join(labels)
            + " · Latest available completed US trading session · USD"
        )
        fetched = [quote.fetched_at for quote in quotes if quote.fetched_at is not None]
        if fetched:
            st.caption(
                "Last fetched "
                + max(fetched)
                .astimezone(ZoneInfo("America/Los_Angeles"))
                .strftime("%b %d, %Y · %I:%M %p %Z")
                + " · Cached daily"
            )
    else:
        st.caption("Price timestamp unavailable · USD")
    if not value.complete:
        st.warning(
            "Incomplete market data: portfolio value, overall unrealized gains, weights, and concentration cannot be fully calculated."
        )
    for issue in value.issues:
        st.warning(issue)
    _cards(value)
    st.caption(
        "FIFO · First in, first out: the oldest shares at each broker are sold first. "
        "This is the app’s cost-basis convention, not tax advice. "
        "Dividends · Cash paid by investments; excluded from all figures here. Fees are also excluded."
    )
    _allocation(value)
    _holdings(value)
