"""Conduce delivery ledger (plan: claude/conduce-module-plan-2026-11-26.md).

A conduce is the delivery note for the items actually delivered, drawn from the QUOTE's lines (Rule 0). Cumulative and
pending are derived by SUM over non-voided conduce_lines; nothing stores a running total. Over-delivery is blocked
inside the creation transaction under a lock on the quote row. This module reads quote_items and writes only
conduces and conduce_lines: no NCF, tax, invoice or quote money is read or written.
"""
import logging
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from core import conduce_ledger as cl
from core.billing_errors import billing_error
from core.dates import business_today
from core.errors import api_error, internal_error
from database import get_db_connection

logger = logging.getLogger(__name__)


def require_enabled() -> None:
    if not cl.ledger_enabled():
        raise billing_error("FEATURE_DISABLED")


def _fmt(q) -> str:
    return f"{Decimal(str(q)):.2f}"


def _quote(cursor, quote_id: str, lock: bool = False) -> dict:
    cursor.execute("SELECT * FROM quotes WHERE quote_id = %s" + (" FOR UPDATE" if lock else ""), (quote_id,))
    row = cursor.fetchone()
    if not row:
        raise api_error(404, "Cotización no encontrada", "QUOTE_NOT_FOUND")
    return dict(row)


def _invoice(cursor, invoice_id: int) -> dict:
    cursor.execute("SELECT id, invoice_number, quote_id, status FROM invoices WHERE id = %s", (invoice_id,))
    row = cursor.fetchone()
    if not row:
        raise api_error(404, "Factura no encontrada", "INVOICE_NOT_FOUND")
    return dict(row)


def _deliverable(cursor, quote_id: str) -> list:
    cursor.execute("SELECT * FROM quote_items WHERE quote_id = %s ORDER BY id", (quote_id,))
    return [dict(r) for r in cursor.fetchall() if cl.is_deliverable(dict(r))]


def _delivered(cursor, quote_id: str, up_to_seq: Optional[int] = None) -> dict:
    sql = ("SELECT l.quote_item_id, SUM(l.quantity) AS qty FROM conduce_lines l JOIN conduces c ON c.id = l.conduce_id "
           "WHERE c.quote_id = %s AND c.voided_at IS NULL")
    params = [quote_id]
    if up_to_seq is not None:
        sql += " AND c.delivery_seq <= %s"
        params.append(up_to_seq)
    cursor.execute(sql + " GROUP BY l.quote_item_id", params)
    return {r["quote_item_id"]: Decimal(str(r["qty"])) for r in cursor.fetchall()}


def _state(cursor, quote_id: str) -> dict:
    quote = _quote(cursor, quote_id)
    items = _deliverable(cursor, quote_id)
    delivered = _delivered(cursor, quote_id)
    lines = []
    for it in items:
        quoted = Decimal(str(it["quantity"] or 0))
        done = delivered.get(it["id"], Decimal(0))
        lines.append({"quote_item_id": it["id"], "product_name": it.get("product_name") or "Item",
                      "unit_of_measure": it.get("unit_of_measure"), "category": it.get("category"),
                      "quoted": float(quoted), "delivered": float(done), "pending": float(max(quoted - done, Decimal(0)))})
    cursor.execute(
        "SELECT c.id, c.conduce_number, c.delivery_seq, c.delivery_date, c.created_at, c.created_by, c.notes, c.invoice_id, "
        "c.voided_at, c.void_reason, (SELECT COUNT(*) FROM conduce_lines l WHERE l.conduce_id = c.id) AS line_count "
        "FROM conduces c WHERE c.quote_id = %s ORDER BY c.delivery_seq", (quote_id,))
    conduces = [{"id": str(r["id"]), "conduce_number": r["conduce_number"], "delivery_seq": r["delivery_seq"],
                 "delivery_date": r["delivery_date"].isoformat() if r["delivery_date"] else None,
                 "created_by": r["created_by"], "notes": r["notes"], "invoice_id": r["invoice_id"],
                 "voided": r["voided_at"] is not None, "void_reason": r["void_reason"], "line_count": r["line_count"]}
                for r in cursor.fetchall()]
    cursor.execute(
        "SELECT c.conduce_number, c.created_at FROM conduces c JOIN invoices i ON i.id = c.invoice_id "
        "WHERE i.quote_id = %s AND c.delivery_seq IS NULL ORDER BY c.created_at", (quote_id,))
    legacy = [{"conduce_number": r["conduce_number"], "created_at": r["created_at"].isoformat() if r["created_at"] else None}
              for r in cursor.fetchall()]
    return {"quote_id": quote_id, "state": cl.state_of(lines), "lines": lines, "conduces": conduces, "legacy_conduces": legacy}


