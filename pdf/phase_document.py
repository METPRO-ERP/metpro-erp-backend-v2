"""Phase invoice document context (PHASE_INVOICE_LAYOUT_ENABLED). Read-only.

A phase invoice stores one summary line (the phase label at the phase base). With the layout switch ON its PDF
prints the source quote's lines and summary instead, plus a "Facturación por fases" block. This module only
LOADS what to print. It never computes tax and never writes:

- quote lines and the quote summary: quote_items and quote_document_totals(), the same source and function the
  quote PDF uses, so the numbers equal the quote's;
- the phase figures (base, tax, total): the invoice's stored columns (subtotal, itbis_total, total_amount);
- accumulated / pending: this phase plus earlier phase invoices of the same quote that are not cancelled.

invoices/service.py invoice_lines_and_totals (used by the conduce) is not touched.
"""
import json
import logging
from decimal import Decimal

from psycopg2.extras import RealDictCursor

from core import phased_invoicing as pi
from core.numbering import quote_label_for
from database import get_db_connection

logger = logging.getLogger(__name__)


def _money(value) -> float:
    return float(pi.money(value))


def _tax_word(quote: dict, label) -> str:
    """"ITBIS" for the ITBIS tax type, "Impuesto" for any other type (the 1.8% construction type)."""
    code = quote.get("tax_type_code")
    if code in (None, "itbis_18") or str(label or "").upper().startswith("ITBIS"):
        return "ITBIS"
    return "Impuesto"


def phase_document_override(invoice: dict) -> dict:
    """Keyword overrides for the invoice PDF builder, or {} (today's layout) when the context cannot be loaded.

    invoice: any invoice dict that has id, quote_id, phase_id, subtotal, itbis_total, total_amount.
    grand_total is NOT overridden: Resumen de Pagos keeps the invoice's own total."""
    from quotes.service import quote_document_totals  # local: avoids an import cycle
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM quotes WHERE quote_id = %s", (invoice["quote_id"],))
        quote = cursor.fetchone()
        cursor.execute("SELECT * FROM invoice_phases WHERE id = %s", (invoice["phase_id"],))
        phase = cursor.fetchone()
        if not quote or not phase:
            logger.warning("Phase layout: quote or phase missing for invoice %s; printing the old layout", invoice.get("id"))
            return {}
        quote, phase = dict(quote), dict(phase)
        cursor.execute("SELECT * FROM quote_items WHERE quote_id = %s ORDER BY id", (quote["quote_id"],))
        items = [dict(r) for r in cursor.fetchall()]
        raw = quote.get("included_charges")
        if not raw:
            charges = {}
        elif isinstance(raw, str):
            try:
                charges = json.loads(raw)
            except Exception:
                charges = {}
        else:
            charges = raw
        t = quote_document_totals(quote, items, charges)

        # Accumulated: this phase + earlier phase invoices of this quote that are not cancelled. The phase's own
        # amount always counts ("incl. esta fase"), even when this invoice is cancelled.
        cursor.execute(
            "SELECT COALESCE(SUM(p.amount_snapshot), 0) AS earlier FROM invoice_phases p "
            "JOIN invoices i ON i.id = p.invoice_id "
            "WHERE p.quote_id = %s AND p.phase_order < %s AND i.status <> 'Cancelled'",
            (quote["quote_id"], phase["phase_order"]),
        )
        earlier = pi.money(cursor.fetchone()["earlier"])
        cumulative = earlier + pi.money(phase["amount_snapshot"])
        quote_total = pi.money(t["grand_total"])

        label = t.get("tax_label")
        phase_doc = {
            "quote_label": quote_label_for(quote),
            "number": phase["phase_order"],
            "label": phase["label"],
            "input_type": phase["amount_input_type"],
            "input_value": None if phase["amount_input_value"] is None else float(phase["amount_input_value"]),
            "base": _money(invoice.get("subtotal")),
            "itbis": _money(invoice.get("itbis_total")),
            "total": _money(invoice.get("total_amount")),
            "quote_total": float(quote_total),
            "cumulative": float(cumulative),
            "pending": float(quote_total - cumulative),
            "tax_word": _tax_word(quote, label),
        }
        return {
            "items": items,
            "charges": charges,
            "items_total": t["items_total"],
            "total_discounts": t["total_discounts"],
            "items_after_discount": t["items_after_discount"],
            "supervision": t["supervision"], "supervision_pct": float(charges.get("supervision_percentage", 10.0)),
            "admin": t["admin"], "admin_pct": float(charges.get("admin_percentage", 4.0)),
            "insurance": t["insurance"], "insurance_pct": float(charges.get("insurance_percentage", 1.0)),
            "transport": t["transport"], "transport_pct": float(charges.get("transport_percentage", 3.0)),
            "contingency": t["contingency"], "contingency_pct": float(charges.get("contingency_percentage", 3.0)),
            "subtotal_general": t["subtotal_general"],
            "itbis": t["itbis"],
            "tax_label": label,
            "print_items": quote.get("print_items", True) is not False,  # the quote's choice (phase invoices never set their own)
            "phase_doc": phase_doc,
        }
    except Exception:
        logger.exception("Phase layout failed for invoice %s; printing the old layout", invoice.get("id"))
        return {}
    finally:
        if conn:
            conn.close()


def phase_layout_override(invoice: dict) -> dict:
    """{} unless the switch is ON and this is a phase invoice. Safe to call from every invoice PDF path."""
    from core import pdf_options
    if invoice.get("phase_id") and pdf_options.phase_layout_enabled():
        return phase_document_override(invoice)
    return {}
