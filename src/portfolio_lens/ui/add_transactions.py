"""Render transaction forms and previews, delegating all data rules to the service."""

import sqlite3

import pandas as pd
import streamlit as st

from portfolio_lens.config import SAMPLE_PATH, local_today
from portfolio_lens.models import ALREADY_ADJUSTED, AS_TRADED, ImportPreview
from portfolio_lens.services.transactions import TransactionService
from portfolio_lens.ui.components import issue_table, transaction_table
from portfolio_lens.ui.dashboard import render_dashboard
from portfolio_lens.ui.history import render_history

SPLIT_LABELS = {
    ALREADY_ADJUSTED: "Already split-adjusted",
    AS_TRADED: "Original trade quantities and prices",
}


def _clear_preview() -> None:
    st.session_state.pop("import_preview", None)
    st.session_state.pop("confirm_replace", None)
    st.session_state.pop("duplicate_choice", None)


def _set_preview(preview: ImportPreview) -> None:
    _clear_preview()
    st.session_state["import_preview"] = preview


def _split_choice(key: str) -> str:
    return st.selectbox(
        "How does this data handle stock splits?",
        options=[ALREADY_ADJUSTED, AS_TRADED],
        format_func=SPLIT_LABELS.get,
        key=key,
        help="Choose already adjusted if both share quantities and prices have been restated "
        "for later stock splits. Otherwise choose original trade values. "
        "We save this choice with your records.",
    )


def _csv_input(service: TransactionService, mode: str) -> None:
    with st.form("csv_import_form"):
        uploaded = st.file_uploader("Choose a transactions CSV", type=["csv"])
        convention = _split_choice("csv_split_convention")
        st.caption(
            "Up to 5 MB and 10,000 rows. Use the six columns shown below; "
            "dates must be YYYY-MM-DD and prices must be in USD."
        )
        with st.container():
            st.caption("CSV format")
            st.code(
                "ticker,date,transaction_type,quantity,price,broker\n"
                "AAPL,2026-01-02,BUY,2,195.58,Fidelity",
                language="csv",
            )
        submitted = st.form_submit_button("Preview CSV", type="primary")
    if submitted:
        if uploaded is None:
            _clear_preview()
            st.warning("Choose a CSV file before previewing.")
        else:
            _set_preview(service.preview_csv(uploaded.getvalue(), uploaded.name, convention, mode))


def _manual_input(service: TransactionService) -> None:
    with st.form("manual_transaction_form"):
        left, middle, right = st.columns(3)
        ticker = left.text_input("Ticker", placeholder="AAPL", max_chars=20)
        trade_date = middle.date_input(
            "Transaction date", value=local_today(), max_value=local_today()
        )
        transaction_type = right.selectbox("Transaction type", ["BUY", "SELL"])
        left, middle, right = st.columns(3)
        quantity = left.text_input("Quantity", placeholder="10 or 0.5")
        price = middle.text_input("Price per share (USD)", placeholder="185.07")
        broker = right.text_input(
            "Broker", placeholder="Robinhood, Fidelity, Vanguard…", max_chars=100
        )
        convention = _split_choice("manual_split_convention")
        submitted = st.form_submit_button("Preview transaction", type="primary")
    if submitted:
        _set_preview(
            service.preview_manual(
                {
                    "ticker": ticker,
                    "date": trade_date.isoformat(),
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "price": price,
                    "broker": broker,
                },
                convention,
            )
        )


def _review(service: TransactionService, saved_count: int) -> None:
    preview = st.session_state.get("import_preview")
    if preview is None:
        return
    with st.container(border=True, key="review_card"):
        st.subheader("Review before saving")
        duplicate_policy = "skip"
        if preview.duplicate_count:
            choice = st.radio(
                "Duplicate rows",
                ["Skip duplicate rows", "Keep all rows (including duplicates)"],
                key="duplicate_choice",
                help="An exact match on all six fields is a possible duplicate. "
                "Keep it only when it represents a separate, intentional trade.",
            )
            duplicate_policy = "skip" if choice == "Skip duplicate rows" else "keep"
        preview = service.review_preview(preview, duplicate_policy)
        st.caption(
            f"Source: {preview.source_name} · {SPLIT_LABELS.get(preview.split_convention)} · "
            f"Action: {preview.mode.capitalize()}"
        )
        errors = [issue for issue in preview.issues if issue.severity == "error"]
        warnings = [issue for issue in preview.issues if issue.severity == "warning"]
        if errors:
            st.error(
                f"Found {len(errors)} validation error(s). Fix the input and preview again. "
                "No rows from this batch will be saved until all errors are fixed."
            )
            issue_table(errors)
        if warnings:
            st.warning(f"{len(warnings)} warning(s) to review. Warnings do not block saving.")
            issue_table(warnings)
        if preview.is_valid:
            st.success(f"{len(preview.transactions)} transaction(s) passed validation.")
        if preview.transactions:
            transaction_table(
                [{"row": txn.source_row, **txn.as_dict()} for txn in preview.transactions],
                include_row=True,
            )
        st.caption(
            "This is a snapshot of your last preview. After editing the form, preview again."
        )
        skipped = preview.duplicate_count if duplicate_policy == "skip" else 0
        st.write(
            f"**{len(preview.transactions) - skipped} row(s) to save** · "
            f"{skipped} duplicate row(s) to skip"
        )
        confirmed = True
        if preview.mode == "replace":
            confirmed = st.checkbox(
                f"Replace all {saved_count} saved transaction(s) with this batch",
                key="confirm_replace",
            )
        save, cancel = st.columns([1, 3])
        if save.button(
            "Save transactions", type="primary", disabled=not preview.is_valid or not confirmed
        ):
            try:
                result = service.commit(
                    preview, mode=preview.mode, duplicate_policy=duplicate_policy
                )
            except (ValueError, sqlite3.Error) as exc:
                st.error(f"Could not save this batch: {exc}")
            else:
                _clear_preview()
                st.session_state["close_import_panel"] = True
                st.session_state["save_notice"] = (
                    f"Saved {result.inserted_count} transaction(s); skipped {result.skipped_count} "
                    f"duplicate(s). {result.total_count} transaction(s) are now stored locally."
                )
                st.rerun()
        if cancel.button("Discard preview"):
            _clear_preview()
            st.rerun()


