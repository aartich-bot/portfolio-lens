"""Small presentation helpers shared by transaction input and review screens."""

import pandas as pd
import streamlit as st

from portfolio_lens.models import CSV_COLUMNS, ValidationIssue


def transaction_table(records: list[dict], *, include_row: bool = False) -> None:
    columns = (["row"] if include_row else []) + list(CSV_COLUMNS)
    frame = pd.DataFrame(records, columns=columns)
    frame["price"] = pd.to_numeric(frame["price"])
    st.dataframe(
        frame,
        hide_index=True,
        row_height=32,
        column_config={
            "price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "quantity": st.column_config.TextColumn("Quantity", alignment="right"),
        },
    )


def issue_table(issues: list[ValidationIssue]) -> None:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "CSV / entry row": str(issue.row) if issue.row is not None else "File",
                    "Field": issue.field or "—",
                    "Message": issue.message,
                }
                for issue in issues
            ]
        ),
        hide_index=True,
    )
