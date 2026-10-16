"""Present concentration, past falls, contribution illustrations, and local downloads."""

from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import streamlit as st

from portfolio_lens.calculations.risk import RiskThresholds
from portfolio_lens.exports import ExportError, holdings_csv, report_pdf, transactions_csv
from portfolio_lens.services import summary as summaries
from portfolio_lens.services.insights import (
    analyze_insights,
    estimation_period,
    make_report,
    money,
    percent,
)
from portfolio_lens.ui.styles import INK, PURPLE


def _risk(analysis) -> None:
    st.subheader("What deserves a closer look?")
    st.caption(
        "Concentration · How much of your holdings rests in one investment or business sector. "
        "These are adjustable product thresholds, not universal risk judgments or trading instructions."
    )
    alerts = analysis.risk.alerts
    if alerts:
        columns = st.columns(min(3, len(alerts)))
        for index, alert in enumerate(alerts):
            with columns[index % len(columns)]:
                st.markdown(
                    f'<section class="insight-alert {escape(alert.severity)}">'
                    f'<div class="alert-severity">{escape(alert.severity.title())}</div>'
                    f"<h3>{escape(alert.title)}</h3><p>{escape(alert.message)}</p></section>",
                    unsafe_allow_html=True,
                )
    elif analysis.risk.complete and analysis.valuation and analysis.valuation.holdings:
        st.success(
            "No configured concentration limit is exceeded in the holdings and known sectors checked."
        )
    elif analysis.risk.complete:
        st.info("There are no open holdings to check for concentration.")
    else:
        st.warning("Concentration checks are unavailable until holdings have complete prices.")
    if analysis.risk.sector_coverage is not None:
        st.caption(
            f"Known stock-sector coverage: {percent(analysis.risk.sector_coverage)} of portfolio value. "
            "ETF · A fund holding a basket of investments. ETFs and unknown sectors are excluded "
            "from sector checks because their underlying holdings are not examined."
        )
    st.caption(
        "Holding and top-three checks include ETFs and combine all brokers; splitting a holding across brokers does not reduce its weight."
    )


def _chart_style(figure, height):
    figure.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": INK, "family": "Arial, sans-serif"},
        margin={"l": 5, "r": 10, "t": 15, "b": 10},
        xaxis={"showgrid": False},
        yaxis={"gridcolor": "#EEE9F8"},
        legend={"orientation": "h", "y": 1.12, "x": 0},
    )


def _drawdown(analysis) -> None:
    with st.container(border=True, key="insights_drawdown"):
        st.subheader("How far has it fallen?")
        st.metric("Max drawdown", percent(analysis.drawdown.max_drawdown))
        st.caption(
            "Drawdown · The largest drop from a previous peak, after adjusting for money added or removed."
        )
        result = analysis.drawdown
        if result.reason:
            st.info(result.reason)
        if result.points:
            figure = go.Figure(
                go.Scatter(
                    x=[point.day.isoformat() for point in result.points],
                    y=[-point.drawdown for point in result.points],
                    mode="lines",
                    fill="tozeroy",
                    line={"color": PURPLE, "width": 2},
                    fillcolor="rgba(108,77,246,0.12)",
                    connectgaps=False,
                    name="Fall from peak",
                    hovertemplate="%{x}<br>%{y:.2%} below peak<extra></extra>",
                )
            )
            _chart_style(figure, 250)
            figure.update_yaxes(tickformat=".0%", autorange=True)
            st.plotly_chart(figure, width="stretch", key="drawdown_chart")
        st.caption(
            "Flow-adjusted · Removes the effect of purchase and sale amounts using a daily "
            "midpoint timing approximation. Starts at your first completed valuation."
        )


