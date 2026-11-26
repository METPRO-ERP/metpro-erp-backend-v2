import io
import os
from fpdf import FPDF
from pdf.utils.text_utils import sanitize_text
from core.dates import fmt_day  # E-03
import logging

logger = logging.getLogger(__name__)

from pdf.utils.layout_utils import add_footer_with_signature, _category_heading, item_row
from core import pdf_options


# ==================== ABSOLUTE LOGO PATH (FIXED) ====================
# builder_conduce.py lives in: backend/pdf/builder_conduce.py
# We must go 2 levels up to reach backend/
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "assets", "logo.png")


class _ConducePdf(FPDF):
    """Conduce page footer: the real page number and the real total on EVERY page (fpdf2 total-pages alias).
    Same mechanism as the RFQ and PO. Styling is the conduce's own (italic 8, grey 180)."""

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(180, 180, 180)
        self.cell(0, 10, f"Página {self.page_no()} de {{nb}}", 0, 0, 'C')


# Project-name value column: it starts at x=45 (left_x 10 + 35 label) and the client column starts at x=110.
PROJECT_VALUE_X = 45
PROJECT_VALUE_W = 64  # 110 - 45 minus a 1 mm gap


def _project_value(pdf, text):
    """Print the project name in full inside its column. A name that fits keeps the single-line cell; a longer one
    wraps (long unbroken words break by character). Nothing is cut."""
    if pdf.get_string_width(text) <= PROJECT_VALUE_W:
        pdf.cell(0, 4, text, 0, 1)
    else:
        pdf.multi_cell(PROJECT_VALUE_W, 4, text, 0, 'L', new_x="LMARGIN", new_y="NEXT")


