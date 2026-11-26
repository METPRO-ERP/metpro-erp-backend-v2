"""Phased invoicing (plan v2.1 section 3). Phases are created one at a time and each one is invoiced through
the single conversion path (quotes.service.convert_quote_to_invoice, PHASED branch).

Tax: F1 pro-rata. The phase ITBIS is computed once, here, when the phase is created, and STORED
(invoice_phases.itbis_snapshot). Nothing downstream recomputes it.
"""
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from core import phased_invoicing as pi
from core.errors import api_error, internal_error
from core.billing_errors import billing_error
from database import get_db_connection


def require_enabled() -> None:
    if not pi.phased_enabled():
        raise billing_error("FEATURE_DISABLED")


def _quote_money(cursor, quote: dict):
    import json
    from quotes.service import quote_grand_and_itbis
    cursor.execute("SELECT * FROM quote_items WHERE quote_id = %s ORDER BY id", (quote["quote_id"],))
    items = [dict(r) for r in cursor.fetchall()]
    raw = quote.get("included_charges")
    charges = json.loads(raw) if isinstance(raw, str) else (raw or {})
    grand, itbis = quote_grand_and_itbis(quote, items, charges)
    return pi.money(grand), pi.money(itbis)


def _committed(cursor, quote_id: str):
    """Phases that count against the balance: planned ones and those whose invoice is not cancelled."""
    cursor.execute(
        "SELECT p.amount_snapshot, p.itbis_snapshot, p.status FROM invoice_phases p "
        "LEFT JOIN invoices i ON i.id = p.invoice_id "
        "WHERE p.quote_id = %s AND (p.invoice_id IS NULL OR i.status <> 'Cancelled') ORDER BY p.phase_order",
        (quote_id,),
    )
    return cursor.fetchall()


def _aggregate(cursor, quote: dict, total: Decimal, itbis: Decimal) -> dict:
    """Section 3.5. Invoiced = non-cancelled phase invoices. Planned phases are not invoiced yet."""
    cursor.execute(
        "SELECT COALESCE(SUM(p.amount_snapshot), 0) AS invoiced, COALESCE(SUM(COALESCE(i.amount_paid, 0)), 0) AS paid "
        "FROM invoice_phases p JOIN invoices i ON i.id = p.invoice_id WHERE p.quote_id = %s AND i.status <> 'Cancelled'",
        (quote["quote_id"],),
    )
    row = cursor.fetchone()
    invoiced, paid = pi.money(row["invoiced"]), pi.money(row["paid"])
    committed = sum((pi.d(r["amount_snapshot"]) for r in _committed(cursor, quote["quote_id"])), Decimal(0))
    return {
        "total": float(total), "itbis": float(itbis),
        "invoiced_to_date": float(invoiced), "paid_to_date": float(paid),
        "pending_to_invoice": float(total - invoiced), "pending_to_pay": float(invoiced - paid),
        "remaining_for_new_phase": float(max(total - pi.money(committed), Decimal(0))),
    }


