"""KPI cards and charts drawn solely from saved transactions."""

import plotly.graph_objects as go
import streamlit as st

from portfolio_lens.transaction_summary import summarize_transactions
from portfolio_lens.ui.styles import CHART_COLORS, GREEN, INK, PURPLE, RED


def _chart(figure: go.Figure, *, height: int = 270) -> None:
    figure.update_layout(
        colorway=CHART_COLORS,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Arial, sans-serif", "color": INK, "size": 12},
        height=height,
        margin={"l": 10, "r": 10, "t": 12, "b": 10},
        legend={"orientation": "h", "y": -0.18, "x": 0},
        hoverlabel={"bgcolor": "white", "font_color": INK},
    )
    figure.update_xaxes(showgrid=False, linecolor="#E6E0FA", automargin=True)
    figure.update_yaxes(gridcolor="#F0ECFA", zerolinecolor="#E6E0FA", automargin=True)
    st.plotly_chart(
        figure,
        width="stretch",
        theme=None,
        config={"displayModeBar": False, "responsive": True},
    )


def render_dashboard(records: list[dict]) -> None:
    summary = summarize_transactions(records)
    cards = [
        (
            "Saved transactions",
            f"{summary['count']:,}",
            "Trades across your brokers",
            "receipt_long",
        ),
        (
            "Total bought ($)",
            f"${summary['bought']:,.2f}",
            "Money spent buying shares",
            "south_west",
        ),
        ("Total sold ($)", f"${summary['sold']:,.2f}", "Money received from sales", "north_east"),
        (
            "Net invested ($)",
            f"${summary['net_invested']:,.2f}",
            "Total buys minus total sells",
            "account_balance",
        ),
    ]
    for column, (label, value, hint, icon) in zip(st.columns(4), cards, strict=True):
        with column:
            # Keep the accessible metric label stable for existing clients and AppTest.
            st.metric(
                label,
                value,
                delta=f":material/{icon}: {hint}",
                delta_color="off",
                delta_arrow="off",
                border=True,
            )
    st.caption("YOUR ACTIVITY · All saved transactions · USD · Transaction values, before fees")
    with st.container(border=True, key="chart_net"):
        st.subheader("Your money, over time")
        st.caption(
            "Net invested · Cumulative buys minus sells. This is not your portfolio’s market value."
        )
        points = summary["daily"]
        area = go.Figure(
            go.Scatter(
                x=[row["date"] for row in points],
                y=[float(row["net_invested"]) for row in points],
                name="Net invested",
                mode="lines+markers",
                fill="tozeroy",
                fillcolor="rgba(108,77,246,0.12)",
                line={"color": PURPLE, "width": 3, "shape": "hv"},
                marker={"size": 5},
                hovertemplate="%{x|%b %d, %Y}<br>Net invested: $%{y:,.2f}<extra></extra>",
            )
        )
        area.update_yaxes(tickprefix="$", tickformat=",.0f")
        area.update_layout(hovermode="x unified")
        _chart(area, height=275)
    left, right = st.columns([1.65, 1])
    with left, st.container(border=True, key="chart_monthly"):
        st.subheader("A rhythm to your investing")
        st.caption("Monthly buys vs sells · Dollar value of trades")
        monthly = summary["monthly"]
        bars = go.Figure()
        for kind, name, color in [("BUY", "Buys", GREEN), ("SELL", "Sells", RED)]:
            bars.add_bar(
                x=[row["month"] + "-01" for row in monthly],
                y=[float(row[kind]) for row in monthly],
                name=name,
                marker_color=color,
                hovertemplate=f"%{{x|%b %Y}}<br>{name}: $%{{y:,.2f}}<extra></extra>",
            )
        bars.update_layout(barmode="group", bargap=0.25)
        bars.update_xaxes(tickformat="%b %Y", type="date")
        bars.update_yaxes(tickprefix="$", tickformat=",.0f")
        _chart(bars)
    with right, st.container(border=True, key="chart_brokers"):
        st.subheader("One view. Every broker.")
        st.caption("Activity by broker · Share of transaction count")
        brokers = summary["brokers"]
        donut = go.Figure(
            go.Pie(
                labels=[row["broker"] for row in brokers],
                values=[row["count"] for row in brokers],
                hole=0.73,
                sort=False,
                marker={"colors": CHART_COLORS, "line": {"color": "white", "width": 3}},
                textinfo="none",
                hovertemplate="%{label}<br>%{value} trades · %{percent}<extra></extra>",
            )
        )
        donut.add_annotation(
            text=f"<b>{summary['count']}</b><br>trades",
            x=0.5,
            y=0.5,
            showarrow=False,
            font={"size": 20, "color": INK},
        )
        _chart(donut)