def create_conduce_pdf(doc_id, doc_date, client, project_name, notes, items, signer_name=None, seal=None):
    """Generate PDF bytes for a conduce (delivery note)."""
    from core.visibility import customer_items
    items = customer_items(items)  # A3: a hidden line is not shown to the customer on any document
    pdf = _ConducePdf()
    pdf.alias_nb_pages("{nb}")
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # ==================== HEADER: METPRO BRANDING ====================
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

    # ==================== TITLE ====================
    pdf.set_font('Arial', 'B', 14)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 7, 'CONDUCE (NOTA DE ENTREGA)', 0, 1, 'R')
    pdf.set_draw_color(220, 220, 220)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ==================== DOCUMENT INFO & CLIENT ====================
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(100, 100, 100)

    left_x = 10
    right_x = 110
    start_y = pdf.get_y()

    # Left column
    pdf.set_xy(left_x, start_y)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(35, 4, 'Número de Conduce:', 0, 0)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 4, sanitize_text(doc_id), 0, 1)

    pdf.set_x(left_x)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(35, 4, 'Fecha:', 0, 0)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 4, sanitize_text(fmt_day(doc_date)), 0, 1)

    if project_name:
        pdf.set_x(left_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(35, 4, 'Proyecto:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        _project_value(pdf, sanitize_text(project_name))
    left_end_y = pdf.get_y()  # the left column can now be taller than the right one

    # Right column: Client
    pdf.set_xy(right_x, start_y)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(25, 4, 'Cliente:', 0, 0)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 4, sanitize_text(client["company_name"])[:40], 0, 1)

    if client.get('contact_name'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'Contacto:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["contact_name"])[:40], 0, 1)

    if client.get('address'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'Dirección:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["address"])[:40], 0, 1)

    pdf.set_y(max(pdf.get_y(), left_end_y))  # the items start below BOTH columns
    pdf.ln(8)

    # ==================== ITEMS TABLE (NO PRICES) ====================
    pdf.set_font('Arial', 'B', 9)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 5, 'Items Entregados', 0, 1, 'L')
    pdf.ln(2)

    # Table headers - NO PRICE COLUMNS
    pdf.set_fill_color(245, 245, 245)
    pdf.set_draw_color(220, 220, 220)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(60, 60, 60)
    desc_w, qty_w, qty_h = (170, 20, 'CANT.') if pdf_options.desc_wrap_enabled() else (140, 50, 'CANTIDAD')
    pdf.cell(desc_w, 6, 'DESCRIPCIÓN', 1, 0, 'L', True)
    pdf.cell(qty_w, 6, qty_h, 1, 1, 'C', True)

    # Table rows
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)
    row_color = True

    current_category = None
    for item in items:
        current_category = _category_heading(pdf, item, current_category, 190)
        qty = float(item['quantity'] or 0)
        product_name = sanitize_text(item.get('product_name', 'Item'))
        if not pdf_options.desc_wrap_enabled():
            product_name = product_name[:80]

        if row_color:
            pdf.set_fill_color(252, 252, 252)
        else:
            pdf.set_fill_color(255, 255, 255)

        item_row(pdf, desc_w, product_name, [(qty_w, f'{qty:.2f}', 'C')])

        row_color = not row_color

    pdf.ln(12)

    # ==================== SIGNATURES ====================
    add_footer_with_signature(pdf, signer_name, seal=seal)

    # Footer: _ConducePdf.footer() prints "Página X de Y" on every page.

    pdf_bytes = pdf.output()
    return io.BytesIO(pdf_bytes)


# ==================== LEDGER CONDUCE (CONDUCE_LEDGER_ENABLED) ====================
# Separate function: create_conduce_pdf above is untouched, so the switch-OFF conduce stays byte for byte.
LEDGER_COLS = [("DESCRIPCIÓN", 66, "L"), ("UND.", 14, "C"), ("COTIZADO", 24, "C"),
               ("ESTA ENTREGA", 30, "C"), ("ACUMULADO", 28, "C"), ("PENDIENTE", 28, "C")]
_MARKERS = {"PARTIAL": "ENTREGA PARCIAL", "TOTAL": "ENTREGA TOTAL", "VOID": "CONDUCE ANULADO"}


def _ledger_thead(pdf):
    pdf.set_fill_color(245, 245, 245)
    pdf.set_draw_color(220, 220, 220)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(60, 60, 60)
    for i, (title, w, align) in enumerate(LEDGER_COLS):
        pdf.cell(w, 6, title, 1, 1 if i == len(LEDGER_COLS) - 1 else 0, align, True)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)


class _ConduceLedgerPdf(_ConducePdf):
    """Same footer (page X of Y). Once the items table has started, every new page opens with the column header."""
    repeat_header = False

    def header(self):
        if self.repeat_header and self.page_no() > 1:
            self.set_y(10)
            _ledger_thead(self)


def _qty(value) -> str:
    return f"{float(value):,.2f}"


def _kv(pdf, x, label_w, label, value, col_w=None):
    pdf.set_x(x)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(label_w, 4, label, 0, 0)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)
    value = sanitize_text(str(value or ""))
    if col_w and pdf.get_string_width(value) > col_w:
        pdf.multi_cell(col_w, 4, value, 0, 'L', new_x="LMARGIN", new_y="NEXT")  # wraps, never cuts
    else:
        pdf.cell(0, 4, value, 0, 1)


