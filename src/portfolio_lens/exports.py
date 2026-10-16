"""Pure exports of supplied facts; this module performs no portfolio calculations.

CSV numbers retain their stored precision; percentages and weights are fractions.
Transactions are never sanitized or rewritten: spreadsheet-formula textual fields
make the entire export fail. PDF text uses documented ASCII transliteration for
portable Helvetica rendering, with unsupported characters displayed as ``?``.
"""

import csv
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from portfolio_lens.calculations.valuation import PortfolioValuation
from portfolio_lens.models import CSV_COLUMNS, Transaction

HOLDINGS_COLUMNS = (
    "ticker",
    "shares",
    "average_cost",
    "cost_basis",
    "price",
    "market_value",
    "unrealized_gain",
    "unrealized_pct",
    "weight",
    "sector",
    "as_of",
    "status",
)


class ExportError(ValueError):
    """An export cannot preserve the original facts safely."""


@dataclass(frozen=True)
class ReportData:
    """Already formatted, current facts assembled by the application service."""

    as_of: str
    metrics: tuple[tuple[str, str], ...]
    alerts: tuple[str, ...]
    max_drawdown: str
    projection: tuple[str, ...]
    summary: str
    notices: tuple[str, ...]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _safe_text(value: str, field: str, row: int) -> str:
    # Spreadsheet applications may ignore whitespace/control characters before
    # evaluating a formula, even when CSV correctly quotes the cell.
    offset = 0
    while offset < len(value) and (
        value[offset].isspace() or unicodedata.category(value[offset]).startswith("C")
    ):
        offset += 1
    leading = value[offset:]
    if leading.startswith(("=", "+", "-", "@")):
        raise ExportError(
            f"Cannot export row {row}: {field} begins with a spreadsheet formula marker. "
            "The original value has been preserved; correct it before exporting."
        )
    return value


def _csv_bytes(columns: tuple[str, ...], rows: Iterable[list[str]]) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def transactions_csv(records: Iterable[Mapping | Transaction]) -> bytes:
    """Export exactly six original fields in input order, omitting metadata."""
    rows = []
    for index, record in enumerate(records, start=1):
        original = record.as_dict() if isinstance(record, Transaction) else record
        cells = []
        for field in CSV_COLUMNS:
            if field not in original or original[field] is None:
                raise ExportError(f"Cannot export row {index}: original {field} is missing.")
            value = _cell(original[field])
            numeric = False
            if field in ("quantity", "price"):
                try:
                    numeric = Decimal(value).is_finite()
                except InvalidOperation:
                    pass
            if not numeric:
                value = _safe_text(value, field, index)
            cells.append(value)
        rows.append(cells)
    return _csv_bytes(CSV_COLUMNS, rows)


def holdings_csv(valuation: PortfolioValuation | None) -> bytes:
    """Export supplied holding values; missing valuation yields a header only.

    ``unrealized_pct`` and ``weight`` are fractions, not percentage points.
    Price absence marks a row unavailable; other portfolio incompleteness is
    retained in the row status. Cache/freshness metadata belongs in the report
    notices because PortfolioValuation does not carry it.
    """
    rows = []
    if valuation is not None:
        for index, holding in enumerate(valuation.holdings, start=1):
            status = (
                "unavailable"
                if holding.market_value is None or holding.price is None
                else "complete"
                if valuation.complete
                else "incomplete"
            )
            values = [getattr(holding, field) for field in HOLDINGS_COLUMNS[:-2]]
            values.extend((valuation.as_of, status))
            cells = [_cell(value) for value in values]
            for field in ("ticker", "sector"):
                position = HOLDINGS_COLUMNS.index(field)
                cells[position] = _safe_text(cells[position], field, index)
            rows.append(cells)
    return _csv_bytes(HOLDINGS_COLUMNS, rows)


_ASCII_SYMBOLS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u2265": ">=",
        "\u2264": "<=",
        "\u00b1": "+/-",
        "\u00d7": "x",
        "\u20ac": "EUR ",
        "\u00a3": "GBP ",
        "\u2022": "-",
        "\u2192": "->",
    }
)