def quote_state(quote_id: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        return _state(conn.cursor(cursor_factory=RealDictCursor), quote_id)
    except HTTPException:
        raise
    except Exception as e:
        raise internal_error("No se pudo leer el estado de entrega", e)
    finally:
        if conn:
            conn.close()


def invoice_state(invoice_id: int) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        invoice = _invoice(cursor, invoice_id)
        out = _state(cursor, invoice["quote_id"])
        out.update({"invoice_id": invoice_id, "invoice_number": invoice["invoice_number"], "invoice_status": invoice["status"]})
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise internal_error("No se pudo leer el estado de entrega", e)
    finally:
        if conn:
            conn.close()


def create_conduce(body, actor: str) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        invoice = _invoice(cursor, body.invoice_id)
        if invoice["status"] == "Cancelled":
            raise billing_error("CONDUCE_INVOICE_CANCELLED")
        quote_id = invoice["quote_id"]
        quote = _quote(cursor, quote_id, lock=True)  # serializes deliveries of this quote

        items = {it["id"]: it for it in _deliverable(cursor, quote_id)}
        if not items:
            raise billing_error("CONDUCE_NO_DELIVERABLE_LINES")
        if not body.lines:
            raise billing_error("CONDUCE_NO_LINES")

        today = business_today()
        delivery_date = body.delivery_date or today
        if delivery_date > today:
            raise billing_error("CONDUCE_DATE_INVALID")

        delivered = _delivered(cursor, quote_id)
        seen, clean, over = set(), [], []
        for ln in body.lines:
            if ln.quote_item_id in seen:
                raise billing_error("CONDUCE_DUPLICATE_LINE")
            seen.add(ln.quote_item_id)
            item = items.get(ln.quote_item_id)
            if item is None:
                raise billing_error("CONDUCE_LINE_NOT_DELIVERABLE")
            qty = cl.parse_quantity(ln.quantity)
            if qty is None:
                raise billing_error("CONDUCE_QUANTITY_INVALID")
            quoted = Decimal(str(item["quantity"] or 0))
            pending = max(quoted - delivered.get(item["id"], Decimal(0)), Decimal(0))
            if qty > pending:
                over.append(f"{item.get('product_name') or 'Item'}: pendiente {_fmt(pending)}, solicitado {_fmt(qty)}")
            clean.append((item, qty, quoted))
        if over:
            raise api_error(409, "La cantidad excede lo pendiente de entregar. " + "; ".join(over), "CONDUCE_OVER_DELIVERY")

        cursor.execute("SELECT COALESCE(MAX(delivery_seq), 0) + 1 AS seq FROM conduces WHERE quote_id = %s", (quote_id,))
        seq = cursor.fetchone()["seq"]
        number = cl.conduce_number(quote, seq)
        cursor.execute(
            "INSERT INTO conduces (conduce_number, invoice_id, quote_number, quote_id, delivery_seq, delivery_date, notes, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (number, body.invoice_id, quote.get("quote_number"), quote_id, seq, delivery_date,
             (body.notes or "").strip() or None, actor))
        conduce_id = cursor.fetchone()["id"]
        for item, qty, quoted in clean:
            cursor.execute(
                "INSERT INTO conduce_lines (conduce_id, quote_item_id, quantity, product_name, unit_of_measure, category, quoted_quantity) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (conduce_id, item["id"], qty, item.get("product_name") or "Item", item.get("unit_of_measure"),
                 item.get("category"), quoted))
        state = _state(cursor, quote_id)
        conn.commit()
        return {"conduce_id": str(conduce_id), "conduce_number": number, "delivery_seq": seq,
                "state": state["state"], "message": f"Conduce {number} creado"}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("Conduce creation failed for invoice %s", getattr(body, "invoice_id", None))
        raise internal_error("No se pudo crear el conduce", e)
    finally:
        if conn:
            conn.close()