def create_conduce_ledger_pdf(number, seq, doc_date, quote_label, invoice_label, client, project_name, notes,
                              lines, marker, signer_name=None, seal=None):
    """Delivery note for one delivery of a quote: item, unit, quoted, delivered now, cumulative, pending.
    No prices, payments, phase labels or amounts. lines: dicts with category, name, unit, quoted, now, cum."""
    from pdf.utils.layout_utils import WRAP_H, WRAP_PAD
    pdf = _ConduceLedgerPdf()
    pdf.alias_nb_pages("{nb}")
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    try:
        if os.path.exists(LOGO_PATH):
            pdf.image(LOGO_PATH, x=10, y=10, w=15)
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
    pdf.cell(0, 7, 'CONDUCE (NOTA DE ENTREGA)', 0, 1, 'R')
    pdf.set_draw_color(220, 220, 220)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    start_y = pdf.get_y()
    pdf.set_xy(10, start_y)
    _kv(pdf, 10, 35, 'Número de Conduce:', number)
    _kv(pdf, 10, 35, 'Entrega:', f'No. {seq} de esta cotización')
    _kv(pdf, 10, 35, 'Fecha:', fmt_day(doc_date))
    _kv(pdf, 10, 35, 'Cotización:', quote_label)
    if invoice_label:
        _kv(pdf, 10, 35, 'Factura:', invoice_label)
    if project_name:
        _kv(pdf, 10, 35, 'Proyecto:', project_name, col_w=PROJECT_VALUE_W)
    left_end = pdf.get_y()
    pdf.set_xy(110, start_y)
    _kv(pdf, 110, 25, 'Cliente:', client.get('company_name'), col_w=65)
    if client.get('contact_name'):
        _kv(pdf, 110, 25, 'Contacto:', client['contact_name'], col_w=65)
    if client.get('address'):
        _kv(pdf, 110, 25, 'Dirección:', client['address'], col_w=65)
    pdf.set_y(max(pdf.get_y(), left_end))
    pdf.ln(5)

    label = _MARKERS.get(marker, _MARKERS["PARTIAL"])
    pdf.set_font('Arial', 'B', 8)
    pdf.set_text_color(30, 30, 30)
    pdf.set_draw_color(60, 60, 60)
    pdf.cell(pdf.get_string_width(label) + 8, 6, label, 1, 1, 'C')
    pdf.ln(3)
    pdf.set_font('Arial', 'B', 9)
    pdf.cell(0, 5, 'Items Entregados', 0, 1, 'L')
    pdf.ln(2)
    _ledger_thead(pdf)
    pdf.repeat_header = True

    current_category = None
    for ln in lines:
        current_category = _category_heading(pdf, ln, current_category, 190)
        desc_w = LEDGER_COLS[0][1]
        name = sanitize_text(ln.get('name') or 'Item')
        wrapped = pdf.multi_cell(desc_w, WRAP_H, name, 0, 'L', dry_run=True, output='LINES') or ['']
        h = WRAP_H * len(wrapped) + WRAP_PAD
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
        x, y = pdf.get_x(), pdf.get_y()
        pdf.set_fill_color(252, 252, 252)
        pdf.set_draw_color(220, 220, 220)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.rect(x, y, desc_w, h, 'DF')
        pdf.set_xy(x, y + WRAP_PAD / 2)
        pdf.multi_cell(desc_w, WRAP_H, name, 0, 'L', False)
        pdf.set_xy(x + desc_w, y)
        quoted, now, cum = float(ln['quoted']), float(ln['now']), float(ln['cum'])
        cells = [(sanitize_text(ln.get('unit') or '-'), 'C', False), (_qty(quoted), 'C', False), (_qty(now), 'C', True),
                 (_qty(cum), 'C', False), (_qty(max(quoted - cum, 0)), 'C', False)]
        for i, (text_, align, bold) in enumerate(cells):
            pdf.set_font('Arial', 'B' if bold else '', 7)
            w = LEDGER_COLS[i + 1][1]
            pdf.cell(w, h, text_, 1, 1 if i == len(cells) - 1 else 0, align, True)
        pdf.set_x(x)

    pdf.ln(4)
    if notes:
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 4, 'Observaciones:', 0, 1)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 4, sanitize_text(notes), 0, 'L')
        pdf.ln(2)
    pdf.set_font('Arial', 'I', 7)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 4, "Las cantidades acumuladas incluyen esta entrega y las anteriores de la misma cotización. "
                         "Este documento acredita la entrega física de los items listados.", 0, 'L')
    pdf.ln(8)
    add_footer_with_signature(pdf, signer_name, seal=seal)
    return io.BytesIO(pdf.output())
