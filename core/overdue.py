"""R2: read-only overdue view. Plan: claude/four-items-scoping-plan-2026-11-27.md section 3 (D-A to D-E, D-N).

Rides PAYMENT_DUE_ENABLED (no new switch): off = the route answers 404, nothing here runs.
Writes NOTHING: one SELECT, no status change, no audit row, no column touched.

The overdue test, exactly as coded (D-C):
    status IN ('Pending', 'Overdue')                       open: never Paid, never Cancelled
    AND due_date IS NOT NULL                               a stored due date (B2 snapshot or audited admin edit)
    AND due_date < today                                   today = business calendar date (America/Santo_Domingo)
    AND COALESCE(amount_due, total_amount) > 0             a balance remains
The due date itself is not overdue. Each invoice ages on its OWN due_date, so a phase invoice is aged individually (D6).
Planned phases are not invoices and never appear.

"Sin plazo" (D-C3): open invoices (same status and balance test) with NO due_date. Not overdue by computation. Sorted by
days since the invoice date. A stored status 'Overdue' without a due date carries marked_overdue = true.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from core.dates import business_today, business_tz

OPEN_STATUSES = ("Pending", "Overdue")
BUCKETS = (("1-30", "1 a 30 días", 1, 30), ("31-60", "31 a 60 días", 31, 60),
           ("61-90", "61 a 90 días", 61, 90), ("90+", "Más de 90 días", 91, None))

_SQL = """
    SELECT i.id,
           i.invoice_number,
           i.invoice_date,
           i.client_id,
           c.company_name AS client_name,
           i.total_amount,
           COALESCE(i.amount_paid, 0) AS amount_paid,
           COALESCE(i.amount_due, i.total_amount) AS amount_due,
           i.status,
           (to_jsonb(i)->>'due_date')::date AS due_date,
           (to_jsonb(i)->>'phase_id') IS NOT NULL AS is_phase
    FROM invoices i
    JOIN clients c ON c.id = i.client_id
    WHERE i.status IN ('Pending', 'Overdue')
      AND COALESCE(i.amount_due, i.total_amount) > 0
"""


def bucket_key(days_past_due: int) -> Optional[str]:
    """Bucket for a count of days past due (>= 1). 30 stays in 1-30, 31 opens 31-60, 90 stays in 61-90, 91 opens 90+."""
    for key, _label, low, high in BUCKETS:
        if days_past_due >= low and (high is None or days_past_due <= high):
            return key
    return None


def _day(value) -> Optional[date]:
    """A calendar date. A timestamp is read in the business time zone (invoice_date is timestamptz)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(business_tz())
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _row(raw: dict, today: date) -> dict:
    issued = _day(raw["invoice_date"])
    due = _day(raw.get("due_date"))
    return {
        "id": raw["id"],
        "invoice_number": raw["invoice_number"],
        "client_id": raw["client_id"],
        "client_name": raw["client_name"],
        "invoice_date": issued.isoformat() if issued else None,
        "due_date": due.isoformat() if due else None,
        "days_past_due": (today - due).days if due else None,
        "days_since_issue": (today - issued).days if issued else None,
        "total_amount": _money(raw["total_amount"]),
        "amount_paid": _money(raw["amount_paid"]),
        "amount_due": _money(raw["amount_due"]),
        "status": raw["status"],
        "is_phase": bool(raw.get("is_phase")),
    }


def build_report(rows: List[dict], today: date) -> dict:
    """Pure: raw open-invoice rows in, the report out. Sorting and bucketing live here so the tests need no database."""
    overdue, sin_plazo = [], []
    for raw in rows:
        if raw["status"] not in OPEN_STATUSES or Decimal(str(raw["amount_due"] or 0)) <= 0:
            continue                                            # defensive: the SQL already filters these
        row = _row(raw, today)
        if row["due_date"] is None:
            row["marked_overdue"] = raw["status"] == "Overdue"
            sin_plazo.append(row)
        elif row["days_past_due"] >= 1:
            row["bucket"] = bucket_key(row["days_past_due"])
            overdue.append(row)
        # due today or later: open but not overdue, not part of this view
    overdue.sort(key=lambda r: (-r["days_past_due"], -r["amount_due"], r["id"]))
    sin_plazo.sort(key=lambda r: (-(r["days_since_issue"] or 0), -r["amount_due"], r["id"]))

    buckets = []
    for key, label, _low, _high in BUCKETS:
        members = [r for r in overdue if r["bucket"] == key]
        buckets.append({"key": key, "label": label, "count": len(members),
                        "amount_due": round(sum(r["amount_due"] for r in members), 2)})
    return {
        "as_of": today.isoformat(),
        "overdue": {"count": len(overdue), "amount_due": round(sum(r["amount_due"] for r in overdue), 2),
                    "buckets": buckets, "rows": overdue},
        "sin_plazo": {"count": len(sin_plazo), "amount_due": round(sum(r["amount_due"] for r in sin_plazo), 2),
                      "rows": sin_plazo},
    }


def fetch_report(cursor, today: Optional[date] = None) -> dict:
    """One SELECT. Never writes."""
    today = today or business_today()
    cursor.execute(_SQL)
    return build_report([dict(r) for r in cursor.fetchall()], today)
