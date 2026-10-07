"""The pay application as a PDF (F08.1 Part 2, F08.3; D-36, D-39, D-40). Built with
reportlab from the same ``ApplicationView`` the screen shows (no figure is recomputed):
Letter, **landscape** (F08.3: the schedule of values with its eight columns fits inside
the margins, measured by a test), the tenant's name, "Pay application n", the customer,
the job, the estimate, the application date and the status; the schedule of values with
fixed column widths, wrapped headers and names, and its header repeated on every page;
the summary; the surcharge line when it applies; "Invoice <number>" (the document number
to key); "Page n of m". The words are the owner's yes of 2026-10-07 (Plan answer 7); no
legend (a customer document, not a report), no product name (D-33). Money with cents,
negatives in parentheses; percents with two places.
"""

import io
from dataclasses import dataclass
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.domain.billing.figures import words
from app.domain.billing.pay_applications import ApplicationView

HEADERS = [
    "#",
    "Work area",
    "Scheduled value",
    "Percent complete to date",
    "Earned to date",
    "Earned on previous applications",
    "Earned this application",
    "Balance to finish",
]

PAGE = landscape(letter)
MARGIN = 0.5 * inch
FRAME_WIDTH = PAGE[0] - 2 * MARGIN  # 10.0 in
# F08.3 (Plan answer 3): "#", "Work area", then six percent and money columns; 9.90 in.
COLUMN_WIDTHS = [0.55 * inch, 3.05 * inch] + [1.05 * inch] * 6
FONT = "Helvetica"
FONT_SIZE = 7
CELL_PADDING = 6  # reportlab's default left and right padding, in points
WRAPPED_COLUMNS = (0, 1)  # the header row wraps in every column; the body wraps in these

_CELL = ParagraphStyle("cell", fontName=FONT, fontSize=FONT_SIZE, leading=8)
_HEAD = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=FONT_SIZE, leading=8)
_HEAD_RIGHT = ParagraphStyle("head_right", parent=_HEAD, alignment=2)


@dataclass(frozen=True)
class Heading:
    tenant_name: str
    job_name: str
    customer_name: str | None
    estimate_number: str


def percent_words(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}%"


class _NumberedCanvas(canvas.Canvas):
    """ "Page n of m" on every page, written once the count is known."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self):  # noqa: N802 (reportlab's name)
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont(FONT, FONT_SIZE)
            width = self._pagesize[0]
            self.drawRightString(width - MARGIN, 0.3 * inch, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def status_words(view: ApplicationView) -> str:
    if view.status == "draft":
        return "Draft"
    if view.status == "void":
        return f"Void. Voided on {(view.voided_at or '')[:10]}: {view.void_reason or ''}"
    return f"Issued on {(view.issued_at or '')[:10]}"


def schedule_rows(view: ApplicationView) -> list[list[str]]:
    """The schedule of values as strings: the header, one row per line, the totals row."""
    rows = [list(HEADERS)]
    for ln in view.lines:
        rows.append(
            [
                ln.label,
                ln.name,
                words(ln.scheduled_value),
                percent_words(ln.percent),
                words(ln.earned_to_date),
                words(ln.previous),
                words(ln.this_application),
                words(ln.balance),
            ]
        )
    total = lambda attr: words(sum((getattr(ln, attr) for ln in view.lines), Decimal("0.00")))  # noqa: E731
    rows.append(
        [
            "",
            "Total",
            total("scheduled_value"),
            "",
            total("earned_to_date"),
            total("previous"),
            total("this_application"),
            total("balance"),
        ]
    )
    return rows


def schedule_table(view: ApplicationView) -> Table:
    """The schedule of values laid out to ``COLUMN_WIDTHS``: the header cells and the two
    text columns are Paragraphs, so a long header or name wraps inside its column instead
    of widening it; the numbers are one-line strings (the test measures them against
    their column)."""
    rows = schedule_rows(view)
    data: list[list] = []
    for r, row in enumerate(rows):
        cells: list = []
        for c, text in enumerate(row):
            if r == 0:
                cells.append(Paragraph(text, _HEAD if c in WRAPPED_COLUMNS else _HEAD_RIGHT))
            elif c in WRAPPED_COLUMNS:
                cells.append(Paragraph(text, _CELL))
            else:
                cells.append(text)
        data.append(cells)
    table = Table(data, colWidths=COLUMN_WIDTHS, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), FONT_SIZE),
        ("LEADING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for c in range(2, 8):
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    return table


def summary_rows(view: ApplicationView) -> list[list[str]]:
    """Label and amount per row; "Billed ahead by" carries its amount in the amount column
    like the lines above it (F08.3)."""
    s = view.summary
    rows = [
        ["Total earned to date", words(s.earned_to_date)],
        ["Less billed to date before this application", words(s.billed_before)],
        ["Amount due this application", words(s.amount_due)],
    ]
    if s.billed_ahead is not None:
        rows.append(["Billed ahead by", words(s.billed_ahead)])
        rows.append(["No invoice is due", ""])
    elif s.surcharge is not None and s.surcharge_rate is not None:
        rows.append(
            [f"Fuel surcharge ({percent_words(s.surcharge_rate * 100)})", words(s.surcharge)]
        )
        rows.append(["Total to invoice", words(s.total_to_invoice or Decimal("0.00"))])
    elif s.total_to_invoice is not None:
        rows.append(["Total to invoice", words(s.total_to_invoice)])
    return rows


def application_pdf(view: ApplicationView, heading: Heading) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"Pay application {view.number}",
        pageCompression=0,  # the text stays readable to a test and a grep (F08)
    )
    styles = getSampleStyleSheet()
    small = styles["BodyText"]
    small.fontSize = 8
    small.leading = 10
    story = [
        Paragraph(heading.tenant_name, styles["Heading2"]),
        Paragraph(f"Pay application {view.number}", styles["Heading3"]),
        Paragraph(f"Customer: {heading.customer_name or ''}", small),
        Paragraph(f"Job: {heading.job_name}", small),
        Paragraph(f"Estimate: {heading.estimate_number}", small),
        Paragraph(f"Application date: {view.application_date.isoformat()}", small),
        Paragraph(status_words(view), small),
        Spacer(1, 8),
        Paragraph("Schedule of values", styles["Heading4"]),
        schedule_table(view),
        Spacer(1, 10),
        Paragraph("Summary", styles["Heading4"]),
    ]
    summary = Table(summary_rows(view), colWidths=[4.5 * inch, 1.5 * inch])
    summary.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEADING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
            ]
        )
    )
    story.append(summary)
    if view.invoice_number:
        story += [Spacer(1, 8), Paragraph(f"Invoice {view.invoice_number}", small)]
    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
