import io
import os
from fpdf import FPDF
from pdf.utils.text_utils import sanitize_text
from core.dates import fmt_day  # E-03: every printed date is dd/mm/aaaa
from quotes.dynamic_charges import dynamic_charge_lines
from pdf.utils.seal_utils import SEAL_OVERLAP_MM, draw_seal
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
LOGO_PATH = os.path.join(BASE_DIR, "assets", "logo.png")


def add_footer_with_signature(pdf, signer_name=None, seal=None):
    """Add signature section with two columns to PDF.

    seal: optional pdf.utils.seal_utils.PreparedSeal. None (the default) leaves the layout exactly as before.
    With a seal, room is made above the signature line and the seal is drawn first, so the typed signature
    and the line print over it.
    """

    sig_y = pdf.get_y() + 10
    if seal is not None:
        sig_y = max(sig_y, pdf.get_y() + seal.height_mm - SEAL_OVERLAP_MM + 2)  # keep the stamp clear of the content above
    if sig_y > 260:
        pdf.add_page()
        sig_y = 20 if seal is None else 15 + seal.height_mm - SEAL_OVERLAP_MM

    FONT_PATH = os.path.join(BASE_DIR, "fonts", "GreatVibes-Regular.ttf")

    font_loaded = False
    try:
        pdf.add_font("GreatVibes", "", FONT_PATH, uni=True)
        font_loaded = True
    except Exception as e:
        logger.error("Font load failed: %s", e)

    # Company seal (optional). Drawn before the signature text so the signature sits over it.
    if seal is not None:
        try:
            draw_seal(pdf, seal, center_x=15 + 75 / 2, sig_y=sig_y)
        except Exception:
            logger.exception("Seal drawing failed; continuing without it")

    # Left — METPRO signature
    pdf.set_xy(15, sig_y)
    if font_loaded:
        pdf.set_font("GreatVibes", "", 18)
    else:
        pdf.set_font("Arial", "I", 12)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(75, 6, sanitize_text(signer_name) if signer_name else "", 0, 1, "C")

    pdf.set_draw_color(180, 180, 180)
    pdf.line(15, sig_y + 8, 90, sig_y + 8)

    pdf.set_xy(15, sig_y + 10)
    pdf.set_font("Arial", "B", 7)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(75, 4, "Representante Metpro", 0, 1, "C")

    # Right — client signature
    pdf.line(115, sig_y + 8, 190, sig_y + 8)

    pdf.set_xy(115, sig_y + 10)
    pdf.set_font("Arial", "B", 7)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(75, 4, "Representante Cliente", 0, 1, "C")


def _category_heading(pdf, item, current, width):
    """
    Bold section row above the first item of each template-pack category.
    Items without a category after categorised ones go under "Otros".
    Documents with no categories at all render exactly as before.
    Returns the category now in effect.
    """
    cat = (item.get("category") or "").strip() or None
    if cat == current:
        return current
    if cat is None and current is None:
        return current
    label = cat if cat else "Otros"
    pdf.set_font("Arial", "B", 7)
    pdf.set_fill_color(235, 235, 235)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(width, 5, sanitize_text(label)[:80], 1, 1, "L", True)
    pdf.set_font("Arial", "", 7)
    pdf.set_text_color(30, 30, 30)
    return cat


from core.visibility import customer_items  # noqa: E402  (A3)
from core import pdf_options  # noqa: E402

LINE_H = 5  # one text line in an item row (mm)
WRAP_H, WRAP_PAD = 3.6, 1.4  # wrapped description: line height and top+bottom padding (mm)
# (width mm, header) for Unidad, Cantidad, Precio unit., Total
CLASSIC_COLS = ((25, 'UNIDAD'), (25, 'CANTIDAD'), (35, 'PRECIO UNIT.'), (45, 'TOTAL'))
COMPACT_COLS = ((14, 'UND.'), (17, 'CANT.'), (26, 'P.U.'), (30, 'TOTAL'))