def _projection(analysis) -> None:
    with st.container(border=True, key="insights_projection"):
        st.subheader("What could regular contributions look like?")
        st.caption(
            "Illustrative scenarios, not a forecast. Future returns can be lower or higher, including losses."
        )
        if analysis.projection.reason:
            st.info(analysis.projection.reason)
        scenarios = analysis.projection.scenarios
        if not scenarios:
            return
        figure = go.Figure()
        colors = {"lower": "#A78BFA", "base": PURPLE, "higher": "#2B9389"}
        for scenario in scenarios:
            figure.add_trace(
                go.Scatter(
                    x=[point.month / 12 for point in scenario.points],
                    y=[point.balance for point in scenario.points],
                    mode="lines",
                    name=scenario.name.title(),
                    line={
                        "color": colors[scenario.name],
                        "width": 3 if scenario.name == "base" else 2,
                    },
                    hovertemplate="Year %{x:.1f}<br>%{y:$,.2f}<extra>%{fullData.name}</extra>",
                )
            )
        _chart_style(figure, 290)
        figure.update_layout(
            xaxis_title="Years from now",
            yaxis={"tickprefix": "$", "tickformat": ",.0f", "gridcolor": "#EEE9F8"},
        )
        st.plotly_chart(figure, width="stretch", key="projection_chart")
        for column, scenario in zip(st.columns(3), scenarios, strict=True):
            with column:
                st.metric(f"{scenario.name.title()} scenario", money(scenario.points[-1].balance))
                st.caption(f"Assumed yearly return: {percent(scenario.annual_rate)}")
        st.caption(
            f"Starts with {money(analysis.valuation.current_value)}; adds {money(analysis.monthly)} "
            f"at each month-end for {analysis.years} years. New contributions total "
            f"{money(analysis.projection.contributed)}."
        )
        st.caption(
            f"Historical yearly growth: {percent(analysis.drawdown.annual_return)}; "
            f"annualized volatility: {percent(analysis.drawdown.annual_volatility)}. "
            "Volatility · How much daily returns varied, expressed on a yearly scale. "
            "The lower and higher rates vary log-growth by that amount; they are not probability bounds."
        )
        st.caption(
            estimation_period(analysis)
            + " The first valuation is the starting point; its execution-day change is excluded."
        )


def _summary(analysis):
    with st.container(border=True, key="insights_summary_card"):
        st.subheader("Your next review, in plain English")
        st.caption(
            "AI explains verified aggregate metrics only; raw transactions and broker names stay local. "
            "Without an API key, this feature uses the built-in rule-based explanation."
        )
        saved = st.session_state.get("insights_summary")
        if saved and saved.fingerprint != analysis.fingerprint:
            st.session_state.pop("insights_summary", None)
            saved = None
        if st.button("Generate AI Summary", icon=":material/auto_awesome:", type="primary"):
            with st.spinner("Preparing an explanation of the verified numbers…"):
                saved = summaries.generate_summary(analysis.facts, fingerprint=analysis.fingerprint)
                st.session_state["insights_summary"] = saved
        result = saved or summaries.rule_based_summary(
            analysis.facts, fingerprint=analysis.fingerprint
        )
        st.caption(
            "AI-assisted explanation selection"
            if result.source == "ai"
            else "Rule-based explanation"
        )
        st.caption(result.notice)
        st.write(result.text)
        st.caption(
            "Review these observations against your goals and comfort with losses; this summary does not recommend trades."
        )
        with st.expander("What can be shared with AI?"):
            st.write(
                "Only the following verified facts and their approved explanations can leave this app when you press Generate AI Summary with a key configured. The model chooses which to emphasize; it cannot supply new numbers."
            )
            st.dataframe(
                [
                    {
                        "Metric": fact.label,
                        "Value": fact.value,
                        "Verified statement": fact.statement,
                        "Approved explanation": " ".join(fact.explanations),
                    }
                    for fact in analysis.facts
                ],
                hide_index=True,
                width="stretch",
            )
    return result


def _downloads(analysis, records, summary) -> None:
    st.subheader("Keep a copy")
    st.caption(
        "Exports reflect the current data and sliders. Holdings CSV percentages are fractions; missing numbers stay blank. Transactions keep their original dates and values."
    )
    columns = st.columns(3)
    for column, label, producer, filename in [
        (
            columns[0],
            "Download holdings CSV",
            lambda: holdings_csv(analysis.valuation),
            "holdings.csv",
        ),
        (
            columns[1],
            "Download transactions CSV",
            lambda: transactions_csv(records),
            "transactions.csv",
        ),
        (
            columns[2],
            "Download summary PDF",
            lambda: report_pdf(make_report(analysis, summary)),
            "portfolio-summary.pdf",
        ),
    ]:
        with column:
            try:
                data = producer()
            except ExportError as exc:
                st.warning(str(exc))
                continue
            st.download_button(
                label,
                data,
                file_name=filename,
                mime="application/pdf" if filename.endswith(".pdf") else "text/csv",
                width="stretch",
                on_click="ignore",
            )
    if analysis.valuation is None:
        st.caption("Holdings are unavailable; the holdings CSV contains only column headings.")