def _pdf_text(value: str) -> str:
    translated = unicodedata.normalize("NFKD", str(value).translate(_ASCII_SYMBOLS))
    plain = "".join(char for char in translated if not unicodedata.combining(char))
    return " ".join(plain.encode("ascii", "replace").decode("ascii").split())


def _bounded_paragraph(lines: Iterable[str], style, width: float, height: float) -> Paragraph:
    # Cap before XML parsing to keep maliciously huge inputs cheap. ReportLab
    # sees only escaped text; all styling belongs to this module.
    parts = []
    capped = False
    for index, line in enumerate(lines):
        if index >= 40:
            capped = True
            break
        value = _pdf_text(str(line)[:4001])
        if len(str(line)) > 4000:
            value = value[:4000]
            capped = True
        parts.append(value)
    plain = "\n".join(parts)
    suffix = " [Content truncated to fit one page.]"

    def paragraph(content):
        return Paragraph(escape(content).replace("\n", "<br/>"), style)

    candidate = paragraph(plain + (suffix if capped else ""))
    if candidate.wrap(width, height)[1] <= height:
        return candidate
    low, high = 0, len(plain)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = paragraph(plain[:middle].rstrip() + suffix)
        if candidate.wrap(width, height)[1] <= height:
            low = middle
        else:
            high = middle - 1
    return paragraph(plain[:low].rstrip() + suffix)


def report_pdf(data: ReportData) -> bytes:
    """Render exactly one purple page, explicitly truncating lengthy sections.

    Financial values, summaries, labels, and dates are supplied by the caller.
    No rates, gains, projections, or concentrations are calculated here.
    """
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.setTitle("Vamshi's Portfolio Lens")
    purple = colors.HexColor("#6C4DF6")
    width, height = letter
    left, content_width = 36, width - 72
    canvas.setFillColor(colors.white)
    canvas.rect(0, 0, width, height, stroke=0, fill=1)
    canvas.setFillColor(purple)
    canvas.rect(0, height - 65, width, 65, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 20)
    canvas.drawString(left, height - 39, "Vamshi's Portfolio Lens")
    body = ParagraphStyle(
        "body",
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#241B38"),
        splitLongWords=True,
    )
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    y = height - 79

    def section(title: str, lines: Iterable[str], body_height: float):
        nonlocal y
        canvas.setFont("Helvetica-Bold", 10)
        canvas.setFillColor(purple)
        canvas.drawString(left, y - 10, title)
        y -= 17
        paragraph = _bounded_paragraph(lines, body, content_width, body_height)
        actual_height = paragraph.wrap(content_width, body_height)[1]
        paragraph.drawOn(canvas, left, y - actual_height)
        y -= body_height + 7

    section("As of", (data.as_of or "Unavailable",), 25)
    section("Portfolio metrics", (f"{label}: {value}" for label, value in data.metrics), 72)
    section("Concentration alerts", data.alerts or ("No current alerts supplied.",), 48)
    section("Maximum drawdown", (data.max_drawdown or "Unavailable",), 25)
    section("Contribution scenarios", data.projection or ("Unavailable",), 84)
    section("Verified summary", (data.summary or "No current summary supplied.",), 60)
    section("Data notices", data.notices or ("No additional data notices supplied.",), 72)
    assumptions = (
        "FIFO is the app's cost-basis convention. Dividends are excluded. "
        "Concentration limits are product thresholds. Scenarios are illustrations, not forecasts.",
        "Drawdown uses a midpoint cash-flow approximation; purchase and sale flows "
        "represent external contributions and withdrawals.",
        "PDF text uses ASCII transliteration; unsupported characters are shown as ?.",
    )
    canvas.setStrokeColor(colors.HexColor("#DDD2F5"))
    canvas.line(left, 100, width - left, 100)
    paragraph = _bounded_paragraph(assumptions, small, content_width, 60)
    actual_height = paragraph.wrap(content_width, 60)[1]
    paragraph.drawOn(canvas, left, 91 - actual_height)
    canvas.showPage()
    canvas.save()
    return output.getvalue()
