"""Locate this project and its local database without relying on the working directory."""

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PATH = PROJECT_ROOT / "transactions.csv"


def database_path() -> Path:
    """An optional override lets tests use a separate, temporary database."""
    override = os.environ.get("PORTFOLIO_LENS_DB")
    return Path(override).expanduser().resolve() if override else PROJECT_ROOT / "data/portfolio.db"


def local_today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()
