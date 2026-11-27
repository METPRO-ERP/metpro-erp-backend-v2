"""R3 recibo de pago (RECEIPT_ENABLED). One page, internal receipt, NOT a comprobante fiscal.

Prints stored values only: the payment row and its two snapshot figures. Nothing is recomputed here.
"""
import io
import logging
import os

from fpdf import FPDF

from core.dates import fmt_day
from pdf.utils.layout_utils import add_footer_with_signature
from pdf.utils.text_utils import sanitize_text

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "assets", "logo.png")
NOT_FISCAL = "Este recibo no es un comprobante fiscal. No sustituye la factura ni el NCF."


def _money(value) -> str:
    return f"RD$ {float(value):,.2f}"


class _ReceiptPdf(FPDF):
    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(180, 180, 180)
        self.cell(0, 10, f"Página {self.page_no()} de {{nb}}", 0, 0, 'C')


def _row(pdf, label, value, bold=False, wrap_w=125):
    """label at x=20 (45 wide), value after it. A long value wraps inside its column; nothing is cut."""
    pdf.set_x(20)
    pdf.set_font('Arial', 'B', 8)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(45, 5, label, 0, 0)
    pdf.set_font('Arial', 'B' if bold else '', 8)
    pdf.set_text_color(30, 30, 30)
    text = sanitize_text(value)
    if pdf.get_string_width(text) <= wrap_w - 2 * pdf.c_margin:
        pdf.cell(0, 5, text, 0, 1)
    else:
        pdf.multi_cell(wrap_w, 5, text, 0, 'L', new_x="LMARGIN", new_y="NEXT")


def create_receipt_pdf(number, payment, invoice, ncf=None, signer_name=None):
    """payment: amount, method, notes, payment_date, paid_so_far_after, pending_after.
    invoice: invoice_number, total_amount, company_name, company_tax_id. ncf: printed only when given."""
    pdf = _ReceiptPdf()
    pdf.alias_nb_pages("{nb}")
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    try:
        if os.path.exists(LOGO_PATH):
            pdf.image(LOGO_PATH, x=10, y=10, w=15)
        else:
            logger.error("Logo not found at: %s", LOGO_PATH)
    except Exception as e:
        logger.error("Logo loading failed: %s", e)

    pdf.set_font('Arial', '', 6)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 3, 'Parque Industrial Disdo', 0, 1, 'R')
    pdf.cell(0, 3, 'Calle Central No. 1, Hato Nuevo Palave', 0, 1, 'R')
    pdf.cell(0, 3, 'Tel: (829) 439-8476 | RNC: 131-71683-2', 0, 1, 'R')
    pdf.ln(8)

    pdf.set_font('Arial', 'B', 14)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 7, 'RECIBO DE PAGO', 0, 1, 'R')
    pdf.set_draw_color(220, 220, 220)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    _row(pdf, 'Recibo No.:', number, bold=True)
    _row(pdf, 'Fecha de pago:', fmt_day(payment["payment_date"]))
    pdf.ln(2)
    _row(pdf, 'Cliente:', invoice.get("company_name") or "")
    if invoice.get("company_tax_id"):
        _row(pdf, 'RNC del cliente:', invoice["company_tax_id"])
    _row(pdf, 'Factura No.:', invoice["invoice_number"])
    if ncf:
        _row(pdf, 'NCF de la factura:', ncf)
    pdf.ln(4)

    pdf.set_draw_color(220, 220, 220)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(3)
    _row(pdf, 'Monto recibido:', _money(payment["amount"]), bold=True)
    _row(pdf, 'Método de pago:', payment.get("method") or "")
    if (payment.get("notes") or "").strip():
        _row(pdf, 'Nota:', payment["notes"])
    pdf.ln(2)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(3)

    _row(pdf, 'Total de la factura:', _money(invoice["total_amount"]))
    _row(pdf, 'Pagado a la fecha:', _money(payment["paid_so_far_after"]))
    _row(pdf, 'Pendiente:', _money(payment["pending_after"]), bold=True)
    pdf.set_x(20)
    pdf.set_font('Arial', 'I', 7)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(170, 4, "Pagado a la fecha y Pendiente son los saldos al momento de registrar este pago.", 0, 'L',
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_x(20)
    pdf.set_font('Arial', 'B', 8)
    pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(170, 5, NOT_FISCAL, 0, 'L', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    add_footer_with_signature(pdf, signer_name)
    return io.BytesIO(pdf.output())