def item_row(pdf, desc_w, desc, cells, fill=True):
    """
    One item row. With PDF_DESC_WRAP_ENABLED on, the description wraps onto as many lines as it
    needs and every cell in the row grows to the same height. The row never splits across pages:
    if it does not fit, a new page starts first. With the switch off, the old single-line row prints.
    cells: list of (width, text, align) for the columns after the description.
    """
    if not pdf_options.desc_wrap_enabled():
        pdf.cell(desc_w, LINE_H, desc, 1, 0, 'L', fill)
        for i, (w, text, align) in enumerate(cells):
            pdf.cell(w, LINE_H, text, 1, 1 if i == len(cells) - 1 else 0, align, fill)
        return
    lines = pdf.multi_cell(desc_w, WRAP_H, desc, 0, 'L', dry_run=True, output='LINES') or ['']
    h = WRAP_H * len(lines) + WRAP_PAD  # one line = 5 mm, same as before
    if pdf.get_y() + h > pdf.page_break_trigger:
        pdf.add_page()
    x, y = pdf.get_x(), pdf.get_y()
    pdf.rect(x, y, desc_w, h, 'DF' if fill else 'D')
    pdf.set_xy(x, y + WRAP_PAD / 2)
    pdf.multi_cell(desc_w, WRAP_H, desc, 0, 'L', False)
    pdf.set_xy(x + desc_w, y)
    for i, (w, text, align) in enumerate(cells):
        pdf.cell(w, h, text, 1, 1 if i == len(cells) - 1 else 0, align, fill)
    pdf.set_x(x)


def _pct_text(value) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".") + "%"


PHASE_BLOCK_H = 62  # mm kept together: heading, phase line, base, tax, boxed total, accumulated, pending, note


def _phase_block(pdf, d):
    """The "Facturación por fases" block (PHASE_INVOICE_LAYOUT_ENABLED). Every figure is a stored value given by the
    caller; nothing here calls a tax function, and no rate is printed next to the phase tax (it is a stored
    pro-rata share, not rate x the printed lines)."""
    if pdf.get_y() + PHASE_BLOCK_H > pdf.h - 15:
        pdf.add_page()  # the dominant total is never split from its lines
    pdf.set_x(10)
    pdf.set_font('Arial', 'B', 9)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 5, sanitize_text('Facturación por fases'), 0, 1, 'L')
    pdf.ln(1)

    y0 = pdf.get_y()
    pdf.set_x(10)
    pdf.set_font('Arial', 'B', 8)
    pdf.multi_cell(110, 5, sanitize_text(f"Fase {d['number']} - {d['label']}"), 0, 'L')
    y1 = pdf.get_y()
    if d['input_type'] == 'percent' and d.get('input_value') is not None:
        basis = f"{_pct_text(d['input_value'])} del total de la cotización"
    elif d['input_type'] == 'fixed':
        basis = "Monto fijo acordado"  # no percentage was agreed; none is derived
    else:
        basis = "Saldo final"
    pdf.set_xy(120, y0)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(80, 5, sanitize_text(basis), 0, 1, 'R')
    pdf.set_y(max(y1, y0 + 5) + 1)

    x, lw, vw = 110, 60, 30
    tax_line = f"{d.get('tax_word') or 'ITBIS'} de la fase (parte proporcional):"
    for lbl, val in (("Base de la fase:", d['base']), (tax_line, d['itbis'])):
        pdf.set_x(x)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(lw, 4.5, sanitize_text(lbl), 0, 0, 'L')
        pdf.set_text_color(30, 30, 30)
        pdf.cell(vw, 4.5, f"${val:,.2f}", 0, 1, 'R')
    pdf.ln(1)
    pdf.set_x(x)
    pdf.set_fill_color(235, 235, 235)
    pdf.set_draw_color(120, 120, 120)
    pdf.set_font('Arial', 'B', 12)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(lw, 9, 'TOTAL DE ESTA FACTURA:', 1, 0, 'L', True)
    pdf.cell(vw, 9, f"${d['total']:,.2f}", 1, 1, 'R', True)
    pdf.ln(2)
    for lbl, val in (("Facturado acumulado (incl. esta fase):", d['cumulative']), ("Pendiente de facturar:", d['pending'])):
        pdf.set_x(x)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(lw, 4.5, sanitize_text(lbl), 0, 0, 'L')
        pdf.set_text_color(30, 30, 30)
        pdf.cell(vw, 4.5, f"${val:,.2f}", 0, 1, 'R')
    pdf.ln(2)
    pdf.set_x(10)
    pdf.set_font('Arial', '', 6)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(190, 3.2, sanitize_text("El ITBIS de cada fase es la parte proporcional del ITBIS de la cotización. "
                                           "No se calcula sobre los renglones impresos."), 0, 'L')


