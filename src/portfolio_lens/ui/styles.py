"""Shared presentation tokens and app-wide styles."""

from pathlib import Path

import streamlit as st

PURPLE = "#6C4DF6"
INK = "#1E1B3A"
GREEN = "#1FA971"
RED = "#E5484D"
AMBER = "#F5A524"
CHART_COLORS = [PURPLE, "#A78BFA", "#C4B5FD", "#2DD4BF"]


def apply_styles() -> None:
    st.markdown(
        f"<style>{Path(__file__).with_name('styles.css').read_text()}</style>",
        unsafe_allow_html=True,
    )