def render(records: list[dict], db_path: str | Path) -> None:
    st.markdown('<div class="eyebrow">YOUR NEXT STEP</div>', unsafe_allow_html=True)
    st.title("Insights")
    st.write(
        "What should I do next? Review concentration, understand past falls, and explore regular contributions."
    )
    if not records:
        st.info(
            "Start in Add Transactions and save the sample or your own trades to explore Insights."
        )
        return
    with st.expander("Adjust risk thresholds"):
        st.caption(
            "Alerts appear only above a limit. Changing these product settings changes the alerts, not your holdings."
        )
        columns = st.columns(3)
        values = []
        for column, label, initial in zip(
            columns,
            ["Single holding limit (%)", "Top three limit (%)", "Sector limit (%)"],
            [25, 60, 50],
            strict=True,
        ):
            with column:
                values.append(Decimal(st.slider(label, 1, 100, initial)) / 100)
    with st.container(border=True, key="insights_controls"):
        contribution, horizon, refresh = st.columns([2, 2, 1])
        with contribution:
            monthly = Decimal(st.slider("Monthly contribution ($)", 0, 2000, 500, step=50))
        with horizon:
            years = st.slider("Years", 1, 30, 10)
        with refresh:
            force = st.button("Refresh insights", icon=":material/refresh:", width="stretch")
    with st.spinner("Checking concentration and building your illustrations…"):
        analysis = analyze_insights(
            records,
            db_path,
            thresholds=RiskThresholds(*values),
            monthly=monthly,
            years=years,
            force=force,
        )
    st.caption(
        f"Values as of {analysis.performance.as_of.isoformat()} market close · All brokers · USD · Dividends excluded."
    )
    timestamps = sorted((analysis.performance.market.fetched_at or {}).values())
    if timestamps:
        stamp = (
            timestamps[0]
            .astimezone(ZoneInfo("America/Los_Angeles"))
            .strftime("%Y-%m-%d %I:%M %p %Z")
        )
        st.caption(
            f"Oldest price-history download used: {stamp}. Daily cache; refresh retries missing data."
        )
    for warning in analysis.warnings[:3]:
        st.warning(warning)
    if len(analysis.warnings) > 3:
        with st.expander(f"All {len(analysis.warnings)} data notices"):
            for warning in analysis.warnings:
                st.warning(warning)
    _risk(analysis)
    left, right = st.columns([1, 2])
    with left:
        _drawdown(analysis)
    with right:
        _projection(analysis)
    summary = _summary(analysis)
    _downloads(analysis, records, summary)
    with st.expander("Insights assumptions"):
        st.markdown(
            "- **Risk alerts:** strict breaches of adjustable product thresholds, using open holdings "
            "across brokers. Known sector weights use the whole portfolio as their denominator. "
            "ETFs and unknown sectors remain excluded from sector coverage.\n"
            "- **Daily flow-adjusted return:** (ending value − starting value − net contributions) "
            "/ (starting value + half of net contributions). Purchases add money and sales remove it. "
            "Midpoint timing is an approximation because the CSV has no trade times or cash ledger. "
            "Returns are linked from the first completed valuation; that first day's execution change "
            "is excluded. Missing values or invalid return intervals make drawdown unavailable.\n"
            "- **Maximum drawdown:** the greatest percentage drop of that linked return index from "
            "a prior peak. It describes this observed history, not the worst possible future loss.\n"
            "- **Illustrations:** at least thirty valid daily-return intervals are required. Historical "
            "geometric growth and daily-return variability set three constant yearly rates. Monthly "
            "contributions enter at month-end. No probability or forecast is implied; inflation, "
            "dividends, taxes, and fees are excluded.\n"
            "- **FIFO:** first-in, first-out uses the oldest shares first within each broker; this is "
            "an app convention, not tax advice. Original dates and split conventions remain stored.\n"
            "- **Summary and report:** all numbers come from calculations. AI only chooses approved "
            "explanations. Changing data or sliders invalidates an older summary. The one-page PDF "
            "may shorten long sections and transliterate unsupported characters; truncation is labeled."
        )