def build_quote_invoice_pdf(
    doc_type, doc_id, doc_date, client, project_name, notes, items,
    charges, items_total, total_discounts, items_after_discount,
    supervision, supervision_pct, admin, admin_pct, insurance, insurance_pct,
    transport, transport_pct, contingency, contingency_pct,
    subtotal_general, itbis, grand_total,
    payment_terms=None,
    valid_until=None,
    payments=None,
    amount_paid=0,
    amount_due=0,
    signer_name=None,
    seal=None,
    tax_label=None,
    print_items=True,
    full_copy=False,
    ncf=None,
    ncf_valid_until=None,
    phase_doc=None
):
    payments = payments or []
    amount_paid = amount_paid or 0
    amount_due = amount_due or 0
    # Internal full copy (admin only, own switch): every line prints and the checkbox is ignored. Totals are the same.
    if full_copy:
        print_items = True
    else:
        items = customer_items(items)  # A3: hidden lines stay in the totals the caller computed, never print
    # PDF update. print_items = false (the quote checkbox, copied to the invoice) removes the whole item table;
    # charges and totals are the caller's and never change. The other two features follow their switches.
    show_items = print_items is not False
    project_heading = pdf_options.project_desc_enabled() and bool(project_name)
    show_units = show_items and pdf_options.medidas_enabled() and any(pdf_options.clean_unit(i.get('unit_of_measure')) for i in items)

    pdf = FPDF()

    # Page 1: cover (portada). A failure here must never block the quote or invoice itself.
    # 2026-11-03: with PORTADA_NOTE_ENABLED the quote letter carries the note; the bottom "Notas" block is then left out.
    note_in_cover = False
    try:
        from cover_page.service import get_active_body
        kind = "quote" if doc_type == "COTIZACION" else "invoice"
        cover_body = get_active_body(kind)
        if cover_body:
            from pdf.builder_cover import add_cover_page
            note_in_cover = add_cover_page(
                pdf, kind, cover_body,
                client_name=(client or {}).get("company_name", ""),
                contact_name=(client or {}).get("contact_name", ""),
                user_name=signer_name or "",
                date_text=doc_date,
                doc_label=doc_id,
                note=notes or "",
            ) is True
    except Exception:
        note_in_cover = False
        logger.exception("Cover page failed; continuing without it")

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
    # FISCAL_NCF_ENABLED: an invoice with an NCF prints the fiscal title, the NCF and the batch expiry here.
    # Nothing else moves. With no ncf (switch off, or an invoice from before activation) this is the old output.
    pdf.cell(0, 7, 'Factura de Crédito Fiscal' if ncf else doc_type, 0, 1, 'R')
    if ncf:
        pdf.set_font('Arial', 'B', 8)
        pdf.cell(0, 4, sanitize_text('NCF: ' + str(ncf)), 0, 1, 'R')
        if ncf_valid_until:
            pdf.set_font('Arial', '', 7)
            pdf.cell(0, 4, sanitize_text('Vencimiento NCF: ' + fmt_day(ncf_valid_until)), 0, 1, 'R')
        pdf.ln(1)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(100, 100, 100)

    left_x = 10
    right_x = 110
    start_y = pdf.get_y()

    pdf.set_xy(left_x, start_y)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(80, 80, 80)
    label = 'Número de Cotización:' if doc_type == 'COTIZACION' else 'Número de Factura:'
    pdf.cell(35, 4, label, 0, 0)
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

    if payment_terms:
        pdf.set_x(left_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(35, 4, 'Términos de Pago:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(payment_terms)[:60], 0, 1)

    if valid_until:
        pdf.set_x(left_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(35, 4, 'Válida Hasta:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(fmt_day(valid_until)), 0, 1)

    if project_name and not project_heading:  # with the heading switch on, the name prints above the items instead
        pdf.set_x(left_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(35, 4, 'Proyecto:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(project_name)[:60], 0, 1)

    pdf.set_xy(right_x, start_y)
    pdf.set_font('Arial', 'B', 7)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(25, 4, 'Cliente:', 0, 0)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 4, sanitize_text(client.get("company_name", ""))[:40], 0, 1)

    if client.get('contact_name'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'Contacto:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["contact_name"])[:40], 0, 1)

    if client.get('email'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'Email:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["email"])[:40], 0, 1)

    if client.get('phone'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'Teléfono:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["phone"])[:30], 0, 1)

    if client.get('address'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'Dirección:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["address"])[:40], 0, 1)

    if client.get('tax_id'):
        pdf.set_x(right_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(25, 4, 'RNC:', 0, 0)
        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 4, sanitize_text(client["tax_id"])[:30], 0, 1)

    pdf.ln(8)
    final_y = max(pdf.get_y(), start_y + 20)
    pdf.set_y(final_y + 4)

    def _project_heading():
        pdf.set_x(10)
        pdf.set_font('Arial', 'B', 9)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(190, 5, sanitize_text('Proyecto: ' + str(project_name)), 0, 'L')
        pdf.ln(2)

    if project_heading:
        _project_heading()  # right on top of "Detalle de Items"; with no table it still prints, then the totals follow

    if show_items:
        pdf.set_font('Arial', 'B', 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 5, 'Detalle de Items', 0, 1, 'L')
        pdf.ln(2)

        pdf.set_fill_color(245, 245, 245)
        pdf.set_draw_color(220, 220, 220)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(60, 60, 60)
        if pdf_options.desc_wrap_enabled():
            # Compact number columns; the description gets the rest of the 190 mm
            (unit_w, unit_h), (qty_w, qty_h), (pu_w, pu_h), (tot_w, tot_h) = COMPACT_COLS
        else:
            (unit_w, unit_h), (qty_w, qty_h), (pu_w, pu_h), (tot_w, tot_h) = CLASSIC_COLS
        desc_w = 190 - qty_w - pu_w - tot_w - (unit_w if show_units else 0)
        name_max = 38 if show_units else 50  # only used with the switch off
        pdf.cell(desc_w, 6, 'DESCRIPCIÓN', 1, 0, 'L', True)
        if show_units:
            pdf.cell(unit_w, 6, unit_h, 1, 0, 'C', True)
        pdf.cell(qty_w, 6, qty_h, 1, 0, 'C', True)
        pdf.cell(pu_w, 6, pu_h, 1, 0, 'R', True)
        pdf.cell(tot_w, 6, tot_h, 1, 1, 'R', True)

        pdf.set_font('Arial', '', 7)
        pdf.set_text_color(30, 30, 30)
        row_color = True

        current_category = None
        for item in items:
            current_category = _category_heading(pdf, item, current_category, 190)
            qty = float(item.get('quantity') or 0)
            price = float(item.get('unit_price') or 0)
            subtotal = qty * price
            product_name = sanitize_text(item.get('product_name', 'Item'))
            if not pdf_options.desc_wrap_enabled():
                product_name = product_name[:name_max]

            if row_color:
                pdf.set_fill_color(252, 252, 252)
            else:
                pdf.set_fill_color(255, 255, 255)

            cells = []
            if show_units:
                cells.append((unit_w, sanitize_text(pdf_options.clean_unit(item.get('unit_of_measure')) or ''), 'C'))
            cells += [(qty_w, f'{qty:.2f}', 'C'), (pu_w, f'${price:,.2f}', 'R'), (tot_w, f'${subtotal:,.2f}', 'R')]
            item_row(pdf, desc_w, product_name, cells)

            row_color = not row_color

    pdf.ln(6)

    pdf.set_font('Arial', 'B', 9)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 5, sanitize_text('Resumen Financiero de la Cotización ' + str(phase_doc['quote_label'])) if phase_doc else 'Resumen Financiero', 0, 1, 'L')
    pdf.ln(2)

    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(60, 60, 60)
    summary_x = 120

    pdf.set_x(summary_x)
    pdf.cell(45, 4, 'Subtotal de Items:', 0, 0, 'L')
    pdf.set_text_color(30, 30, 30)
    pdf.cell(25, 4, f'${items_total:,.2f}', 0, 1, 'R')

    if total_discounts > 0:
        pdf.set_x(summary_x)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(45, 4, 'Total Descuentos:', 0, 0, 'L')
        pdf.set_text_color(200, 50, 50)
        pdf.cell(25, 4, f'-${total_discounts:,.2f}', 0, 1, 'R')

        pdf.set_x(summary_x)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(45, 4, 'Después de Descuentos:', 0, 0, 'L')
        pdf.set_text_color(30, 30, 30)
        pdf.cell(25, 4, f'${items_after_discount:,.2f}', 0, 1, 'R')
        pdf.ln(1)

    dynamic_lines = list(dynamic_charge_lines(charges, items_after_discount))
    if any(charges.get(k) for k in ('supervision', 'admin', 'insurance', 'transport', 'contingency')) or dynamic_lines:
        pdf.set_x(summary_x)
        pdf.set_font('Arial', 'B', 7)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(70, 5, 'Cargos Indirectos', 0, 1, 'L')
        pdf.set_font('Arial', '', 7)

    if charges.get('supervision'):
        pdf.set_x(summary_x)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(45, 4, f'Supervisión ({supervision_pct:.1f}%):', 0, 0, 'L')
        pdf.set_text_color(60, 60, 60)
        pdf.cell(25, 4, f'${supervision:,.2f}', 0, 1, 'R')

    if charges.get('admin'):
        pdf.set_x(summary_x)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(45, 4, f'Administración ({admin_pct:.1f}%):', 0, 0, 'L')
        pdf.set_text_color(60, 60, 60)
        pdf.cell(25, 4, f'${admin:,.2f}', 0, 1, 'R')

    if charges.get('insurance'):
        pdf.set_x(summary_x)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(45, 4, f'Seguro ({insurance_pct:.1f}%):', 0, 0, 'L')
        pdf.set_text_color(60, 60, 60)
        pdf.cell(25, 4, f'${insurance:,.2f}', 0, 1, 'R')

    if charges.get('transport'):
        pdf.set_x(summary_x)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(45, 4, f'Transporte ({transport_pct:.1f}%):', 0, 0, 'L')
        pdf.set_text_color(60, 60, 60)
        pdf.cell(25, 4, f'${transport:,.2f}', 0, 1, 'R')

    if charges.get('contingency'):
        pdf.set_x(summary_x)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(45, 4, f'Contingencia ({contingency_pct:.1f}%):', 0, 0, 'L')
        pdf.set_text_color(60, 60, 60)
        pdf.cell(25, 4, f'${contingency:,.2f}', 0, 1, 'R')

    # User-defined charges (none for legacy quotes). Same base and rule as totals.
    for dc in dynamic_lines:
        pdf.set_x(summary_x)
        pdf.set_text_color(100, 100, 100)
        label = sanitize_text(f"{dc['name'][:22]} ({dc['percentage']:.1f}%):")
        pdf.cell(45, 4, label, 0, 0, 'L')
        pdf.set_text_color(60, 60, 60)
        pdf.cell(25, 4, f"${dc['amount']:,.2f}", 0, 1, 'R')

    pdf.ln(2)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(summary_x, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(2)

    pdf.set_x(summary_x)
    pdf.set_font('Arial', 'B', 8)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(45, 5, 'Subtotal General:', 0, 0, 'L')
    pdf.cell(25, 5, f'${subtotal_general:,.2f}', 0, 1, 'R')

    pdf.set_x(summary_x)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(45, 4, f'{sanitize_text(tax_label or "ITBIS")}:', 0, 0, 'L')  # A1: the document's stored label
    pdf.set_text_color(30, 30, 30)
    pdf.cell(25, 4, f'${itbis:,.2f}', 0, 1, 'R')

    pdf.ln(1)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(summary_x, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(2)

    if phase_doc:
        # Phase invoice (switch ON): the quote's total is secondary; the phase block carries the dominant total.
        pdf.set_x(summary_x)
        pdf.set_font('Arial', 'B', 8)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(45, 6, sanitize_text('TOTAL COTIZACIÓN:'), 0, 0, 'L')
        pdf.cell(25, 6, f"${phase_doc['quote_total']:,.2f}", 0, 1, 'R')
        pdf.ln(6)
        _phase_block(pdf, phase_doc)
        pdf.ln(6)
    else:
        pdf.set_x(summary_x)
        pdf.set_font('Arial', 'B', 11)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(45, 7, 'TOTAL GENERAL:', 0, 0, 'L')
        pdf.cell(25, 7, f'${grand_total:,.2f}', 0, 1, 'R')

        pdf.ln(8)

    # ==================== PAYMENT SECTION (INVOICES ONLY) ====================
    if doc_type != 'COTIZACION':
        pdf.set_font("Arial", "B", 10)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 6, "Resumen de Pagos", 0, 1, "L")
        pdf.ln(2)

        pdf.set_font("Arial", "", 8)
        pdf.set_text_color(60, 60, 60)

        pdf.cell(40, 5, "Total Facturado:", 0, 0, "L")
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 5, f"${grand_total:,.2f}", 0, 1, "L")

        pdf.set_text_color(60, 60, 60)
        pdf.cell(40, 5, "Total Pagado:", 0, 0, "L")
        pdf.set_text_color(0, 140, 0)
        pdf.cell(0, 5, f"${amount_paid:,.2f}", 0, 1, "L")

        pdf.set_text_color(60, 60, 60)
        pdf.cell(40, 5, "Pendiente:", 0, 0, "L")
        pdf.set_text_color(200, 0, 0)
        pdf.cell(0, 5, f"${amount_due:,.2f}", 0, 1, "L")

        pdf.ln(8)

        pdf.set_font("Arial", "B", 10)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 6, "Historial de Pagos", 0, 1, "L")
        pdf.ln(2)

        if len(payments) == 0:
            pdf.set_font("Arial", "", 8)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(0, 5, "No hay pagos registrados.", 0, 1, "L")
        else:
            pdf.set_font("Arial", "B", 8)
            pdf.set_fill_color(245, 245, 245)
            pdf.set_draw_color(220, 220, 220)
            pdf.set_text_color(60, 60, 60)

            pdf.cell(40, 6, "Fecha", 1, 0, "L", True)
            pdf.cell(40, 6, "Método", 1, 0, "L", True)
            pdf.cell(40, 6, "Monto", 1, 0, "R", True)
            pdf.cell(70, 6, "Notas", 1, 1, "L", True)

            pdf.set_font("Arial", "", 8)
            pdf.set_text_color(30, 30, 30)

            for p in payments:
                pay_date = fmt_day(p.get("payment_date") or p.get("date") or "")
                pay_method = sanitize_text(str(p.get("method") or ""))
                pay_amount = float(p.get("amount") or 0)
                pay_notes = sanitize_text(str(p.get("notes") or ""))[:60]

                pdf.cell(40, 5, pay_date[:20], 1, 0, "L")
                pdf.cell(40, 5, pay_method[:20], 1, 0, "L")
                pdf.cell(40, 5, f"${pay_amount:,.2f}", 1, 0, "R")
                pdf.cell(70, 5, pay_notes, 1, 1, "L")

        pdf.ln(8)

    # ==================== NOTES SECTION ====================
    if notes and not note_in_cover:  # never lost: printed here unless the portada letter already carries it
        pdf.set_font("Arial", "B", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 6, "Notas", 0, 1, "L")
        pdf.ln(1)
        pdf.set_font("Arial", "", 8)
        pdf.set_text_color(80, 80, 80)
        pdf.multi_cell(0, 5, sanitize_text(str(notes)))
        pdf.ln(6)

    # ==================== SIGNATURES ====================
    add_footer_with_signature(pdf, signer_name, seal=seal)

    # Final page: Terms and Conditions (off until an admin enables it). Never blocks the document.
    try:
        from terms_page.service import get_active_sections
        terms = get_active_sections("quote" if doc_type == "COTIZACION" else "invoice")
        if terms:
            from pdf.builder_terms import add_terms_pages
            add_terms_pages(pdf, terms)
    except Exception:
        logger.exception("Terms page failed; continuing without it")

    pdf_bytes = pdf.output(dest="S")   # already bytearray
    return io.BytesIO(pdf_bytes)