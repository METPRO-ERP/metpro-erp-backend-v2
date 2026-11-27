"""Payment terms days and invoice due date. Plan: claude/workstream-bc-plan-2026-11-26.md section 4.3 (D1 to D7, D11).

Switch: PAYMENT_DUE_ENABLED, default off. Off = nothing here runs: no column is read or written, the invoice and quote
PDFs print exactly as before, the new route answers 404.

Rules (on):
- quotes.credit_days (0..365, NULL = no structured term) is set on the quote. It is entry data only.
- invoices.due_date is a STORED snapshot written ONCE, in the conversion transaction (FULL) or the phase-invoice
  transaction (D6): the invoice's own issue date + the quote's credit_days. Never derived at print or read time, and a
  later edit of the quote never moves an existing invoice.
- After conversion only an admin may change it, with a mandatory note, and every change appends a row to
  invoice_due_date_audit (old -> new, actor, time). Never on a Paid or Cancelled invoice. No silent edits.
- Fiscal isolation: no NCF, tax, numbering, total or payment column is read or written by anything in this module.

Every SQL statement here runs only when the switch is on, so the code works before the migration is applied
(the migration is applied before the switch is turned on).
"""
from datetime import date, datetime, timedelta
from typing import Optional

from core.billing_errors import billing_error
from core.switches import switch_on

CREDIT_DAYS_MAX = 365
NOTE_MAX = 500
LOCKED_STATUSES = ("Paid", "Cancelled")


def enabled() -> bool:
    return switch_on("PAYMENT_DUE_ENABLED")


def require_enabled() -> None:
    if not enabled():
        raise billing_error("FEATURE_DISABLED")


def clean_credit_days(value) -> Optional[int]:
    """None stays None. A whole number 0..365 is returned as int. Anything else is a ValueError (the model already
    bounds the field; this is the second check on the service side)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("credit_days must be a whole number")
    try:
        days = int(str(value).strip())
    except ValueError:
        raise ValueError("credit_days must be a whole number")
    if not 0 <= days <= CREDIT_DAYS_MAX:
        raise ValueError(f"credit_days must be between 0 and {CREDIT_DAYS_MAX}")
    return days


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def compute_due_date(issue_date, credit_days: Optional[int]) -> Optional[date]:
    """issue date + credit days; None when the quote has no structured term."""
    if credit_days is None:
        return None
    return _as_date(issue_date) + timedelta(days=int(credit_days))


# ------------------------------------------------------------------ quote side (entry)

def read_credit_days(cursor, quote_id: str) -> Optional[int]:
    cursor.execute("SELECT credit_days FROM quotes WHERE quote_id = %s", (quote_id,))
    row = cursor.fetchone()
    return None if not row else row["credit_days"]


def write_credit_days(cursor, quote_id: str, credit_days: Optional[int]) -> None:
    """Switch on only (the caller checks). None clears the term."""
    cursor.execute("UPDATE quotes SET credit_days = %s WHERE quote_id = %s", (clean_credit_days(credit_days), quote_id))


def copy_credit_days(cursor, from_quote_id: str, to_quote_id: str) -> None:
    """Duplicate quote keeps the term (same rule as payment_terms)."""
    cursor.execute(
        "UPDATE quotes SET credit_days = (SELECT credit_days FROM quotes WHERE quote_id = %s) WHERE quote_id = %s",
        (from_quote_id, to_quote_id),
    )


# ------------------------------------------------------------------ invoice side (snapshot)

def snapshot_due_date(cursor, invoice_id: int, quote_id: str, issue_date) -> Optional[date]:
    """Write invoices.due_date once, inside the caller's transaction. No-op when the switch is off or the quote has no
    credit_days (the invoice then keeps a NULL due date and nothing prints). Returns the stored date or None."""
    if not enabled():
        return None
    due = compute_due_date(issue_date, read_credit_days(cursor, quote_id))
    if due is None:
        return None
    cursor.execute("UPDATE invoices SET due_date = %s WHERE id = %s AND due_date IS NULL", (due, invoice_id))
    return due


def due_date_select_sql(alias: str = "i") -> str:
    """Column expression for a SELECT that must also work before the migration (reads through to_jsonb)."""
    return f"(to_jsonb({alias})->>'due_date')::date AS due_date"


def visible_due_date(invoice: dict) -> Optional[date]:
    """The stored value, shown only while the switch is on (off = today's output and payloads)."""
    if not enabled():
        return None
    value = invoice.get("due_date")
    return _as_date(value) if value else None


def pdf_kwargs(invoice: dict) -> dict:
    """{} unless the switch is on. Safe to call from every invoice PDF path (same pattern as phase_layout_override)."""
    due = visible_due_date(invoice)
    return {"due_date": due} if due else {}


# ------------------------------------------------------------------ admin edit (audited)

def edit_due_date(conn, invoice_id: int, new_due_date, note: Optional[str], actor_user_id: Optional[str]) -> dict:
    """Admin only (the route checks the role). One transaction: lock the invoice, validate, update, append the audit row.
    The caller commits or rolls back."""
    require_enabled()
    note = (note or "").strip()
    if not note:
        raise billing_error("DUE_DATE_NOTE_REQUIRED")
    note = note[:NOTE_MAX]
    new_date = _as_date(new_due_date)

    from psycopg2.extras import RealDictCursor
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, status, invoice_date, due_date FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,))
    row = cursor.fetchone()
    if not row:
        from core.errors import api_error
        raise api_error(404, "Factura no encontrada", "INVOICE_NOT_FOUND")
    if row["status"] in LOCKED_STATUSES:
        raise billing_error("INVOICE_DUE_DATE_LOCKED")
    old = row["due_date"]
    if old is not None and old == new_date:
        raise billing_error("DUE_DATE_UNCHANGED")
    if new_date < _as_date(row["invoice_date"]):
        raise billing_error("DUE_DATE_BEFORE_INVOICE")

    cursor.execute("UPDATE invoices SET due_date = %s WHERE id = %s", (new_date, invoice_id))
    cursor.execute(
        "INSERT INTO invoice_due_date_audit (invoice_id, old_due_date, new_due_date, note, changed_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (invoice_id, old, new_date, note, actor_user_id or None),
    )
    return {"invoice_id": invoice_id, "old_due_date": old, "new_due_date": new_date, "note": note}


def list_audit(cursor, invoice_id: int) -> list:
    cursor.execute(
        "SELECT a.id, a.old_due_date, a.new_due_date, a.note, a.changed_at, "
        "       COALESCE(u.full_name, u.username) AS changed_by_name "
        "FROM invoice_due_date_audit a LEFT JOIN users u ON u.id = a.changed_by "
        "WHERE a.invoice_id = %s ORDER BY a.id",
        (invoice_id,),
    )
    return [dict(r) for r in cursor.fetchall()]
