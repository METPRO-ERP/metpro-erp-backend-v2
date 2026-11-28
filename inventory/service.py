"""Inventory stock ledger, Phase 1 (plan: claude/inventory-module-plan-2026-11-28.md, decisions I-D2 to I-D16).

Append-only movements with weighted-average valuation per item and warehouse. Every posting runs in one transaction
that first locks the item rows (in id order, so two postings never deadlock), then reads Q = SUM(quantity) and
V = SUM(value) from the lines, so a second posting on the same item waits and sees the first one's result.
Issue values are computed once, at posting, and stored on the line. A void inserts reversal lines at the stored
values; nothing already written is recomputed or edited. This module reads quotes, projects and suppliers only to
validate a reference; it never writes anything outside the inventory tables.
"""
import logging
from decimal import Decimal
from typing import Optional

import psycopg2.errors
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from core import inventory as inv
from core.billing_errors import billing_error
from core.dates import business_today
from core.errors import api_error, internal_error
from database import get_db_connection

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
ADMIN_ROLES = ("admin", "superadmin")
VOIDABLE_KINDS = ("receipt", "issue_job", "adjustment")
# Unique-violation constraint name -> error code (a race that slipped past the pre-checks).
UNIQUE_CODES = {
    "inv_items_sku_key": "INVENTORY_SKU_EXISTS",
    "inv_lots_item_ref_key": "INVENTORY_LOT_EXISTS",
    "inv_movements_one_opening_key": "INVENTORY_OPENING_EXISTS",
    "inv_movements_one_reversal_key": "INVENTORY_ALREADY_VOID",
}


def require_enabled() -> None:
    if not inv.inventory_enabled():
        raise billing_error("FEATURE_DISABLED")


def is_admin(user: dict) -> bool:
    return user.get("role") in ADMIN_ROLES


def require_admin(user: dict) -> None:
    if not is_admin(user):
        raise api_error(403, "Permisos insuficientes", "INSUFFICIENT_PERMISSIONS")


def _f(d) -> Optional[float]:
    return None if d is None else float(d)


