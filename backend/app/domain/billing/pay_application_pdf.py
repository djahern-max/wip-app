"""The pay application as a PDF (F08.1 Part 2; D-36, D-39, D-40). Built with reportlab
from the same ``ApplicationView`` the screen shows (no figure is recomputed): Letter,
portrait; the tenant's name, "Pay application n", the customer, the job, the estimate,
the application date and the status; the schedule of values with its header repeated on
every page; the summary; the surcharge line when it applies; "Invoice <number>" (the
document number to key); "Page n of m". The words are the owner's yes of 2026-10-07 (Plan
answer 7); no legend (a customer document, not a report), no product name (D-33). Money
with cents, negatives in parentheses; percents with two places.
"""

import io
from dataclasses import dataclass
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
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
            self.setFont("Helvetica", 7)
            self.drawRightString(
                letter[0] - 0.75 * inch, 0.5 * inch, f"Page {self._pageNumber} of {total}"
            )
            super().showPage()
        super().save()


def status_words(view: ApplicationView) -> str:
    if view.status == "draft":
        return "Draft"
    if view.status == "void":
        return f"Void. Voided on {(view.voided_at or '')[:10]}: {view.void_reason or ''}"
    return f"Issued on {(view.issued_at or '')[:10]}"


def schedule_rows(view: ApplicationView) -> list[list[str]]:
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


def summary_rows(view: ApplicationView) -> list[list[str]]:
    s = view.summary
    rows = [
        ["Total earned to date", words(s.earned_to_date)],
        ["Less billed to date before this application", words(s.billed_before)],
        ["Amount due this application", words(s.amount_due)],
    ]
    if s.billed_ahead is not None:
        rows.append([f"Billed ahead by {words(s.billed_ahead)}", ""])
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
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
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
    ]
    table = Table(schedule_rows(view), repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEADING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black),
    ]
    for c in range(2, 8):
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 10), Paragraph("Summary", styles["Heading4"])]
    summary = Table(summary_rows(view), colWidths=[4.5 * inch, 1.5 * inch])
    summary.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
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
