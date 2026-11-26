"""Fiscal sequence (NCF) rules. Plan v2.1 section 2. Series B, type 01 only.

Switch: FISCAL_NCF_ENABLED, default off. Read at request time.
Every function that touches the database takes the caller's cursor, so the assignment runs in the
same transaction as the invoice INSERT. No Postgres sequence (nextval): a rollback must return the number.
"""
from datetime import date, datetime, timezone
from typing import Optional

from core.errors import api_error
from core.switches import switch_on

COMPROBANTE_TYPE = "01"
WARN_DAYS = 30        # warn when this many days or fewer remain before expiry
WARN_REMAINING = 20   # warn when this many numbers or fewer remain


def fiscal_ncf_enabled() -> bool:
    return switch_on("FISCAL_NCF_ENABLED")


def format_ncf(series: str, comprobante_type: str, number: int) -> str:
    """B + 01 + 8 digits: B0100000319. Built from the stored number, never stored as free text."""
    return f"{series}{comprobante_type}{int(number):08d}"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def evaluate(row: Optional[dict], today: date) -> dict:
    """State and warnings of a sequence row. Pure: no database, no clock (today is passed in)."""
    if not row or not row.get("active"):
        return {"state": "inactive", "usable": False, "remaining": None, "days_to_expiry": None,
                "warn_expiry": False, "warn_exhaustion": False}
    nxt, end, expiry = row.get("next_number"), row.get("range_end"), _as_date(row.get("expiry_date"))
    if nxt is None or end is None or expiry is None or row.get("range_start") is None:
        return {"state": "incomplete", "usable": False, "remaining": None, "days_to_expiry": None,
                "warn_expiry": False, "warn_exhaustion": False}
    remaining = int(end) - int(nxt) + 1
    days = (expiry - today).days
    if days < 0:
        state = "expired"
    elif remaining <= 0:
        state = "exhausted"
    else:
        state = "ok"
    return {
        "state": state,
        "usable": state == "ok",
        "remaining": max(remaining, 0),
        "days_to_expiry": days,
        "warn_expiry": state == "ok" and days <= WARN_DAYS,
        "warn_exhaustion": state == "ok" and remaining <= WARN_REMAINING,
    }


_BLOCK = {
    "inactive": ("No hay secuencia fiscal activa", "NCF_NO_ACTIVE_SEQUENCE"),
    "incomplete": ("La secuencia fiscal no está completa. No se pueden emitir facturas", "NCF_SEQUENCE_INCOMPLETE"),
    "expired": ("La secuencia fiscal venció. No se pueden emitir facturas", "NCF_SEQUENCE_EXPIRED"),
    "exhausted": ("La secuencia fiscal se agotó. No se pueden emitir facturas", "NCF_SEQUENCE_EXHAUSTED"),
}


def require_usable(row: Optional[dict], today: date) -> dict:
    """Raise when the batch cannot issue a number (section 2.4). Returns the evaluation when it can."""
    info = evaluate(row, today)
    if not info["usable"]:
        message, code = _BLOCK[info["state"]]
        raise api_error(400, message, code)
    return info


def fetch_active(cursor, lock: bool = False) -> Optional[dict]:
    cursor.execute(
        "SELECT * FROM fiscal_sequences WHERE active AND comprobante_type = %s ORDER BY id LIMIT 1"
        + (" FOR UPDATE" if lock else ""),
        (COMPROBANTE_TYPE,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def precheck(cursor, today: date) -> None:
    """First of the two checks (section 2.4): before the conversion starts writing. No lock."""
    require_usable(fetch_active(cursor, lock=False), today)


def assign_ncf(cursor, today: date) -> dict:
    """Second check and the assignment, inside the conversion transaction.

    Lock order everywhere: quote row first, then this row. Locks the active row, re-checks, takes
    next_number, advances it and sets locked (the batch is then permanent). The caller writes the
    returned values on the invoice in the same transaction; a rollback gives the number back.
    """
    row = fetch_active(cursor, lock=True)
    info = require_usable(row, today)
    number = int(row["next_number"])
    ncf = format_ncf(row["series"], row["comprobante_type"], number)
    cursor.execute(
        "UPDATE fiscal_sequences SET next_number = next_number + 1, locked = true WHERE id = %s",
        (row["id"],),
    )
    return {
        "ncf": ncf,
        "fiscal_sequence_id": row["id"],
        "ncf_assigned_at": datetime.now(timezone.utc),
        "warn_expiry": info["warn_expiry"],
        "warn_exhaustion": info["warn_exhaustion"],
    }


def attach_to_invoice(cursor, invoice_id: int, assigned: dict) -> None:
    cursor.execute(
        "UPDATE invoices SET ncf = %s, fiscal_sequence_id = %s, ncf_assigned_at = %s WHERE id = %s",
        (assigned["ncf"], assigned["fiscal_sequence_id"], assigned["ncf_assigned_at"], invoice_id),
    )