def void_conduce(conduce_id: str, reason: str, actor: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise billing_error("CONDUCE_VOID_REASON_REQUIRED")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT id, conduce_number, quote_id, delivery_seq, voided_at FROM conduces WHERE id::text = %s FOR UPDATE", (conduce_id,))
        row = cursor.fetchone()
        if not row or row["delivery_seq"] is None:
            raise billing_error("CONDUCE_NOT_FOUND")
        if row["voided_at"] is not None:
            raise billing_error("CONDUCE_ALREADY_VOID")
        _quote(cursor, row["quote_id"], lock=True)  # same lock as creation: no delivery slips between
        cursor.execute("UPDATE conduces SET voided_at = now(), voided_by = %s, void_reason = %s WHERE id = %s",
                       (actor, reason, row["id"]))
        state = _state(cursor, row["quote_id"])
        conn.commit()
        return {"conduce_number": row["conduce_number"], "state": state["state"], "message": f"Conduce {row['conduce_number']} anulado"}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise internal_error("No se pudo anular el conduce", e)
    finally:
        if conn:
            conn.close()


def conduce_pdf_data(conduce_id: str = None, latest_for_quote: str = None) -> dict:
    """Everything the ledger PDF prints. Cumulative = non-voided deliveries up to and including this one."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if conduce_id is not None:
            cursor.execute("SELECT * FROM conduces WHERE id::text = %s AND delivery_seq IS NOT NULL", (conduce_id,))
        else:
            cursor.execute("SELECT * FROM conduces WHERE quote_id = %s AND delivery_seq IS NOT NULL AND voided_at IS NULL "
                           "ORDER BY delivery_seq DESC LIMIT 1", (latest_for_quote,))
        c = cursor.fetchone()
        if not c:
            raise billing_error("CONDUCE_NOT_FOUND")
        c = dict(c)
        quote = _quote(cursor, c["quote_id"])
        cursor.execute("SELECT i.id, i.invoice_number, i.client_id, i.contact_id, "
                       "(SELECT COALESCE(u.full_name, u.username) FROM users u WHERE u.id = i.created_by_user_id) AS created_by_name "
                       "FROM invoices i WHERE i.id = %s", (c["invoice_id"],))
        invoice = dict(cursor.fetchone() or {})
        cursor.execute("SELECT * FROM clients WHERE id = %s", (quote["client_id"],))
        client = dict(cursor.fetchone() or {})
        contact = {}
        contact_id = invoice.get("contact_id") or quote.get("contact_id")
        if contact_id:
            cursor.execute("SELECT name FROM contacts WHERE id = %s", (contact_id,))
            contact = dict(cursor.fetchone() or {})
        cursor.execute("SELECT * FROM conduce_lines WHERE conduce_id = %s ORDER BY quote_item_id", (c["id"],))
        mine = [dict(r) for r in cursor.fetchall()]
        cumulative = _delivered(cursor, c["quote_id"], up_to_seq=c["delivery_seq"])
        everything = _deliverable(cursor, c["quote_id"])
        lines = []
        for r in mine:
            quoted = Decimal(str(r["quoted_quantity"]))
            cum = cumulative.get(r["quote_item_id"], Decimal(0))
            lines.append({"category": r["category"], "name": r["product_name"], "unit": r["unit_of_measure"],
                          "quoted": float(quoted), "now": float(r["quantity"]), "cum": float(cum)})
        complete = all(cumulative.get(it["id"], Decimal(0)) >= Decimal(str(it["quantity"] or 0)) for it in everything) if everything else False
        from core.numbering import quote_label_for
        return {
            "number": c["conduce_number"], "seq": c["delivery_seq"], "date": c["delivery_date"],
            "quote_label": quote_label_for(quote), "invoice_label": invoice.get("invoice_number") or "",
            "project_name": quote.get("project_name"), "notes": c.get("notes"),
            "client": {"company_name": client.get("company_name", ""), "address": client.get("address", ""),
                       "contact_name": contact.get("name", "")},
            "lines": lines, "marker": "VOID" if c["voided_at"] is not None else ("TOTAL" if complete else "PARTIAL"),
            "created_by_name": invoice.get("created_by_name"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise internal_error("No se pudo preparar el conduce", e)
    finally:
        if conn:
            conn.close()
