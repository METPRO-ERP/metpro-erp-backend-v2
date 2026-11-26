"""Fiscal sequence activation and status (plan v2.1 section 2.7). Admin only.

Available to admins whether or not FISCAL_NCF_ENABLED is on: the batch is entered and checked BEFORE the
switch is turned on (activation order, plan section 7). Entering a batch does not turn the switch on.
"""
import json
import re
from typing import Optional

from psycopg2.extras import RealDictCursor

from core import fiscal_ncf
from core.dates import business_today
from core.errors import api_error, internal_error
from database import get_db_connection
from fastapi import HTTPException

_SERIES = re.compile(r"^[A-Z]$")


def _public(row: Optional[dict], today) -> dict:
    info = fiscal_ncf.evaluate(row, today)
    out = {
        "ncf_enabled": fiscal_ncf.fiscal_ncf_enabled(),
        "configured": bool(row),
        "state": info["state"],
        "remaining": info["remaining"],
        "days_to_expiry": info["days_to_expiry"],
        "warn_expiry": info["warn_expiry"],
        "warn_exhaustion": info["warn_exhaustion"],
        "warn_days": fiscal_ncf.WARN_DAYS,
        "warn_remaining": fiscal_ncf.WARN_REMAINING,
    }
    if row:
        out.update({
            "series": row["series"], "comprobante_type": row["comprobante_type"],
            "next_number": row["next_number"], "range_start": row["range_start"], "range_end": row["range_end"],
            "expiry_date": row["expiry_date"].isoformat() if row["expiry_date"] else None,
            "locked": row["locked"],
            "next_ncf": fiscal_ncf.format_ncf(row["series"], row["comprobante_type"], row["next_number"]) if row["next_number"] else None,
        })
    return out


def get_status() -> dict:
    conn = get_db_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        return _public(fiscal_ncf.fetch_active(cursor), business_today())
    finally:
        conn.close()


def _validate(body) -> None:
    if not _SERIES.match(body.series or ""):
        raise api_error(400, "La serie debe ser una letra mayúscula", "NCF_SERIES_INVALID")
    if body.next_number != body.next_number_confirm:
        raise api_error(400, "El próximo número y su confirmación no coinciden", "NCF_NEXT_NUMBER_MISMATCH")
    if body.range_start > body.range_end:
        raise api_error(400, "El rango no es válido: el inicio es mayor que el final", "NCF_RANGE_INVALID")
    if not (body.range_start <= body.next_number <= body.range_end):
        raise api_error(400, "El próximo número debe estar dentro del rango autorizado", "NCF_NEXT_OUT_OF_RANGE")
    if body.range_end > 99999999:
        raise api_error(400, "El número final excede 8 dígitos", "NCF_RANGE_INVALID")


def _preview(body) -> dict:
    return {
        "series": body.series, "comprobante_type": fiscal_ncf.COMPROBANTE_TYPE,
        "next_number": body.next_number, "range_start": body.range_start, "range_end": body.range_end,
        "expiry_date": body.expiry_date.isoformat(),
        "next_ncf": fiscal_ncf.format_ncf(body.series, fiscal_ncf.COMPROBANTE_TYPE, body.next_number),
        "last_ncf": fiscal_ncf.format_ncf(body.series, fiscal_ncf.COMPROBANTE_TYPE, body.range_end),
        "quantity": body.range_end - body.next_number + 1,
    }


def _values_json(body) -> str:
    return json.dumps({k: (v if k != "expiry_date" else v.isoformat()) for k, v in body.dict().items()
                       if k not in ("confirm", "next_number_confirm")})


def activate(body, actor_user_id: Optional[str]) -> dict:
    """First entry of the batch. confirm=false returns the confirmation screen data; confirm=true activates."""
    _validate(body)
    if not body.confirm:
        return {"confirmed": False, "batch": _preview(body)}
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        existing = fiscal_ncf.fetch_active(cursor, lock=True)
        if existing:
            code = "NCF_BATCH_LOCKED" if existing["locked"] else "NCF_BATCH_ACTIVE"
            msg = ("La secuencia ya emitió NCF y no se puede modificar" if existing["locked"]
                   else "Ya hay una secuencia activa. Use la corrección mientras no se haya emitido ningún NCF")
            raise api_error(400, msg, code)
        cursor.execute(
            "INSERT INTO fiscal_sequences (series, comprobante_type, next_number, range_start, range_end, expiry_date, "
            "active, activated_at, activated_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, true, now(), %s) RETURNING id",
            (body.series, fiscal_ncf.COMPROBANTE_TYPE, body.next_number, body.range_start, body.range_end,
             body.expiry_date, actor_user_id),
        )
        seq_id = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO fiscal_sequence_audit (fiscal_sequence_id, action, values_json, actor_user_id) VALUES (%s, 'activate', %s::jsonb, %s)",
            (seq_id, _values_json(body), actor_user_id),
        )
        conn.commit()
        return {"confirmed": True, "batch": _preview(body), "status": get_status()}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise internal_error("No se pudo activar la secuencia fiscal", e)
    finally:
        if conn:
            conn.close()


def correct(body, actor_user_id: Optional[str]) -> dict:
    """Correction of the active batch. Open ONLY while zero NCFs have been issued from it (permanent lock after)."""
    _validate(body)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        row = fiscal_ncf.fetch_active(cursor, lock=True)
        if not row:
            raise api_error(400, "No hay secuencia fiscal activa", "NCF_NO_ACTIVE_SEQUENCE")
        cursor.execute("SELECT 1 FROM invoices WHERE fiscal_sequence_id = %s LIMIT 1", (row["id"],))
        if row["locked"] or cursor.fetchone():
            raise api_error(400, "La secuencia ya emitió NCF y no se puede modificar", "NCF_BATCH_LOCKED")
        if not body.confirm:
            conn.rollback()
            return {"confirmed": False, "batch": _preview(body)}
        cursor.execute(
            "UPDATE fiscal_sequences SET series = %s, next_number = %s, range_start = %s, range_end = %s, expiry_date = %s WHERE id = %s",
            (body.series, body.next_number, body.range_start, body.range_end, body.expiry_date, row["id"]),
        )
        cursor.execute(
            "INSERT INTO fiscal_sequence_audit (fiscal_sequence_id, action, values_json, actor_user_id) VALUES (%s, 'correct', %s::jsonb, %s)",
            (row["id"], _values_json(body), actor_user_id),
        )
        conn.commit()
        return {"confirmed": True, "batch": _preview(body), "status": get_status()}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise internal_error("No se pudo corregir la secuencia fiscal", e)
    finally:
        if conn:
            conn.close()