def _import_options(service: TransactionService, saved_count: int) -> None:
    with st.container(key="import_options"):
        source = st.radio(
            "Add transactions using",
            ["Sample data", "CSV upload", "Manual entry"],
            horizontal=True,
            key="input_source",
            on_change=_clear_preview,
        )
    mode = "append"
    if source != "Manual entry":
        action = st.radio(
            "When you save this batch",
            ["Append to saved transactions", "Replace saved transactions"],
            horizontal=True,
            key="import_action",
            on_change=_clear_preview,
        )
        mode = "append" if action.startswith("Append") else "replace"
    if source == "Sample data":
        with st.container(border=True, key="sample_card"):
            st.markdown("**A ready-to-explore portfolio**")
            st.write("28 transactions · 7 tickers · Robinhood, Fidelity, and Vanguard")
            st.caption("January 2024 – May 2026 · USD · Already split-adjusted")
            if st.button("Load Sample Data", type="primary"):
                try:
                    data = SAMPLE_PATH.read_bytes()
                except OSError:
                    st.error(
                        "The sample file could not be read. Check transactions.csv in the project folder."
                    )
                else:
                    _set_preview(
                        service.preview_csv(data, SAMPLE_PATH.name, ALREADY_ADJUSTED, mode)
                    )
    elif source == "CSV upload":
        _csv_input(service, mode)
    else:
        _manual_input(service)
    _review(service, saved_count)


def render(service: TransactionService) -> None:
    if st.session_state.pop("close_import_panel", False):
        st.session_state["import_panel"] = False
    notice = st.session_state.pop("save_notice", None)
    if notice:
        st.success(notice)
    records = service.list_transactions()
    if not records:
        st.markdown(
            '<section class="hero"><span class="pill">A clearer start to investing</span>'
            "<h1>Welcome, Vamshi.<br>Make yourself at home.</h1>"
            "<p>Your first trade is a starting point. Bring your transactions together "
            "to see where your money is going, one step at a time.</p></section>",
            unsafe_allow_html=True,
        )
        st.subheader("Three ways to get started")
        st.caption(
            "Try the sample, bring a CSV, or add your first trade. You’ll review everything before saving."
        )
        _import_options(service, 0)
    else:
        heading, action = st.columns([3, 1], vertical_alignment="center")
        with heading:
            st.markdown('<div class="eyebrow">YOUR INVESTING JOURNEY</div>', unsafe_allow_html=True)
            st.title("A clearer view of your activity")
            st.caption("Welcome back, Vamshi. Here’s the story behind your trades.")
        with action:
            if st.button("+ Add transactions", type="primary", width="stretch"):
                st.session_state["import_panel"] = True
        with st.expander(
            "Add to your transaction history",
            expanded=False,
            key="import_panel",
            on_change="rerun",
            icon=":material/add_circle:",
        ):
            # Keep forms mounted while collapsed to preserve their widget values and previews.
            _import_options(service, len(records))
        render_dashboard(records)
        with st.container(border=True, key="history_card"):
            render_history(records)
        with st.expander("Import details", icon=":material/folder_open:"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Ticker": row["ticker"],
                            "Trade date": row["date"],
                            "Source": row["source_name"],
                            "Source row": row["source_row"],
                            "Split convention": SPLIT_LABELS.get(row["split_convention"]),
                            "Saved at (UTC)": row["imported_at"],
                        }
                        for row in records
                    ]
                ),
                hide_index=True,
            )
    st.caption(
        "◈ Transactions stay on this computer. Non-trading dates are flagged and kept as entered. "
        "Sales are checked against shares held at the same broker."
    )