def _run(context: str, work, write: bool = True):
    """One connection, one transaction. `work(cursor)` returns the response."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        result = work(cursor)
        if write:
            conn.commit()
        else:
            conn.rollback()
        return result
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except psycopg2.errors.UniqueViolation as e:
        if conn:
            conn.rollback()
        code = UNIQUE_CODES.get(getattr(getattr(e, "diag", None), "constraint_name", None))
        if code:
            raise billing_error(code)
        logger.exception(context)
        raise internal_error(context, e)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(context)
        raise internal_error(context, e)
    finally:
        if conn:
            conn.close()


# ----------------------------------------------------------------------------------------------- shared steps

def _warehouse(cursor, warehouse_id: Optional[int]) -> int:
    if warehouse_id is None:
        cursor.execute("SELECT id FROM inv_warehouses WHERE is_active ORDER BY id")
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise billing_error("INVENTORY_WAREHOUSE_REQUIRED")
        return rows[0]["id"]
    cursor.execute("SELECT id FROM inv_warehouses WHERE id = %s AND is_active", (warehouse_id,))
    if not cursor.fetchone():
        raise billing_error("INVENTORY_WAREHOUSE_NOT_FOUND")
    return warehouse_id


def _date(value):
    today = business_today()
    d = value or today
    if d > today:
        raise billing_error("INVENTORY_DATE_INVALID")
    return d


def _lock_items(cursor, ids) -> dict:
    ids = sorted(set(ids))
    cursor.execute("SELECT * FROM inv_items WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (ids,))
    items = {r["id"]: dict(r) for r in cursor.fetchall()}
    for i in ids:
        if i not in items:
            raise billing_error("INVENTORY_ITEM_NOT_FOUND")
    return items


def _require_active(items: dict) -> None:
    for it in items.values():
        if not it["is_active"]:
            raise billing_error("INVENTORY_ITEM_INACTIVE")


def _stock(cursor, item_ids, warehouse_id: int) -> dict:
    """{item_id: [Q, V]} for one warehouse, read after the item locks are held."""
    state = {i: [ZERO, ZERO] for i in item_ids}
    cursor.execute(
        "SELECT l.item_id, SUM(l.quantity) AS q, SUM(l.value) AS v FROM inv_movement_lines l "
        "JOIN inv_movements m ON m.id = l.movement_id WHERE m.warehouse_id = %s AND l.item_id = ANY(%s) GROUP BY l.item_id",
        (warehouse_id, list(item_ids)))
    for r in cursor.fetchall():
        state[r["item_id"]] = [Decimal(str(r["q"])), Decimal(str(r["v"]))]
    return state


def _lot_fields(item: dict, lot_ref, weight_lb):
    """(lot_ref, weight) for a lot-tracked item; (None, None) for any other item. Wrong combinations are refused."""
    if not item["tracks_lots"]:
        if lot_ref is not None or weight_lb is not None:
            raise billing_error("INVENTORY_LOT_NOT_ALLOWED")
        return None, None
    weight = inv.parse_positive(weight_lb)
    if lot_ref is None or weight is None:
        raise billing_error("INVENTORY_LOT_REQUIRED")
    return lot_ref, weight


def _new_lot(cursor, item_id: int, lot_ref: str, weight: Decimal, quantity: Decimal, cost: Decimal, actor: str, seen: set) -> int:
    key = (item_id, lot_ref)
    if key in seen:
        raise billing_error("INVENTORY_LOT_EXISTS")
    cursor.execute("SELECT 1 FROM inv_lots WHERE item_id = %s AND lot_ref = %s", key)
    if cursor.fetchone():
        raise billing_error("INVENTORY_LOT_EXISTS")
    seen.add(key)
    cursor.execute(
        "INSERT INTO inv_lots (item_id, lot_ref, weight_lb, entry_quantity, entry_cost, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (item_id, lot_ref, weight, quantity, cost, actor))
    return cursor.fetchone()["id"]


def _insert_movement(cursor, header: dict, lines: list):
    cursor.execute(
        "INSERT INTO inv_movements (kind, warehouse_id, occurred_on, quote_id, project_id, source_type, source_id, "
        "supplier_id, supplier_ref, reason, note, reverses_movement_id, created_by) "
        "VALUES (%(kind)s, %(warehouse_id)s, %(occurred_on)s, %(quote_id)s, %(project_id)s, %(source_type)s, %(source_id)s, "
        "%(supplier_id)s, %(supplier_ref)s, %(reason)s, %(note)s, %(reverses_movement_id)s, %(created_by)s) "
        "RETURNING id, number",
        {"quote_id": None, "project_id": None, "source_type": "manual", "source_id": None, "supplier_id": None,
         "supplier_ref": None, "reason": None, "note": None, "reverses_movement_id": None, **header})
    row = cursor.fetchone()
    for pos, ln in enumerate(lines, start=1):
        cursor.execute(
            "INSERT INTO inv_movement_lines (movement_id, kind, position, item_id, lot_id, quantity, unit_cost, value) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (row["id"], header["kind"], pos, ln["item_id"], ln.get("lot_id"), ln["quantity"], ln["unit_cost"], ln["value"]))
    return row["id"], row["number"]


def _take(item: dict, state: dict, quantity: Decimal) -> Decimal:
    """Value (positive) taken out of the running state for an exit of `quantity`; refuses when stock is short."""
    q, v = state[item["id"]]
    if quantity > q:
        raise api_error(409, f"No hay existencia suficiente de {item['sku']}: disponible {q}, solicitado {quantity}",
                        "INVENTORY_INSUFFICIENT_STOCK")
    value = inv.issue_value(quantity, q, v)
    state[item["id"]] = [q - quantity, v - value]
    return value


def _put(item: dict, state: dict, quantity: Decimal, total: Decimal) -> None:
    q, v = state[item["id"]]
    state[item["id"]] = [q + quantity, v + total]


def _movement_out(cursor, movement_id: int, show_cost: bool) -> dict:
    cursor.execute("SELECT * FROM inv_movements WHERE id = %s", (movement_id,))
    m = cursor.fetchone()
    if not m:
        raise billing_error("INVENTORY_MOVEMENT_NOT_FOUND")
    cursor.execute(
        "SELECT l.position, l.item_id, i.sku, i.name, i.stock_unit, l.lot_id, t.lot_ref, t.weight_lb, l.quantity, l.unit_cost, l.value "
        "FROM inv_movement_lines l JOIN inv_items i ON i.id = l.item_id LEFT JOIN inv_lots t ON t.id = l.lot_id "
        "WHERE l.movement_id = %s ORDER BY l.position", (movement_id,))
    lines = []
    for r in cursor.fetchall():
        ln = {"position": r["position"], "item_id": r["item_id"], "sku": r["sku"], "name": r["name"], "stock_unit": r["stock_unit"],
              "lot_id": r["lot_id"], "lot_ref": r["lot_ref"], "weight_lb": _f(r["weight_lb"]), "quantity": _f(r["quantity"])}
        if show_cost:
            ln.update({"unit_cost": _f(r["unit_cost"]), "value": _f(r["value"])})
        lines.append(ln)
    return {"id": m["id"], "number": m["number"], "label": inv.movement_label(m["number"]), "kind": m["kind"],
            "warehouse_id": m["warehouse_id"], "occurred_on": m["occurred_on"].isoformat(), "quote_id": m["quote_id"],
            "project_id": m["project_id"], "source_type": m["source_type"], "source_id": m["source_id"],
            "supplier_id": m["supplier_id"], "supplier_ref": m["supplier_ref"], "reason": m["reason"], "note": m["note"],
            "reverses_movement_id": m["reverses_movement_id"], "created_by": m["created_by"],
            "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            "voided": m["voided_at"] is not None, "void_reason": m["void_reason"], "lines": lines}


# ----------------------------------------------------------------------------------------------- items

def _item_out(r: dict, show_cost: bool) -> dict:
    q = Decimal(str(r.get("on_hand") or 0))
    out = {"id": r["id"], "sku": r["sku"], "name": r["name"], "kind": r["kind"], "stock_unit": r["stock_unit"],
           "tracks_lots": r["tracks_lots"], "standard_length_ft": _f(r["standard_length_ft"]), "product_id": r["product_id"],
           "is_active": r["is_active"], "is_dummy": r["is_dummy"], "notes": r["notes"], "on_hand": _f(q)}
    if show_cost:
        v = Decimal(str(r.get("on_hand_value") or 0))
        out["on_hand_value"] = _f(v)
        out["average_cost"] = _f(inv.unit_cost(v, q)) if q > 0 else None
    return out


def _positive_length(value):
    if value is None:
        return None
    d = inv.parse_positive(value)
    if d is None:
        raise api_error(400, "La longitud estándar debe ser mayor que cero y tener máximo 2 decimales", "INVENTORY_LENGTH_INVALID")
    return d


def _check_product(cursor, product_id):
    if product_id is not None:
        cursor.execute("SELECT 1 FROM products WHERE id = %s", (product_id,))
        if not cursor.fetchone():
            raise billing_error("INVENTORY_PRODUCT_NOT_FOUND")


def create_item(body, actor: str) -> dict:
    def work(cur):
        if not body.sku or not body.name:
            raise api_error(400, "El código y el nombre son obligatorios", "INVENTORY_ITEM_FIELDS_REQUIRED")
        length = _positive_length(body.standard_length_ft)
        _check_product(cur, body.product_id)
        cur.execute("SELECT 1 FROM inv_items WHERE sku = %s", (body.sku,))
        if cur.fetchone():
            raise billing_error("INVENTORY_SKU_EXISTS")
        cur.execute(
            "INSERT INTO inv_items (sku, name, kind, stock_unit, tracks_lots, standard_length_ft, product_id, is_dummy, notes, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (body.sku, body.name, body.kind, body.stock_unit, body.tracks_lots, length, body.product_id, body.is_dummy,
             body.notes, actor))
        return _item_out({**dict(cur.fetchone()), "on_hand": 0, "on_hand_value": 0}, True)
    return _run("No se pudo crear el artículo", work)


def update_item(item_id: int, body, fields_set: set) -> dict:
    def work(cur):
        items = _lock_items(cur, [item_id])
        item = items[item_id]
        sets, params = [], []
        if "name" in fields_set:
            if not body.name:
                raise api_error(400, "El nombre es obligatorio", "INVENTORY_ITEM_FIELDS_REQUIRED")
            sets.append("name = %s"); params.append(body.name)
        if "notes" in fields_set:
            sets.append("notes = %s"); params.append((body.notes or None))
        if "standard_length_ft" in fields_set:
            sets.append("standard_length_ft = %s"); params.append(_positive_length(body.standard_length_ft))
        if "product_id" in fields_set:
            _check_product(cur, body.product_id)
            sets.append("product_id = %s"); params.append(body.product_id)
        if "is_active" in fields_set and body.is_active is not None:
            if body.is_active is False and item["is_active"]:
                cur.execute("SELECT COALESCE(SUM(quantity), 0) AS q FROM inv_movement_lines WHERE item_id = %s", (item_id,))
                if Decimal(str(cur.fetchone()["q"])) != 0:
                    raise billing_error("INVENTORY_ITEM_HAS_STOCK")
            sets.append("is_active = %s"); params.append(body.is_active)
        if sets:
            sets.append("updated_at = now()")
            cur.execute(f"UPDATE inv_items SET {', '.join(sets)} WHERE id = %s", params + [item_id])
        return item_detail(cur, item_id, True)
    return _run("No se pudo actualizar el artículo", work)


def list_items(user: dict, include_inactive: bool = False, warehouse_id: Optional[int] = None) -> dict:
    show_cost = is_admin(user)

    def work(cur):
        cur.execute(
            "SELECT i.*, COALESCE(s.q, 0) AS on_hand, COALESCE(s.v, 0) AS on_hand_value FROM inv_items i LEFT JOIN ("
            "SELECT l.item_id, SUM(l.quantity) AS q, SUM(l.value) AS v FROM inv_movement_lines l "
            "JOIN inv_movements m ON m.id = l.movement_id WHERE (%s::bigint IS NULL OR m.warehouse_id = %s) GROUP BY l.item_id"
            ") s ON s.item_id = i.id WHERE (%s OR i.is_active) ORDER BY i.sku", (warehouse_id, warehouse_id, include_inactive))
        rows = [dict(r) for r in cur.fetchall()]
        out = {"items": [_item_out(r, show_cost) for r in rows]}
        if show_cost:  # dummy items are out of valuation (I-D14)
            out["total_value"] = _f(sum((Decimal(str(r["on_hand_value"])) for r in rows if not r["is_dummy"]), ZERO))
        return out
    return _run("No se pudo leer el inventario", work, write=False)


def item_detail(cur, item_id: int, show_cost: bool) -> dict:
    cur.execute(
        "SELECT i.*, COALESCE(SUM(l.quantity), 0) AS on_hand, COALESCE(SUM(l.value), 0) AS on_hand_value FROM inv_items i "
        "LEFT JOIN inv_movement_lines l ON l.item_id = i.id WHERE i.id = %s GROUP BY i.id", (item_id,))
    r = cur.fetchone()
    if not r:
        raise billing_error("INVENTORY_ITEM_NOT_FOUND")
    out = _item_out(dict(r), show_cost)
    cur.execute(
        "SELECT w.id, w.code, w.name, COALESCE(SUM(l.quantity), 0) AS q, COALESCE(SUM(l.value), 0) AS v FROM inv_warehouses w "
        "LEFT JOIN inv_movements m ON m.warehouse_id = w.id LEFT JOIN inv_movement_lines l ON l.movement_id = m.id AND l.item_id = %s "
        "GROUP BY w.id ORDER BY w.id", (item_id,))
    out["warehouses"] = []
    for w in cur.fetchall():
        q, v = Decimal(str(w["q"])), Decimal(str(w["v"]))
        row = {"warehouse_id": w["id"], "code": w["code"], "name": w["name"], "on_hand": _f(q)}
        if show_cost:
            row.update({"on_hand_value": _f(v), "average_cost": _f(inv.unit_cost(v, q)) if q > 0 else None})
        out["warehouses"].append(row)
    cur.execute("SELECT id, lot_ref, weight_lb, entry_quantity, entry_cost, created_at FROM inv_lots WHERE item_id = %s ORDER BY id", (item_id,))
    out["lots"] = []
    for t in cur.fetchall():
        lot = {"id": t["id"], "lot_ref": t["lot_ref"], "weight_lb": _f(t["weight_lb"]), "entry_quantity": _f(t["entry_quantity"]),
               "created_at": t["created_at"].isoformat() if t["created_at"] else None}
        if show_cost:
            lot["entry_cost"] = _f(t["entry_cost"])
        out["lots"].append(lot)
    return out


def get_item(user: dict, item_id: int) -> dict:
    return _run("No se pudo leer el artículo", lambda cur: item_detail(cur, item_id, is_admin(user)), write=False)


def list_warehouses() -> dict:
    def work(cur):
        cur.execute("SELECT id, code, name, is_active FROM inv_warehouses ORDER BY id")
        return {"warehouses": [dict(r) for r in cur.fetchall()]}
    return _run("No se pudo leer los almacenes", work, write=False)


def options() -> dict:
    """Always answers. Off: {"enabled": false} with no database read."""
    if not inv.inventory_enabled():
        return {"enabled": False}
    def work(cur):
        cur.execute("SELECT count(*) AS n FROM inv_warehouses WHERE is_active")
        return {"enabled": True, "units": list(inv.UNITS), "kinds": list(inv.KINDS), "multi_warehouse": cur.fetchone()["n"] > 1}
    return _run("No se pudo leer las opciones", work, write=False)


# ----------------------------------------------------------------------------------------------- postings

def post_receipt(body, actor: str) -> dict:
    def work(cur):
        if not body.lines:
            raise billing_error("INVENTORY_NO_LINES")
        wh, day = _warehouse(cur, body.warehouse_id), _date(body.occurred_on)
        if body.supplier_id is not None:
            cur.execute("SELECT 1 FROM suppliers WHERE id = %s", (body.supplier_id,))
            if not cur.fetchone():
                raise billing_error("INVENTORY_SUPPLIER_NOT_FOUND")
        parsed = []
        for ln in body.lines:
            qty, cost = inv.parse_positive(ln.quantity), inv.parse_positive(ln.total_cost)
            if qty is None:
                raise billing_error("INVENTORY_QUANTITY_INVALID")
            if cost is None:
                raise billing_error("INVENTORY_COST_INVALID")
            parsed.append((ln, qty, cost))
        items = _lock_items(cur, [p[0].item_id for p in parsed])
        _require_active(items)
        lines, seen = [], set()
        for ln, qty, cost in parsed:
            item = items[ln.item_id]
            lot_ref, weight = _lot_fields(item, ln.lot_ref, ln.weight_lb)
            lot_id = _new_lot(cur, item["id"], lot_ref, weight, qty, cost, actor, seen) if lot_ref else None
            lines.append({"item_id": item["id"], "lot_id": lot_id, "quantity": qty, "unit_cost": inv.unit_cost(cost, qty), "value": cost})
        mid, number = _insert_movement(cur, {"kind": "receipt", "warehouse_id": wh, "occurred_on": day, "supplier_id": body.supplier_id,
                                             "supplier_ref": body.supplier_ref, "note": body.note, "created_by": actor}, lines)
        return _movement_out(cur, mid, True)
    return _run("No se pudo registrar la entrada", work)


def post_issue(body, actor: str, show_cost: bool) -> dict:
    def work(cur):
        if not body.lines:
            raise billing_error("INVENTORY_NO_LINES")
        wh, day = _warehouse(cur, body.warehouse_id), _date(body.occurred_on)
        cur.execute("SELECT 1 FROM quotes WHERE quote_id = %s", (body.quote_id,))
        if not cur.fetchone():
            raise billing_error("INVENTORY_QUOTE_NOT_FOUND")
        if body.project_id is not None:
            cur.execute("SELECT 1 FROM projects WHERE id = %s", (body.project_id,))
            if not cur.fetchone():
                raise billing_error("INVENTORY_PROJECT_NOT_FOUND")
        parsed = []
        for ln in body.lines:
            qty = inv.parse_positive(ln.quantity)
            if qty is None:
                raise billing_error("INVENTORY_QUANTITY_INVALID")
            parsed.append((ln.item_id, qty))
        items = _lock_items(cur, [p[0] for p in parsed])
        _require_active(items)
        state = _stock(cur, items.keys(), wh)
        lines = []
        for item_id, qty in parsed:
            value = _take(items[item_id], state, qty)
            lines.append({"item_id": item_id, "quantity": -qty, "unit_cost": inv.unit_cost(value, qty), "value": -value})
        mid, number = _insert_movement(cur, {"kind": "issue_job", "warehouse_id": wh, "occurred_on": day, "quote_id": body.quote_id,
                                             "project_id": body.project_id, "note": body.note, "created_by": actor}, lines)
        return _movement_out(cur, mid, show_cost)
    return _run("No se pudo registrar la salida", work)


def post_adjustment(body, actor: str) -> dict:
    def work(cur):
        reason = (body.reason or "").strip()
        if not reason:
            raise billing_error("INVENTORY_REASON_REQUIRED")
        if not body.lines:
            raise billing_error("INVENTORY_NO_LINES")
        wh, day = _warehouse(cur, body.warehouse_id), _date(body.occurred_on)
        parsed = []
        for ln in body.lines:
            qty = inv.parse_decimal(ln.quantity)
            if qty is None or qty == 0:
                raise billing_error("INVENTORY_ADJUST_QUANTITY_INVALID")
            if qty > 0:
                if ln.total_cost is None:
                    raise billing_error("INVENTORY_COST_REQUIRED")
                cost = inv.parse_positive(ln.total_cost)
                if cost is None:
                    raise billing_error("INVENTORY_COST_INVALID")
            else:
                if ln.total_cost is not None:
                    raise billing_error("INVENTORY_COST_NOT_ALLOWED")
                cost = None
            parsed.append((ln.item_id, qty, cost))
        items = _lock_items(cur, [p[0] for p in parsed])
        _require_active(items)
        state = _stock(cur, items.keys(), wh)
        lines = []
        for item_id, qty, cost in parsed:
            if qty > 0:
                _put(items[item_id], state, qty, cost)
                lines.append({"item_id": item_id, "quantity": qty, "unit_cost": inv.unit_cost(cost, qty), "value": cost})
            else:
                value = _take(items[item_id], state, -qty)
                lines.append({"item_id": item_id, "quantity": qty, "unit_cost": inv.unit_cost(value, qty), "value": -value})
        mid, number = _insert_movement(cur, {"kind": "adjustment", "warehouse_id": wh, "occurred_on": day, "reason": reason,
                                             "note": body.note, "created_by": actor}, lines)
        return _movement_out(cur, mid, True)
    return _run("No se pudo registrar el ajuste", work)


def void_movement(movement_id: int, reason: str, actor: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise billing_error("INVENTORY_REASON_REQUIRED")

    def work(cur):
        cur.execute("SELECT * FROM inv_movements WHERE id = %s FOR UPDATE", (movement_id,))
        m = cur.fetchone()
        if not m:
            raise billing_error("INVENTORY_MOVEMENT_NOT_FOUND")
        if m["voided_at"] is not None:
            raise billing_error("INVENTORY_ALREADY_VOID")
        if m["kind"] not in VOIDABLE_KINDS:
            raise billing_error("INVENTORY_NOT_VOIDABLE")
        cur.execute("SELECT item_id, lot_id, quantity, unit_cost, value FROM inv_movement_lines WHERE movement_id = %s ORDER BY position",
                    (movement_id,))
        original = [dict(r) for r in cur.fetchall()]
        items = _lock_items(cur, [r["item_id"] for r in original])
        state = _stock(cur, items.keys(), m["warehouse_id"])
        delta = {i: [ZERO, ZERO] for i in items}
        for r in original:
            delta[r["item_id"]][0] -= Decimal(str(r["quantity"]))
            delta[r["item_id"]][1] -= Decimal(str(r["value"]))
        for i, (dq, dv) in delta.items():
            if not inv.void_leaves_valid_stock(state[i][0], state[i][1], dq, dv):
                raise billing_error("INVENTORY_VOID_BREAKS_STOCK")
        lines = [{"item_id": r["item_id"], "lot_id": r["lot_id"], "quantity": -Decimal(str(r["quantity"])),
                  "unit_cost": Decimal(str(r["unit_cost"])), "value": -Decimal(str(r["value"]))} for r in original]
        # The reversal carries the job reference, so material cost by job nets out.
        vid, number = _insert_movement(cur, {"kind": "void", "warehouse_id": m["warehouse_id"], "occurred_on": business_today(),
                                             "quote_id": m["quote_id"], "project_id": m["project_id"], "reason": reason,
                                             "reverses_movement_id": movement_id, "created_by": actor}, lines)
        cur.execute("UPDATE inv_movements SET voided_at = now(), voided_by = %s, void_reason = %s WHERE id = %s",
                    (actor, reason, movement_id))
        return _movement_out(cur, vid, True)
    return _run("No se pudo anular el movimiento", work)


# ----------------------------------------------------------------------------------------------- opening stock

def _prepare_opening(cur, body) -> dict:
    """Validate the whole batch and compute it. Writes nothing. Locks existing items it names."""
    if not body.rows:
        raise billing_error("INVENTORY_NO_LINES")
    wh, day = _warehouse(cur, body.warehouse_id), _date(body.occurred_on)
    cur.execute("SELECT 1 FROM inv_movements WHERE warehouse_id = %s AND kind = 'opening'", (wh,))
    if cur.fetchone():
        raise billing_error("INVENTORY_OPENING_EXISTS")
    cur.execute("SELECT * FROM inv_items WHERE sku = ANY(%s) ORDER BY id FOR UPDATE", ([r.sku for r in body.rows],))
    existing = {r["sku"]: dict(r) for r in cur.fetchall()}
    rows, seen_sku, seen_lots, total = [], set(), set(), ZERO
    for r in body.rows:
        if not r.sku or not r.name:
            raise api_error(400, "El código y el nombre son obligatorios", "INVENTORY_ITEM_FIELDS_REQUIRED")
        qty, cost = inv.parse_positive(r.quantity), inv.parse_positive(r.total_cost)
        if qty is None:
            raise billing_error("INVENTORY_QUANTITY_INVALID")
        if cost is None:
            raise billing_error("INVENTORY_COST_INVALID")
        length = _positive_length(r.standard_length_ft)
        item = existing.get(r.sku)
        if item is not None:
            if (item["name"], item["kind"], item["stock_unit"], item["tracks_lots"]) != (r.name, r.kind, r.stock_unit, r.tracks_lots):
                raise billing_error("INVENTORY_OPENING_MISMATCH")
            if item["is_dummy"]:
                raise billing_error("INVENTORY_OPENING_DUMMY")
            if not item["is_active"]:
                raise billing_error("INVENTORY_ITEM_INACTIVE")
        if r.sku in seen_sku and not r.tracks_lots:
            raise billing_error("INVENTORY_DUPLICATE_ROW")
        seen_sku.add(r.sku)
        lot_ref, weight = _lot_fields({"tracks_lots": r.tracks_lots}, r.lot_ref, r.weight_lb)
        if lot_ref:
            key = (r.sku, lot_ref)
            if key in seen_lots:
                raise billing_error("INVENTORY_LOT_EXISTS")
            seen_lots.add(key)
            if item is not None:
                cur.execute("SELECT 1 FROM inv_lots WHERE item_id = %s AND lot_ref = %s", (item["id"], lot_ref))
                if cur.fetchone():
                    raise billing_error("INVENTORY_LOT_EXISTS")
        total += cost
        rows.append({"row": r, "item": item, "quantity": qty, "cost": cost, "length": length, "lot_ref": lot_ref, "weight": weight})
    return {"warehouse_id": wh, "occurred_on": day, "rows": rows, "total_value": total}


def opening_preview(body) -> dict:
    def work(cur):
        p = _prepare_opening(cur, body)
        return {"preview": True, "warehouse_id": p["warehouse_id"], "occurred_on": p["occurred_on"].isoformat(),
                "total_value": _f(p["total_value"]), "row_count": len(p["rows"]),
                "rows": [{"sku": x["row"].sku, "name": x["row"].name, "kind": x["row"].kind, "stock_unit": x["row"].stock_unit,
                          "tracks_lots": x["row"].tracks_lots, "quantity": _f(x["quantity"]), "total_cost": _f(x["cost"]),
                          "unit_cost": _f(inv.unit_cost(x["cost"], x["quantity"])), "lot_ref": x["lot_ref"],
                          "weight_lb": _f(x["weight"]), "will_create_item": x["item"] is None} for x in p["rows"]]}
    return _run("No se pudo preparar la carga inicial", work, write=False)


def post_opening(body, actor: str) -> dict:
    def work(cur):
        p = _prepare_opening(cur, body)
        created, lines, seen = {}, [], set()
        for x in p["rows"]:
            r, item = x["row"], x["item"]
            if item is None:
                if r.sku in created:
                    item_id = created[r.sku]
                else:
                    cur.execute(
                        "INSERT INTO inv_items (sku, name, kind, stock_unit, tracks_lots, standard_length_ft, created_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (r.sku, r.name, r.kind, r.stock_unit, r.tracks_lots, x["length"], actor))
                    item_id = created[r.sku] = cur.fetchone()["id"]
            else:
                item_id = item["id"]
            lot_id = _new_lot(cur, item_id, x["lot_ref"], x["weight"], x["quantity"], x["cost"], actor, seen) if x["lot_ref"] else None
            lines.append({"item_id": item_id, "lot_id": lot_id, "quantity": x["quantity"],
                          "unit_cost": inv.unit_cost(x["cost"], x["quantity"]), "value": x["cost"]})
        mid, number = _insert_movement(cur, {"kind": "opening", "warehouse_id": p["warehouse_id"], "occurred_on": p["occurred_on"],
                                             "note": body.note, "created_by": actor}, lines)
        return _movement_out(cur, mid, True)
    return _run("No se pudo registrar la carga inicial", work)


# ----------------------------------------------------------------------------------------------- movement reads (admin)

def list_movements(item_id: Optional[int], kind: Optional[str], quote_id: Optional[str], limit: int, offset: int) -> dict:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    def work(cur):
        where, params = [], []
        if item_id is not None:
            where.append("EXISTS (SELECT 1 FROM inv_movement_lines x WHERE x.movement_id = m.id AND x.item_id = %s)"); params.append(item_id)
        if kind:
            where.append("m.kind = %s"); params.append(kind)
        if quote_id:
            where.append("m.quote_id = %s"); params.append(quote_id)
        sql = ("SELECT m.id, m.number, m.kind, m.warehouse_id, m.occurred_on, m.quote_id, m.project_id, m.reason, m.note, "
               "m.created_by, m.created_at, m.voided_at, m.reverses_movement_id, "
               "(SELECT COUNT(*) FROM inv_movement_lines l WHERE l.movement_id = m.id) AS line_count, "
               "(SELECT COALESCE(SUM(l.value), 0) FROM inv_movement_lines l WHERE l.movement_id = m.id) AS value "
               "FROM inv_movements m" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.id DESC LIMIT %s OFFSET %s")
        cur.execute(sql, params + [limit, offset])
        return {"movements": [{"id": r["id"], "number": r["number"], "label": inv.movement_label(r["number"]), "kind": r["kind"],
                               "warehouse_id": r["warehouse_id"], "occurred_on": r["occurred_on"].isoformat(), "quote_id": r["quote_id"],
                               "project_id": r["project_id"], "reason": r["reason"], "note": r["note"], "created_by": r["created_by"],
                               "created_at": r["created_at"].isoformat() if r["created_at"] else None, "voided": r["voided_at"] is not None,
                               "reverses_movement_id": r["reverses_movement_id"], "line_count": r["line_count"], "value": _f(r["value"])}
                              for r in cur.fetchall()], "limit": limit, "offset": offset}
    return _run("No se pudo leer los movimientos", work, write=False)


def get_movement(movement_id: int) -> dict:
    return _run("No se pudo leer el movimiento", lambda cur: _movement_out(cur, movement_id, True), write=False)
