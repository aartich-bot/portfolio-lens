"""Saved-history filters and an accessible, sortable table with trade badges."""

from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path

import streamlit as st

from portfolio_lens.trading_calendar import non_trading_reason
from portfolio_lens.transaction_summary import transaction_rows


def _table(rows: list[dict], brokers: list[str]) -> None:
    rendered = []
    for row in rows:
        reason = row.get("non_trading_reason")
        flag = (
            f'<button class="flag" aria-label="{escape(reason, quote=True)}" '
            f'title="{escape(reason, quote=True)}">⚠</button>'
            if reason
            else ""
        )
        kind = row["transaction_type"]
        cells = [
            (row["ticker"], f"<strong>{escape(row['ticker'])}</strong>", ""),
            (row["date"], escape(row["date"]) + flag, ""),
            (kind, f'<span class="badge {kind.lower()}">{kind}</span>', ""),
            (row["quantity"], f"{Decimal(row['quantity']):,f}", "num"),
            (row["price"], f"${Decimal(row['price']):,.2f}", "num"),
            (str(row["total_value"]), f"${row['total_value']:,.2f}", "num"),
            (
                row["broker"],
                f'<span class="broker broker-{brokers.index(row["broker"]) % 4}">'
                f"{escape(row['broker'])}</span>",
                "",
            ),
        ]
        html = "".join(
            f'<td class="{style}" data-sort="{escape(value, quote=True)}">{label}</td>'
            for value, label, style in cells
        )
        rendered.append(f'<tr class="{"weekend" if reason else ""}">{html}</tr>')
    template = Path(__file__).with_name("table.html").read_text()
    st.iframe(
        template.replace("<!--ROWS-->", "".join(rendered)),
        height=466,
        alt="Sortable saved transactions",
    )


def render_history(records: list[dict]) -> None:
    st.subheader("Your transactions")
    st.caption("Every trade, in one place. Use the filters to find what you need.")
    rows = transaction_rows(records)
    for row in rows:
        try:
            row["non_trading_reason"] = non_trading_reason(date.fromisoformat(row["date"]))
        except ValueError:
            row["non_trading_reason"] = "Calendar coverage unavailable; date kept as entered."
    tickers = sorted({row["ticker"] for row in rows})
    brokers = sorted({row["broker"] for row in rows})
    # A replacement may remove the currently selected options; reset only stale filters.
    for key, options in [("filter_ticker", tickers), ("filter_broker", brokers)]:
        if key in st.session_state:
            st.session_state[key] = [value for value in st.session_state[key] if value in options]
    ticker_col, broker_col, type_col, date_col = st.columns([1, 1, 0.8, 1.4])
    ticker_filter = ticker_col.multiselect(
        "Ticker", tickers, placeholder="All tickers", key="filter_ticker"
    )
    broker_filter = broker_col.multiselect(
        "Broker", brokers, placeholder="All brokers", key="filter_broker"
    )
    kind_filter = type_col.multiselect(
        "BUY / SELL", ["BUY", "SELL"], placeholder="All trades", key="filter_kind"
    )
    dates = [date.fromisoformat(row["date"]) for row in rows]
    signature = (len(records), min(dates), max(dates), max(row["imported_at"] for row in records))
    if st.session_state.get("history_signature") != signature:
        st.session_state.pop("filter_dates", None)
        st.session_state["history_signature"] = signature
    selected_dates = date_col.date_input(
        "Date range", value=(min(dates), max(dates)), key="filter_dates", format="YYYY-MM-DD"
    )
    if len(selected_dates) != 2:
        st.info("Choose an end date to complete your date range.")
        return
    start, end = selected_dates
    filtered = [
        row
        for row in rows
        if (not ticker_filter or row["ticker"] in ticker_filter)
        and (not broker_filter or row["broker"] in broker_filter)
        and (not kind_filter or row["transaction_type"] in kind_filter)
        and start.isoformat() <= row["date"] <= end.isoformat()
    ]
    flagged = [row for row in filtered if row["non_trading_reason"]]
    if flagged:
        descriptions = ", ".join(f"{row['ticker']} on {row['date']}" for row in flagged[:3])
        more = f" and {len(flagged) - 3} more" if len(flagged) > 3 else ""
        st.warning(
            f"Non-trading dates to review: {descriptions}{more}. Kept as entered. "
            "Hover over or focus the amber marker for details.",
            icon="⚠️",
        )
    st.caption(
        f"Showing {len(filtered):,} of {len(records):,} transactions · Filters apply to this table"
    )
    if filtered:
        _table(filtered, brokers)
    else:
        st.markdown(
            '<div class="empty-results"><strong>No trades match just yet.</strong><br>'
            "Try another ticker, broker, trade type, or a wider date range.</div>",
            unsafe_allow_html=True,
        )
