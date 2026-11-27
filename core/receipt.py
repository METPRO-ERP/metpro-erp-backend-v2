"""R3 recibo de pago. Plan: claude/four-items-scoping-plan-2026-11-27.md section R3.

Switch: RECEIPT_ENABLED, default off. Off = the receipt route answers 404 and the screens hide the button.
The snapshot columns (invoice_payments.paid_so_far_after / pending_after) are written ALWAYS, switch on or off
(decision D-Y), inside the record-payment INSERT. That write has no switch and changes no response.

Rules:
- Receipt number: REC-<payment id, at least 6 digits>. Stable (derived from the payment id, nothing to allocate)
  and it cannot be mistaken for an NCF (an NCF is a letter and two digits, B01, followed by 8 digits).
- The receipt prints what is stored: the payment row and its two snapshot figures. Nothing is recomputed.
- No receipt for a payment recorded before R3 (no snapshot, decision D-X): 409.
- No receipt for a voided payment (decision D-Z): 409. The stored figures stay in the row.
- Admin only (admin and superadmin), same roles as void and correct.
- It is an internal receipt, not a comprobante fiscal, and the PDF says so.
- Fiscal isolation: this module reads invoice, client and NCF columns and writes nothing.
"""
from psycopg2.extras import RealDictCursor

from core.billing_errors import billing_error
from core.switches import switch_on


def enabled() -> bool:
    return switch_on("RECEIPT_ENABLED")


def require_enabled() -> None:
    if not enabled():
        raise billing_error("FEATURE_DISABLED")


def receipt_number(payment_id: int) -> str:
    return f"REC-{int(payment_id):06d}"


def load_receipt(conn, invoice_id: int, payment_id: int) -> dict:
    """Everything the recibo prints, read from stored columns. Raises the billing errors for the refusals."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT p.id, p.invoice_id, p.amount, p.method, p.notes, p.payment_date, p.voided_at,
                   p.paid_so_far_after, p.pending_after,
                   (SELECT COALESCE(u.full_name, u.username) FROM users u WHERE u.id = p.created_by_user_id) AS created_by_name
            FROM invoice_payments p WHERE p.invoice_id = %s AND p.id = %s
            """,
            (invoice_id, payment_id),
        )
        pay = cur.fetchone()
        if not pay:
            raise billing_error("RECEIPT_PAYMENT_NOT_FOUND")
        if pay["voided_at"] is not None:
            raise billing_error("RECEIPT_PAYMENT_VOIDED")
        if pay["paid_so_far_after"] is None or pay["pending_after"] is None:
            raise billing_error("RECEIPT_NO_SNAPSHOT")
        cur.execute(
            """
            SELECT i.id, i.invoice_number, i.total_amount, to_jsonb(i)->>'ncf' AS ncf,
                   c.company_name, c.tax_id AS company_tax_id
            FROM invoices i LEFT JOIN clients c ON c.id = i.client_id WHERE i.id = %s
            """,
            (invoice_id,),
        )
        inv = cur.fetchone()
        if not inv:
            raise billing_error("RECEIPT_PAYMENT_NOT_FOUND")
    return {"payment": dict(pay), "invoice": dict(inv), "number": receipt_number(pay["id"])}
