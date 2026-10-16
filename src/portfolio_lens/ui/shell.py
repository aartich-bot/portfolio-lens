"""Application shell and sidebar navigation for the four portfolio pages."""

import sqlite3
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import streamlit as st

from portfolio_lens.config import database_path
from portfolio_lens.services.transactions import TransactionService
from portfolio_lens.ui.add_transactions import render
from portfolio_lens.ui.current_portfolio import render as render_portfolio
from portfolio_lens.ui.insights import render as render_insights
from portfolio_lens.ui.performance import render as render_performance
from portfolio_lens.ui.styles import apply_styles

PAGES = [
    ("Add Transactions", ":material/add_chart:"),
    ("Current Portfolio", ":material/account_balance_wallet:"),
    ("Performance", ":material/monitoring:"),
    ("Insights", ":material/lightbulb:"),
]


def _sidebar(records: list[dict]) -> str:
    page = st.session_state.setdefault("active_page", "Add Transactions")
    with st.sidebar:
        st.markdown(
            '<div class="brand-icon">◉</div><div class="brand-name">Portfolio Lens</div>'
            '<div class="brand-tagline">Your investments.<br>A little more clarity.</div>'
            '<div class="nav-eyebrow">YOUR WORKSPACE</div>',
            unsafe_allow_html=True,
        )
        with st.container(key="sidebar_nav"):
            for title, icon in PAGES:
                if st.button(
                    title,
                    icon=icon,
                    width="stretch",
                    type="primary" if page == title else "secondary",
                ):
                    st.session_state["active_page"] = title
                    st.rerun()
        last_import = "No imports yet"
        if records:
            timestamp = datetime.fromisoformat(max(row["imported_at"] for row in records))
            last_import = timestamp.astimezone(ZoneInfo("America/Los_Angeles")).strftime(
                "%b %d, %Y · %I:%M %p %Z"
            )
        st.markdown(
            f'<div class="sidebar-footer"><strong>{len(records):,} saved transactions</strong>'
            f"<br>Last import<br>{escape(last_import)}<br><br>◈ Stored on this computer</div>",
            unsafe_allow_html=True,
        )
    return page


def _database_error() -> None:
    st.title("Let’s reconnect your transaction history")
    st.error(
        "The local transaction database could not be opened. "
        "Check that the project’s data folder is writable, then reload the app."
    )
    st.button("Try again", type="primary")


def main() -> None:
    st.set_page_config(page_title="Vamshi’s Portfolio Lens", page_icon="◉", layout="wide")
    apply_styles()
    try:
        service = TransactionService(database_path())
        records = service.list_transactions()
    except (sqlite3.Error, OSError):
        _sidebar([])
        _database_error()
        return
    page = _sidebar(records)
    if page == "Add Transactions":
        try:
            render(service)
        except (sqlite3.Error, OSError):
            _database_error()
    elif page == "Current Portfolio":
        try:
            render_portfolio(records, database_path())
        except (sqlite3.Error, OSError):
            _database_error()
    elif page == "Performance":
        try:
            render_performance(records, database_path())
        except (sqlite3.Error, OSError):
            _database_error()
    else:
        try:
            render_insights(records, database_path())
        except (sqlite3.Error, OSError):
            _database_error()