def _load_quote(cursor, quote_id: str, lock: bool = False) -> dict:
    cursor.execute(
        "SELECT *, COALESCE(to_jsonb(quotes)->>'billing_mode', 'FULL') AS billing_mode_v FROM quotes WHERE quote_id = %s"
        + (" FOR UPDATE" if lock else ""),
        (quote_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise api_error(404, "Cotización no encontrada", "QUOTE_NOT_FOUND")
    return dict(row)


def list_phases(quote_id: str) -> dict:
    conn = get_db_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        quote = _load_quote(cursor, quote_id)
        total, itbis = _quote_money(cursor, quote)
        cursor.execute(
            "SELECT p.*, i.invoice_number, i.status AS invoice_status, i.ncf, COALESCE(i.amount_paid, 0) AS amount_paid "
            "FROM invoice_phases p LEFT JOIN invoices i ON i.id = p.invoice_id WHERE p.quote_id = %s ORDER BY p.phase_order",
            (quote_id,),
        )
        phases = []
        for r in cursor.fetchall():
            r = dict(r)
            for k in ("amount_snapshot", "itbis_snapshot", "amount_input_value", "amount_paid"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
            r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
            phases.append(r)
        return {"quote_id": quote_id, "billing_mode": quote["billing_mode_v"], "quote_status": quote["status"],
                "phases": phases, "aggregate": _aggregate(cursor, quote, total, itbis)}
    finally:
        conn.close()


def create_phase(quote_id: str, body, actor_user_id: Optional[str]) -> dict:
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        quote = _load_quote(cursor, quote_id, lock=True)
        if quote["billing_mode_v"] != "PHASED":
            raise api_error(400, "La cotización no está en modo de facturación por fases", "BILLING_MODE_NOT_PHASED")
        if quote["status"] != "Approved":
            raise api_error(400, "Solo se pueden agregar fases a una cotización aprobada", "QUOTE_NOT_APPROVED")
        cursor.execute("SELECT 1 FROM invoice_phases WHERE quote_id = %s AND status = 'Planned' LIMIT 1", (quote_id,))
        if cursor.fetchone():
            raise api_error(400, "Ya hay una fase pendiente de facturar. Facture esa fase primero", "PHASE_PLANNED_EXISTS")

        total, itbis = _quote_money(cursor, quote)
        committed = _committed(cursor, quote_id)
        remaining = total - pi.money(sum((pi.d(r["amount_snapshot"]) for r in committed), Decimal(0)))
        if remaining <= 0:
            raise api_error(400, "No queda saldo por facturar", "PHASE_NOTHING_REMAINING")

        kind = body.amount_input_type
        if kind == "final":
            amount = remaining
        elif kind == "percent":
            if body.amount_input_value is None:
                raise api_error(400, "Indique el porcentaje", "PHASE_AMOUNT_REQUIRED")
            amount = pi.amount_from_percent(body.amount_input_value, total)
        else:
            if body.amount_input_value is None:
                raise api_error(400, "Indique el monto", "PHASE_AMOUNT_REQUIRED")
            amount = pi.money(body.amount_input_value)
        if amount <= 0:
            raise api_error(400, "El monto debe ser mayor que cero", "PHASE_AMOUNT_ZERO")
        if amount > remaining:
            raise api_error(400, "El monto supera el saldo pendiente", "PHASE_EXCEEDS_BALANCE")

        is_final = kind == "final" or amount == remaining  # an amount that closes the balance is the final phase
        phase_tax = pi.phase_itbis(amount, total, itbis, [r["itbis_snapshot"] for r in committed], is_final)
        cursor.execute("SELECT COALESCE(MAX(phase_order), 0) + 1 AS n FROM invoice_phases WHERE quote_id = %s", (quote_id,))
        order = cursor.fetchone()["n"]
        cursor.execute(
            "INSERT INTO invoice_phases (quote_id, phase_order, label, amount_input_type, amount_input_value, "
            "amount_snapshot, itbis_snapshot, note, created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (quote_id, order, body.label.strip(), kind, body.amount_input_value if kind != "final" else None,
             amount, phase_tax, body.note, actor_user_id),
        )
        phase_id = cursor.fetchone()["id"]
        conn.commit()
        return {"id": phase_id, "phase_order": order, "label": body.label.strip(), "amount": float(amount),
                "itbis": float(phase_tax), "base": float(amount - phase_tax), "is_final": is_final,
                "remaining_after": float(remaining - amount)}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise internal_error("No se pudo crear la fase", e)
    finally:
        if conn:
            conn.close()


def generate_invoice(phase_id: int, actor_user_id: Optional[str]) -> dict:
    """Invoice one phase through the single conversion path."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT quote_id FROM invoice_phases WHERE id = %s", (phase_id,))
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        raise billing_error("PHASE_NOT_FOUND")
    from quotes.service import convert_quote_to_invoice
    return convert_quote_to_invoice(row["quote_id"], actor_user_id, billing_mode="PHASED", phase_id=phase_id)
