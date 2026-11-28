"""Inventory stock ledger, Phase 1 (INVENTORY_ENABLED). Plan: claude/inventory-module-plan-2026-11-28.md, decisions I-D2 to I-D16.

Real Postgres (METPRO_LEDGER_TEST_DB) for everything that touches the ledger. Pure math has its own tests with no database.
Switch OFF identity of the rest of the app is pinned by the existing golden tests in this suite (they run untouched).
"""
import importlib
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import client, db as _base_db, needs_db, _rows, NO_CHARGES  # noqa: E402
from test_ncf_phases import _hdr  # noqa: E402
from test_payment_due import MIGRATIONS_BEFORE, ROOT, HERE, _psql_available  # noqa: E402
from core import inventory as inv  # noqa: E402

SWITCH = "INVENTORY_ENABLED"
MIG = "2026-11-28_inventory_p1.sql"
ROLL = "2026-11-28_inventory_p1.rollback.sql"
CENT = Decimal("0.01")
D = Decimal


# ================================================================================== pure math (no database)

def test_issue_value_is_proportional_and_rounds_half_up():
    assert inv.issue_value(D("5"), D("20"), D("3000.00")) == D("750.00")
    assert inv.issue_value(D("1"), D("3"), D("100.00")) == D("33.33")
    assert inv.issue_value(D("1"), D("2"), D("66.67")) == D("33.34")          # 33.335 rounds half up


def test_a_full_issue_takes_exactly_the_remaining_value():
    assert inv.issue_value(D("3"), D("3"), D("100.00")) == D("100.00")
    assert inv.issue_value(D("0.01"), D("0.01"), D("0.01")) == D("0.01")


def test_three_issues_of_a_thirds_split_leave_no_residue():
    q, v, out = D("3"), D("100.00"), []
    for take in (D("1"), D("1"), D("1")):
        val = inv.issue_value(take, q, v)
        out.append(val)
        q, v = q - take, v - val
    assert out == [D("33.33"), D("33.34"), D("33.33")] and sum(out) == D("100.00") and q == 0 and v == 0


def test_parse_helpers():
    assert inv.parse_positive(1.5) == D("1.50") and inv.parse_positive("2") == D("2.00")
    for bad in (0, -1, 1.005, float("nan"), float("inf"), None, True, "x"):
        assert inv.parse_positive(bad) is None
    assert inv.parse_decimal(-2.5) == D("-2.50") and inv.parse_decimal(1.234) is None


def test_unit_cost_is_four_decimals_and_informational():
    assert inv.unit_cost(D("1000.00"), D("3")) == D("333.3333")
    assert inv.unit_cost(D("-750.00"), D("-5")) == D("150.0000")
    assert inv.unit_cost(D("0"), D("2")) == D("0.0000")


def test_void_validity_rule():
    ok = inv.void_leaves_valid_stock
    assert ok(D(10), D(1000), D(-10), D(-1000))              # undo the only receipt: empty, no value
    assert not ok(D(6), D(600), D(-10), D(-1000))            # negative quantity
    assert not ok(D(10), D(1500), D(-10), D(-1000))          # quantity zero, value left behind
    assert not ok(D(10), D(1500), D(-10), D(-3000))          # negative value
    assert ok(D(5), D(500), D(5), D(500))                    # undoing an exit adds stock back


def test_label_and_switch(monkeypatch):
    assert inv.movement_label(7) == "M-000007"
    monkeypatch.delenv(SWITCH, raising=False)
    assert inv.inventory_enabled() is False
    for v in ("1", "true", "True", " yes ", "ON"):
        monkeypatch.setenv(SWITCH, v)
        assert inv.inventory_enabled() is True
    monkeypatch.setenv(SWITCH, "nope")
    assert inv.inventory_enabled() is False


# ================================================================================== fixtures and helpers

@pytest.fixture
def db(_base_db, monkeypatch):
    monkeypatch.setattr(importlib.import_module("inventory.service"), "get_db_connection", _base_db["connect"])
    monkeypatch.setenv(SWITCH, "true")
    _reset(_base_db)
    yield _base_db
    _reset(_base_db)


def _reset(base):
    conn = base["connect"]()
    cur = conn.cursor()
    cur.execute("TRUNCATE inv_movement_lines, inv_movements, inv_lots, inv_items RESTART IDENTITY CASCADE")
    cur.execute("DELETE FROM inv_warehouses WHERE code <> 'PRINCIPAL'")
    cur.execute("INSERT INTO inv_warehouses (code, name) VALUES ('PRINCIPAL', 'Almacén principal') ON CONFLICT (code) DO UPDATE SET is_active = true")
    conn.commit()
    conn.close()


def H(role="admin"):
    return _hdr(role)


def post(path, body, role="admin"):
    return client.post(path, json=body, headers=H(role))


def get(path, role="admin"):
    return client.get(path, headers=H(role))


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()["data"]


def refused(r, status, code):
    assert r.status_code == status and r.json()["code"] == code, (r.status_code, r.text)


def mkitem(sku="TUBO-1", unit="Ft", kind="raw_material", lots=False, dummy=False, **extra):
    body = {"sku": sku, "name": f"Item {sku}", "kind": kind, "stock_unit": unit, "tracks_lots": lots, "is_dummy": dummy}
    body.update(extra)
    return ok(post("/inventory/items", body))["id"]


def receipt(item_id, qty, cost, role="admin", **extra):
    ln = {"item_id": item_id, "quantity": qty, "total_cost": cost}
    for k in ("lot_ref", "weight_lb"):
        if k in extra:
            ln[k] = extra.pop(k)
    body = {"lines": [ln]}
    body.update(extra)
    return post("/inventory/receipts", body, role)


def mkquote(db):
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "project_name": "Nave de prueba", "included_charges": NO_CHARGES,
            "items": [{"product_name": "Viga", "quantity": 1, "unit_price": 100.0}]}
    r = client.post("/quotes/", json=body, headers=_hdr())
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]["quote_id"]


def issue(db_or_qid, item_id, qty, role="admin", **extra):
    qid = db_or_qid if isinstance(db_or_qid, str) else mkquote(db_or_qid)
    body = {"quote_id": qid, "lines": [{"item_id": item_id, "quantity": qty}]}
    body.update(extra)
    return post("/inventory/issues", body, role)


def adjust(item_id, qty, cost=None, reason="Conteo fisico", **extra):
    ln = {"item_id": item_id, "quantity": qty}
    if cost is not None:
        ln["total_cost"] = cost
    body = {"reason": reason, "lines": [ln]}
    body.update(extra)
    return post("/inventory/adjustments", body)


def void(mid, reason="Error de captura", role="admin"):
    return post(f"/inventory/movements/{mid}/void", {"reason": reason}, role)


def onhand(sku, warehouse_id=None):
    path = "/inventory/items" + (f"?warehouse_id={warehouse_id}" if warehouse_id else "")
    row = next(i for i in ok(get(path))["items"] if i["sku"] == sku)
    return D(str(row["on_hand"])), D(str(row["on_hand_value"]))


def sql_rows(db, sql, params=()):
    return _rows(db, sql, params)


def sql_exec(db, sql, params=()):
    conn = db["connect"]()
    try:
        conn.cursor().execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def sql_fails(db, sql, params=(), exc=None):
    import psycopg2
    conn = db["connect"]()
    try:
        with pytest.raises(exc or psycopg2.Error):
            conn.cursor().execute(sql, params)
    finally:
        conn.rollback()
        conn.close()


# ================================================================================== valuation, end to end

@needs_db
def test_weighted_average_across_two_receipts_and_an_issue(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    ok(receipt(t, 10, 2000.00))
    assert onhand("TUBO-1") == (D("20"), D("3000.00"))
    m = ok(issue(db, t, 5))
    assert m["kind"] == "issue_job" and m["lines"][0]["quantity"] == -5.0 and m["lines"][0]["value"] == -750.0
    assert m["lines"][0]["unit_cost"] == 150.0
    assert onhand("TUBO-1") == (D("15"), D("2250.00"))


@needs_db
def test_thirds_issue_sequence_matches_the_plan_and_leaves_exactly_zero(db):
    t = mkitem()
    ok(receipt(t, 3, 100.00))
    qid = mkquote(db)
    vals = [ok(issue(qid, t, 1))["lines"][0]["value"] for _ in range(3)]
    assert vals == [-33.33, -33.34, -33.33]
    assert onhand("TUBO-1") == (D("0"), D("0.00"))
    assert sql_rows(db, "SELECT SUM(value) AS v, SUM(quantity) AS q FROM inv_movement_lines")[0] == {"v": D("0.00"), "q": D("0.00")}


@needs_db
def test_full_quantity_issue_leaves_value_exactly_zero_even_after_a_rounded_partial(db):
    t = mkitem()
    ok(receipt(t, 7, 100.00))
    ok(issue(db, t, 3))                                      # 42.86 (3 x 100 / 7 = 42.857...)
    assert onhand("TUBO-1") == (D("4"), D("57.14"))
    ok(issue(db, t, 4))                                      # the rest takes exactly 57.14
    assert onhand("TUBO-1") == (D("0"), D("0.00"))


@needs_db
def test_issue_value_is_stored_and_never_moves_when_the_average_changes_later(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    mid = ok(issue(db, t, 5))["id"]
    before = sql_rows(db, "SELECT id, quantity, unit_cost, value FROM inv_movement_lines ORDER BY id")
    ok(receipt(t, 5, 5000.00))
    ok(adjust(t, -1))
    ok(void(ok(receipt(t, 1, 10.00))["id"]))
    after = sql_rows(db, "SELECT id, quantity, unit_cost, value FROM inv_movement_lines WHERE id <= %s ORDER BY id", (before[-1]["id"],))
    assert before == after
    assert ok(get(f"/inventory/movements/{mid}"))["lines"][0]["value"] == -500.0


@needs_db
def test_receipt_value_is_the_typed_total_not_quantity_times_rounded_unit_cost(db):
    t = mkitem()
    m = ok(receipt(t, 3, 1000.00))
    assert m["lines"][0]["value"] == 1000.0 and m["lines"][0]["unit_cost"] == 333.3333
    assert onhand("TUBO-1") == (D("3"), D("1000.00"))


@needs_db
def test_average_is_per_item_and_per_warehouse(db):
    t = mkitem()
    sql_exec(db, "INSERT INTO inv_warehouses (code, name) VALUES ('B', 'Almacén B')")
    wh2 = sql_rows(db, "SELECT id FROM inv_warehouses WHERE code = 'B'")[0]["id"]
    wh1 = sql_rows(db, "SELECT id FROM inv_warehouses WHERE code = 'PRINCIPAL'")[0]["id"]
    refused(receipt(t, 1, 10.0), 400, "INVENTORY_WAREHOUSE_REQUIRED")        # two active warehouses: must say which
    ok(receipt(t, 10, 1000.00, warehouse_id=wh1))
    ok(receipt(t, 10, 3000.00, warehouse_id=wh2))
    assert onhand("TUBO-1", wh1) == (D("10"), D("1000.00")) and onhand("TUBO-1", wh2) == (D("10"), D("3000.00"))
    m = ok(issue(db, t, 5, warehouse_id=wh2))
    assert m["lines"][0]["value"] == -1500.0                                  # wh2 average 300, not the blended 200
    refused(issue(db, t, 11, warehouse_id=wh1), 409, "INVENTORY_INSUFFICIENT_STOCK")
    sql_exec(db, "UPDATE inv_warehouses SET is_active = false WHERE id = %s", (wh2,))
    refused(receipt(t, 1, 10.0, warehouse_id=wh2), 404, "INVENTORY_WAREHOUSE_NOT_FOUND")
    assert ok(receipt(t, 1, 10.0))["warehouse_id"] == wh1                     # one active warehouse again: default applies


@needs_db
def test_random_sequences_keep_quantity_and_value_equal_to_an_independent_model(db):
    rng = random.Random(20261128)
    skus = ["R1", "R2", "R3"]
    ids = {s: mkitem(s) for s in skus}
    qid = mkquote(db)
    state = {s: [D(0), D(0)] for s in skus}          # independent model: [Q, V]
    posted = []                                       # (movement id, kind, [(sku, qty, value)], voided)
    snapshot = {}

    def valid_void(entry):
        deltas = {}
        for sku, q, v in entry["lines"]:
            d = deltas.setdefault(sku, [D(0), D(0)])
            d[0] -= q
            d[1] -= v
        for sku, (dq, dv) in deltas.items():
            q, v = state[sku][0] + dq, state[sku][1] + dv
            if q < 0 or v < 0 or (q == 0 and v != 0):
                return False
        return True

    applied = {"receipt": 0, "issue": 0, "adjust": 0, "void": 0, "refused": 0}
    for _ in range(160):
        op = rng.choice(["receipt", "receipt", "issue", "issue", "adjust", "void"])
        sku = rng.choice(skus)
        q = D(rng.randint(1, 2000)) / 100
        if op == "receipt":
            cost = D(rng.randint(100, 500000)) / 100
            m = ok(receipt(ids[sku], float(q), float(cost)))
            state[sku][0] += q; state[sku][1] += cost
            posted.append({"id": m["id"], "lines": [(sku, q, cost)], "voided": False, "kind": "receipt"})
            applied["receipt"] += 1
        elif op in ("issue", "adjust"):
            loss = op == "issue" or rng.random() < 0.5
            Q, V = state[sku]
            if loss:
                take = min(q, Q) if rng.random() < 0.8 else Q
                if take <= 0:
                    r = issue(qid, ids[sku], float(q)) if op == "issue" else adjust(ids[sku], -float(q))
                    refused(r, 409, "INVENTORY_INSUFFICIENT_STOCK"); applied["refused"] += 1
                    continue
                val = V if take == Q else (take * V / Q).quantize(CENT, rounding=ROUND_HALF_UP)
                m = ok(issue(qid, ids[sku], float(take))) if op == "issue" else ok(adjust(ids[sku], -float(take)))
                state[sku][0] -= take; state[sku][1] -= val
                posted.append({"id": m["id"], "lines": [(sku, -take, -val)], "voided": False, "kind": op})
                applied["issue" if op == "issue" else "adjust"] += 1
            else:
                cost = D(rng.randint(100, 500000)) / 100
                m = ok(adjust(ids[sku], float(q), float(cost)))
                state[sku][0] += q; state[sku][1] += cost
                posted.append({"id": m["id"], "lines": [(sku, q, cost)], "voided": False, "kind": "adjust"})
                applied["adjust"] += 1
        else:
            live = [p for p in posted if not p["voided"]]
            if not live:
                continue
            p = rng.choice(live)
            r = void(p["id"])
            if valid_void(p):
                m = ok(r)
                for s, qq, vv in p["lines"]:
                    state[s][0] -= qq; state[s][1] -= vv
                p["voided"] = True
                applied["void"] += 1
            else:
                refused(r, 409, "INVENTORY_VOID_BREAKS_STOCK"); applied["refused"] += 1
        for s in skus:                                # the API agrees with the model after every operation
            assert onhand(s) == (state[s][0].quantize(CENT), state[s][1].quantize(CENT)), (op, s)
            assert state[s][0] >= 0 and state[s][1] >= 0 and (state[s][0] > 0 or state[s][1] == 0)
    assert applied["receipt"] and applied["issue"] and applied["adjust"] and applied["void"]
    # database-level truth: sums over lines, per item
    rows = sql_rows(db, "SELECT i.sku, SUM(l.quantity) AS q, SUM(l.value) AS v FROM inv_movement_lines l JOIN inv_items i ON i.id = l.item_id GROUP BY i.sku")
    for r in rows:
        assert (D(str(r["q"])), D(str(r["v"]))) == (state[r["sku"]][0].quantize(CENT), state[r["sku"]][1].quantize(CENT))


# ================================================================================== refusals

@needs_db
def test_negative_stock_is_refused_for_issue_and_loss_and_changes_nothing(db):
    t = mkitem()
    refused(issue(db, t, 1), 409, "INVENTORY_INSUFFICIENT_STOCK")                       # nothing on hand
    ok(receipt(t, 10, 500.00))
    r = issue(db, t, 10.01)
    refused(r, 409, "INVENTORY_INSUFFICIENT_STOCK")
    assert "TUBO-1" in r.json()["detail"]
    refused(adjust(t, -10.01), 409, "INVENTORY_INSUFFICIENT_STOCK")
    assert onhand("TUBO-1") == (D("10"), D("500.00"))
    assert sql_rows(db, "SELECT count(*) AS n FROM inv_movements")[0]["n"] == 1         # only the receipt


@needs_db
def test_a_refused_line_rolls_back_the_whole_movement(db):
    a, b = mkitem("A"), mkitem("B")
    ok(receipt(a, 10, 100.00))
    qid = mkquote(db)
    r = post("/inventory/issues", {"quote_id": qid, "lines": [{"item_id": a, "quantity": 5}, {"item_id": b, "quantity": 1}]})
    refused(r, 409, "INVENTORY_INSUFFICIENT_STOCK")
    assert onhand("A") == (D("10"), D("100.00"))


@needs_db
def test_void_of_a_receipt_is_refused_when_it_would_drive_stock_negative(db):
    t = mkitem()
    rid = ok(receipt(t, 10, 1000.00))["id"]
    iid = ok(issue(db, t, 4))["id"]
    refused(void(rid), 409, "INVENTORY_VOID_BREAKS_STOCK")
    assert onhand("TUBO-1") == (D("6"), D("600.00"))
    ok(void(iid))                                                                        # voiding the exit is fine
    assert onhand("TUBO-1") == (D("10"), D("1000.00"))
    ok(void(rid))                                                                        # now nothing consumed it
    assert onhand("TUBO-1") == (D("0"), D("0.00"))


@needs_db
def test_void_is_refused_when_value_would_be_left_behind_or_go_negative(db):
    t = mkitem()
    a = ok(receipt(t, 10, 1000.00))["id"]
    b = ok(receipt(t, 10, 2000.00))["id"]
    ok(issue(db, t, 10))                                                                 # value 1500, Q10 V1500
    refused(void(a), 409, "INVENTORY_VOID_BREAKS_STOCK")                                 # Q0 with 500 left
    refused(void(b), 409, "INVENTORY_VOID_BREAKS_STOCK")                                 # V -500
    assert onhand("TUBO-1") == (D("10"), D("1500.00"))


@needs_db
def test_void_reverses_at_the_stored_values_not_at_the_current_average(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    iid = ok(issue(db, t, 5))["id"]                                                      # value 500
    ok(receipt(t, 5, 2000.00))                                                           # average is now 250
    m = ok(void(iid))
    assert m["kind"] == "void" and m["lines"][0]["quantity"] == 5.0 and m["lines"][0]["value"] == 500.0
    assert onhand("TUBO-1") == (D("15"), D("3000.00"))


@needs_db
def test_void_shape_marks_original_links_reversal_and_refuses_repeats(db):
    t = mkitem()
    rid = ok(receipt(t, 10, 1000.00))["id"]
    refused(void(rid, reason=""), 400, "INVENTORY_REASON_REQUIRED")
    m = ok(void(rid, "Se digito mal"))
    assert m["reverses_movement_id"] == rid and m["reason"] == "Se digito mal" and m["label"].startswith("M-")
    orig = ok(get(f"/inventory/movements/{rid}"))
    assert orig["voided"] is True and orig["void_reason"] == "Se digito mal"
    refused(void(rid), 409, "INVENTORY_ALREADY_VOID")
    refused(void(m["id"]), 409, "INVENTORY_NOT_VOIDABLE")                               # a reversal is never voided
    refused(void(999999), 404, "INVENTORY_MOVEMENT_NOT_FOUND")


@needs_db
def test_void_carries_the_job_reference_and_an_adjustment_can_be_voided(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    qid = mkquote(db)
    iid = ok(issue(qid, t, 2))["id"]
    v = ok(void(iid))
    assert v["quote_id"] == qid
    aid = ok(adjust(t, -3))["id"]
    ok(void(aid))
    assert onhand("TUBO-1") == (D("10"), D("1000.00"))


# ================================================================================== adjustments, issues, items

@needs_db
def test_adjustment_rules(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    m = ok(adjust(t, -2))
    assert m["lines"][0]["value"] == -200.0 and m["reason"] == "Conteo fisico"            # a loss is valued at the average
    refused(adjust(t, 2), 400, "INVENTORY_COST_REQUIRED")                                 # a gain needs a typed cost
    refused(adjust(t, -1, cost=50.0), 400, "INVENTORY_COST_NOT_ALLOWED")
    refused(adjust(t, 0), 400, "INVENTORY_ADJUST_QUANTITY_INVALID")
    refused(adjust(t, 1, cost=0), 400, "INVENTORY_COST_INVALID")
    refused(adjust(t, -1, reason="  "), 400, "INVENTORY_REASON_REQUIRED")
    ok(adjust(t, 2, cost=300.00))
    assert onhand("TUBO-1") == (D("10"), D("1100.00"))
    ok(adjust(t, -10))                                                                    # all of it: exact remaining value
    assert onhand("TUBO-1") == (D("0"), D("0.00"))


@needs_db
def test_issue_needs_a_job_reference_and_valid_references(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    assert post("/inventory/issues", {"lines": [{"item_id": t, "quantity": 1}]}).status_code == 422     # quote_id is required
    refused(post("/inventory/issues", {"quote_id": "nope", "lines": [{"item_id": t, "quantity": 1}]}), 404, "INVENTORY_QUOTE_NOT_FOUND")
    qid = mkquote(db)
    refused(issue(qid, t, 1, project_id=999999), 404, "INVENTORY_PROJECT_NOT_FOUND")
    m = ok(issue(qid, t, 1))                                                              # project_id is optional
    assert m["quote_id"] == qid and m["project_id"] is None
    refused(post("/inventory/issues", {"quote_id": qid, "lines": []}), 400, "INVENTORY_NO_LINES")
    refused(issue(qid, 424242, 1), 404, "INVENTORY_ITEM_NOT_FOUND")


@needs_db
def test_quantities_and_costs_are_validated(db):
    t = mkitem()
    for qty in (0, -1, 1.005):
        refused(receipt(t, qty, 10.0), 400, "INVENTORY_QUANTITY_INVALID")
    for cost in (0, -5, 10.001):
        refused(receipt(t, 1, cost), 400, "INVENTORY_COST_INVALID")
    refused(post("/inventory/receipts", {"lines": []}), 400, "INVENTORY_NO_LINES")
    assert post("/inventory/receipts", {"lines": [{"item_id": t, "quantity": 1, "total_cost": 5, "unit": "Lb"}]}).status_code == 422  # no unit field
    assert post("/inventory/receipts", {"lines": [{"item_id": t, "quantity": 1, "total_cost": 5, "value": 5}]}).status_code == 422     # no computed field


@needs_db
def test_dates_past_allowed_future_refused_and_valuation_follows_posting_order(db):
    t = mkitem()
    past = (date.today() - timedelta(days=40)).isoformat()
    ok(receipt(t, 10, 1000.00))                                                           # posted first, dated today
    m = ok(receipt(t, 10, 3000.00, occurred_on=past))                                     # posted second, dated earlier
    assert m["occurred_on"] == past
    future = (date.today() + timedelta(days=3)).isoformat()
    refused(receipt(t, 1, 10.0, occurred_on=future), 400, "INVENTORY_DATE_INVALID")
    refused(issue(db, t, 1, occurred_on=future), 400, "INVENTORY_DATE_INVALID")
    refused(adjust(t, -1, occurred_on=future), 400, "INVENTORY_DATE_INVALID")
    v = ok(issue(db, t, 10))["lines"][0]["value"]
    assert v == -2000.0                                                                   # average of 20 units worth 4000


@needs_db
def test_items_create_patch_and_identity_rules(db):
    t = mkitem("ANG-2", unit="Ft", standard_length_ft=20)
    refused(post("/inventory/items", {"sku": "ANG-2", "name": "x", "kind": "raw_material", "stock_unit": "Ft"}), 409, "INVENTORY_SKU_EXISTS")
    for bad in ({"stock_unit": "Pie"}, {"kind": "finished_good"}, {"kind": "remnant"}):
        body = {"sku": "BAD", "name": "x", "kind": "raw_material", "stock_unit": "Ft"}
        body.update(bad)
        assert post("/inventory/items", body).status_code == 422
    assert client.patch(f"/inventory/items/{t}", json={"stock_unit": "Lb"}, headers=H()).status_code == 422   # not patchable
    assert client.patch(f"/inventory/items/{t}", json={"sku": "Z"}, headers=H()).status_code == 422
    r = ok(client.patch(f"/inventory/items/{t}", json={"name": "Angular 2x3/16", "notes": "negro"}, headers=H()))
    assert r["name"] == "Angular 2x3/16" and r["notes"] == "negro" and r["standard_length_ft"] == 20.0
    ok(receipt(t, 5, 50.0))
    r = client.patch(f"/inventory/items/{t}", json={"is_active": False}, headers=H())
    refused(r, 409, "INVENTORY_ITEM_HAS_STOCK")
    ok(adjust(t, -5))
    assert ok(client.patch(f"/inventory/items/{t}", json={"is_active": False}, headers=H()))["is_active"] is False
    refused(receipt(t, 1, 5.0), 409, "INVENTORY_ITEM_INACTIVE")
    assert "ANG-2" not in [i["sku"] for i in ok(get("/inventory/items"))["items"]]
    assert "ANG-2" in [i["sku"] for i in ok(get("/inventory/items?include_inactive=true"))["items"]]
    refused(post("/inventory/items", {"sku": "L", "name": "x", "kind": "raw_material", "stock_unit": "Ft", "standard_length_ft": 0}), 400, "INVENTORY_LENGTH_INVALID")


@needs_db
def test_optional_product_link_and_a_deleted_product_only_clears_the_link(db):
    refused(post("/inventory/items", {"sku": "P", "name": "x", "kind": "raw_material", "stock_unit": "Ft", "product_id": 987654}), 404, "INVENTORY_PRODUCT_NOT_FOUND")
    sql_exec(db, "INSERT INTO products (name, unit_price) VALUES ('Producto de prueba', 10)")
    pid = sql_rows(db, "SELECT id FROM products WHERE name = 'Producto de prueba'")[0]["id"]
    t = mkitem("PL", product_id=pid)
    ok(receipt(t, 1, 5.0))
    assert ok(get(f"/inventory/items/{t}"))["product_id"] == pid
    sql_exec(db, "DELETE FROM products WHERE id = %s", (pid,))
    assert ok(get(f"/inventory/items/{t}"))["product_id"] is None


@needs_db
def test_dummy_items_are_listed_but_excluded_from_total_value(db):
    real, dummy = mkitem("REAL"), mkitem("DUMMY", dummy=True)
    ok(receipt(real, 10, 1000.00))
    ok(receipt(dummy, 10, 999.00))
    out = ok(get("/inventory/items"))
    assert out["total_value"] == 1000.0
    assert {i["sku"]: i["is_dummy"] for i in out["items"]} == {"REAL": False, "DUMMY": True}


# ================================================================================== units and coil lots

@needs_db
def test_coil_lot_needs_reference_and_weight_and_weight_never_drives_stock(db):
    c = mkitem("COIL-24", unit="Ft", lots=True)
    refused(receipt(c, 1000, 5000.00), 400, "INVENTORY_LOT_REQUIRED")
    refused(receipt(c, 1000, 5000.00, lot_ref="C-001"), 400, "INVENTORY_LOT_REQUIRED")
    refused(receipt(c, 1000, 5000.00, weight_lb=2200), 400, "INVENTORY_LOT_REQUIRED")
    refused(receipt(c, 1000, 5000.00, lot_ref="C-001", weight_lb=0), 400, "INVENTORY_LOT_REQUIRED")
    m = ok(receipt(c, 1000, 5000.00, lot_ref="C-001", weight_lb=2200))
    ln = m["lines"][0]
    assert ln["quantity"] == 1000.0 and ln["lot_ref"] == "C-001" and ln["weight_lb"] == 2200.0 and ln["stock_unit"] == "Ft"
    assert onhand("COIL-24") == (D("1000"), D("5000.00"))                    # feet, not pounds
    refused(receipt(c, 500, 2600.00, lot_ref="C-001", weight_lb=1100), 409, "INVENTORY_LOT_EXISTS")
    ok(receipt(c, 500, 2600.00, lot_ref="C-002", weight_lb=1100))
    assert onhand("COIL-24") == (D("1500"), D("7600.00"))
    detail = ok(get(f"/inventory/items/{c}"))
    assert [(l["lot_ref"], l["weight_lb"], l["entry_quantity"], l["entry_cost"]) for l in detail["lots"]] == [
        ("C-001", 1000.0 * 0 + 2200.0, 1000.0, 5000.0), ("C-002", 1100.0, 500.0, 2600.0)]


@needs_db
def test_two_coils_in_one_receipt_and_a_duplicate_reference_inside_it(db):
    c = mkitem("COIL-24", lots=True)
    body = {"lines": [{"item_id": c, "quantity": 100, "total_cost": 500, "lot_ref": "A", "weight_lb": 200},
                      {"item_id": c, "quantity": 100, "total_cost": 500, "lot_ref": "B", "weight_lb": 200}]}
    assert len(ok(post("/inventory/receipts", body))["lines"]) == 2
    body["lines"][1]["lot_ref"] = "C"
    body["lines"][0]["lot_ref"] = "C"
    refused(post("/inventory/receipts", body), 409, "INVENTORY_LOT_EXISTS")
    assert sql_rows(db, "SELECT count(*) AS n FROM inv_lots")[0]["n"] == 2                  # the refused receipt left no lot


@needs_db
def test_a_non_lot_item_refuses_lot_data(db):
    t = mkitem("TORN-1", unit="Unidad", kind="consumable")
    refused(receipt(t, 100, 500.00, lot_ref="X", weight_lb=1), 400, "INVENTORY_LOT_NOT_ALLOWED")
    ok(receipt(t, 100, 500.00))


@needs_db
def test_issue_names_no_lot_and_a_lot_id_is_not_accepted(db):
    c = mkitem("COIL-24", lots=True)
    ok(receipt(c, 1000, 5000.00, lot_ref="C-001", weight_lb=2200))
    qid = mkquote(db)
    r = post("/inventory/issues", {"quote_id": qid, "lines": [{"item_id": c, "quantity": 10, "lot_id": 1}]})
    assert r.status_code == 422                                                              # I-D5: issues do not name a coil
    assert ok(issue(qid, c, 10))["lines"][0]["lot_id"] is None


# ================================================================================== opening stock

def _opening_body(**extra):
    rows = [
        {"sku": "COIL-24", "name": "Bobina calibre 24", "kind": "raw_material", "stock_unit": "Ft", "tracks_lots": True,
         "quantity": 1000, "total_cost": 5000.00, "lot_ref": "C-001", "weight_lb": 2200},
        {"sku": "COIL-24", "name": "Bobina calibre 24", "kind": "raw_material", "stock_unit": "Ft", "tracks_lots": True,
         "quantity": 500, "total_cost": 2600.00, "lot_ref": "C-002", "weight_lb": 1100},
        {"sku": "TORN-1", "name": "Tornillo 1/2", "kind": "consumable", "stock_unit": "Unidad", "quantity": 400, "total_cost": 800.00},
    ]
    body = {"rows": rows}
    body.update(extra)
    return body


def _counts(db):
    return sql_rows(db, "SELECT (SELECT count(*) FROM inv_items) AS items, (SELECT count(*) FROM inv_movements) AS movements, "
                        "(SELECT count(*) FROM inv_movement_lines) AS lines, (SELECT count(*) FROM inv_lots) AS lots")[0]


@needs_db
def test_opening_preview_writes_nothing_and_commit_matches_it(db):
    p = ok(post("/inventory/opening/preview", _opening_body()))
    assert p["preview"] is True and p["row_count"] == 3 and p["total_value"] == 8400.0
    assert [r["will_create_item"] for r in p["rows"]] == [True, True, True]
    assert p["rows"][0]["unit_cost"] == 5.0 and p["rows"][2]["unit_cost"] == 2.0
    assert _counts(db) == {"items": 0, "movements": 0, "lines": 0, "lots": 0}
    m = ok(post("/inventory/opening", _opening_body(note="Conteo inicial")))
    assert m["kind"] == "opening" and len(m["lines"]) == 3 and m["note"] == "Conteo inicial"
    assert _counts(db) == {"items": 2, "movements": 1, "lines": 3, "lots": 2}
    assert onhand("COIL-24") == (D("1500"), D("7600.00")) and onhand("TORN-1") == (D("400"), D("800.00"))
    # the preview of an already-posted batch is refused too, and it is the same refusal as the commit
    refused(post("/inventory/opening/preview", _opening_body()), 409, "INVENTORY_OPENING_EXISTS")


@needs_db
def test_opening_is_locked_after_posting_and_cannot_be_voided(db):
    mid = ok(post("/inventory/opening", _opening_body()))["id"]
    refused(post("/inventory/opening", _opening_body()), 409, "INVENTORY_OPENING_EXISTS")
    refused(void(mid), 409, "INVENTORY_NOT_VOIDABLE")                                       # corrections go through adjustments
    t = sql_rows(db, "SELECT id FROM inv_items WHERE sku = 'TORN-1'")[0]["id"]
    ok(adjust(t, -10))
    assert onhand("TORN-1") == (D("390"), D("780.00"))
    sql_fails(db, "INSERT INTO inv_movements (kind, warehouse_id, occurred_on) SELECT 'opening', id, current_date FROM inv_warehouses LIMIT 1")


@needs_db
def test_opening_is_atomic_one_bad_row_creates_nothing(db):
    body = _opening_body()
    body["rows"][2]["total_cost"] = 0
    refused(post("/inventory/opening", body), 400, "INVENTORY_COST_INVALID")
    assert _counts(db) == {"items": 0, "movements": 0, "lines": 0, "lots": 0}
    body = _opening_body()
    body["rows"][1]["lot_ref"] = "C-001"                                                    # duplicate lot inside the batch
    refused(post("/inventory/opening", body), 409, "INVENTORY_LOT_EXISTS")
    body = _opening_body()
    body["rows"].append(dict(body["rows"][2]))                                              # non-lot sku twice
    refused(post("/inventory/opening", body), 400, "INVENTORY_DUPLICATE_ROW")
    assert _counts(db) == {"items": 0, "movements": 0, "lines": 0, "lots": 0}


@needs_db
def test_opening_existing_items_must_match_and_dummies_are_refused(db):
    mkitem("TORN-1", unit="Unidad", kind="consumable", name="Tornillo 1/2")
    mkitem("COIL-24", unit="Ft", lots=True, name="Bobina calibre 24")
    ok(post("/inventory/opening/preview", _opening_body()))
    bad = _opening_body()
    bad["rows"][2]["stock_unit"] = "Lb"
    refused(post("/inventory/opening", bad), 409, "INVENTORY_OPENING_MISMATCH")
    sql_exec(db, "UPDATE inv_items SET is_dummy = true WHERE sku = 'TORN-1'")
    refused(post("/inventory/opening", _opening_body()), 400, "INVENTORY_OPENING_DUMMY")
    sql_exec(db, "UPDATE inv_items SET is_dummy = false WHERE sku = 'TORN-1'")
    m = ok(post("/inventory/opening", _opening_body()))
    assert _counts(db)["items"] == 2 and len(m["lines"]) == 3                               # existing items reused, none duplicated


@needs_db
def test_opening_requires_a_cost_on_every_row_and_a_row_at_all(db):
    refused(post("/inventory/opening", {"rows": []}), 400, "INVENTORY_NO_LINES")
    body = _opening_body()
    del body["rows"][0]["total_cost"]
    assert post("/inventory/opening", body).status_code == 422


# ================================================================================== concurrency on the item lock

@needs_db
def test_two_issues_on_one_item_serialize_and_the_second_is_refused(db, monkeypatch):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    qid = mkquote(db)
    svc = importlib.import_module("inventory.service")
    real = svc._stock

    def slow_stock(*a, **k):                    # widen the window: without the item lock both would read 10 on hand
        out = real(*a, **k)
        time.sleep(0.5)
        return out
    monkeypatch.setattr(svc, "_stock", slow_stock)
    results, barrier = [], threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(issue(qid, t, 7).status_code)
    threads = [threading.Thread(target=go) for _ in range(2)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert sorted(results) == [200, 409]
    assert onhand("TUBO-1") == (D("3"), D("300.00"))


@needs_db
def test_opposite_item_order_does_not_deadlock(db, monkeypatch):
    a, b = mkitem("A"), mkitem("B")
    ok(post("/inventory/receipts", {"lines": [{"item_id": a, "quantity": 100, "total_cost": 100}, {"item_id": b, "quantity": 100, "total_cost": 100}]}))
    qid = mkquote(db)
    results, barrier = [], threading.Barrier(2)

    def go(order):
        barrier.wait()
        lines = [{"item_id": i, "quantity": 5} for i in order]
        results.append(post("/inventory/issues", {"quote_id": qid, "lines": lines}).status_code)
    threads = [threading.Thread(target=go, args=(o,)) for o in ([a, b], [b, a])]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert results == [200, 200] or sorted(results) == [200, 200]
    assert onhand("A") == (D("90"), D("90.00")) and onhand("B") == (D("90"), D("90.00"))


@needs_db
def test_a_void_and_an_issue_racing_never_leave_negative_stock(db, monkeypatch):
    t = mkitem()
    rid = ok(receipt(t, 10, 1000.00))["id"]
    qid = mkquote(db)
    svc = importlib.import_module("inventory.service")
    real = svc._stock

    def slow_stock(*a, **k):
        out = real(*a, **k)
        time.sleep(0.4)
        return out
    monkeypatch.setattr(svc, "_stock", slow_stock)
    results, barrier = {}, threading.Barrier(2)

    def go_void():
        barrier.wait()
        results["void"] = void(rid).status_code

    def go_issue():
        barrier.wait()
        results["issue"] = issue(qid, t, 10).status_code
    threads = [threading.Thread(target=go_void), threading.Thread(target=go_issue)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    q, v = onhand("TUBO-1")
    assert q >= 0 and v >= 0 and (q > 0 or v == 0)
    assert sorted(results.values()) in ([200, 409],)                                          # exactly one of them wins


# ================================================================================== guard triggers and CHECKs

@needs_db
def test_ledger_rows_cannot_be_updated_or_deleted(db):
    c = mkitem("COIL-24", lots=True)
    mid = ok(receipt(c, 100, 500.00, lot_ref="L1", weight_lb=300))["id"]
    sql_fails(db, "UPDATE inv_movement_lines SET quantity = 1")
    sql_fails(db, "DELETE FROM inv_movement_lines")
    sql_fails(db, "UPDATE inv_lots SET weight_lb = 1")
    sql_fails(db, "DELETE FROM inv_lots")
    sql_fails(db, "DELETE FROM inv_movements")
    sql_fails(db, "UPDATE inv_movements SET note = 'editado'")
    sql_fails(db, "UPDATE inv_movements SET occurred_on = current_date - 1")
    sql_fails(db, "UPDATE inv_movements SET voided_at = now(), voided_by = 'x', void_reason = '  '")     # reason is mandatory
    sql_fails(db, "UPDATE inv_movements SET voided_at = now(), voided_by = 'x', void_reason = 'r', note = 'y'")  # nothing else may move
    sql_exec(db, "UPDATE inv_movements SET voided_at = now(), voided_by = 'x', void_reason = 'r' WHERE id = %s", (mid,))   # the stamp is allowed once
    sql_fails(db, "UPDATE inv_movements SET voided_at = now(), voided_by = 'y', void_reason = 'otra'")
    sql_fails(db, "TRUNCATE inv_items")                                                                 # referenced by lines


@needs_db
def test_item_identity_is_frozen_once_it_has_movements_but_a_fresh_item_can_change(db):
    t = mkitem("FRESH")
    sql_exec(db, "UPDATE inv_items SET stock_unit = 'Lb', kind = 'consumable', tracks_lots = true, is_dummy = true WHERE id = %s", (t,))
    ok(receipt(t, 1, 5.0, lot_ref="L", weight_lb=1))
    for col, val in (("stock_unit", "'Ft'"), ("kind", "'raw_material'"), ("tracks_lots", "false"), ("is_dummy", "false"), ("sku", "'OTHER'")):
        sql_fails(db, f"UPDATE inv_items SET {col} = {val} WHERE id = {t}")
    sql_exec(db, "UPDATE inv_items SET name = 'otro nombre', notes = 'n', is_active = true WHERE id = %s", (t,))
    sql_fails(db, f"DELETE FROM inv_items WHERE id = {t}")


@needs_db
def test_check_constraints_unit_kind_sign_and_job_reference(db):
    import psycopg2
    ck, fk = psycopg2.errors.CheckViolation, psycopg2.errors.ForeignKeyViolation
    sql_fails(db, "INSERT INTO inv_items (sku, name, kind, stock_unit) VALUES ('U', 'x', 'raw_material', 'Pie')", exc=ck)
    sql_fails(db, "INSERT INTO inv_items (sku, name, kind, stock_unit) VALUES ('U', 'x', 'raw_material', 'ft')", exc=ck)
    sql_fails(db, "INSERT INTO inv_items (sku, name, kind, stock_unit) VALUES ('U', 'x', 'finished_good', 'Ft')", exc=ck)
    sql_fails(db, "INSERT INTO inv_items (sku, name, kind, stock_unit) VALUES ('U', 'x', 'remnant', 'Ft')", exc=ck)
    t = mkitem("CK")
    rid = ok(receipt(t, 10, 100.0))["id"]
    wh = sql_rows(db, "SELECT id FROM inv_warehouses LIMIT 1")[0]["id"]
    hdr = "INSERT INTO inv_movements (kind, warehouse_id, occurred_on{c}) VALUES ('{k}', %s, current_date{v})"
    sql_fails(db, hdr.format(c="", k="issue_job", v=""), (wh,), exc=ck)                                    # issue without a quote
    sql_fails(db, hdr.format(c="", k="adjustment", v=""), (wh,), exc=ck)                                   # adjustment without a reason
    sql_fails(db, hdr.format(c="", k="void", v=""), (wh,), exc=ck)                                         # void without its original
    sql_fails(db, hdr.format(c=", reverses_movement_id", k="receipt", v=f", {rid}"), (wh,), exc=ck)        # only a void reverses
    sql_fails(db, hdr.format(c="", k="bogus", v=""), (wh,), exc=ck)
    line = "INSERT INTO inv_movement_lines (movement_id, kind, position, item_id, quantity, unit_cost, value) VALUES (%s, %s, 9, %s, %s, 1, %s)"
    sql_fails(db, line, (rid, "receipt", t, -1, -1), exc=ck)                                               # entry must be positive
    sql_fails(db, line, (rid, "receipt", t, 1, 0), exc=ck)                                                 # entry needs a cost
    sql_fails(db, line, (rid, "receipt", t, 0, 0), exc=ck)
    sql_fails(db, line, (rid, "issue_job", t, -1, -1), exc=fk)                                             # kind must equal the header kind
    # issue lines must be negative: build a real issue header, then try a positive line on it
    qid = mkquote(db)
    iid = ok(issue(qid, t, 1))["id"]
    sql_fails(db, line, (iid, "issue_job", t, 1, 1), exc=ck)
    sql_fails(db, line, (iid, "issue_job", t, -1, 5), exc=ck)                                              # an exit never adds value
    lot = "INSERT INTO inv_movement_lines (movement_id, kind, position, item_id, lot_id, quantity, unit_cost, value) VALUES (%s, 'issue_job', 9, %s, 1, -1, 1, -1)"
    sql_exec(db, "INSERT INTO inv_lots (item_id, lot_ref, weight_lb, entry_quantity, entry_cost) VALUES (%s, 'L', 1, 1, 1)", (t,))
    sql_fails(db, lot, (iid, t), exc=ck)                                                                   # a lot only rides entry lines
    sql_fails(db, "INSERT INTO inv_lots (item_id, lot_ref, weight_lb, entry_quantity, entry_cost) VALUES (%s, 'L0', 0, 1, 1)", (t,), exc=ck)
    sql_fails(db, "INSERT INTO inv_lots (item_id, lot_ref, weight_lb, entry_quantity, entry_cost) VALUES (%s, 'L', 1, 1, 1)", (t,),
              exc=psycopg2.errors.UniqueViolation)
    sql_fails(db, "INSERT INTO inv_movements (kind, warehouse_id, occurred_on, source_type) VALUES ('receipt', %s, current_date, 'web')", (wh,), exc=ck)


# ================================================================================== switch OFF

ROUTES = [
    ("GET", "/inventory/warehouses", None), ("GET", "/inventory/items", None), ("GET", "/inventory/items/1", None),
    ("POST", "/inventory/items", {"sku": "X", "name": "x", "kind": "raw_material", "stock_unit": "Ft"}),
    ("PATCH", "/inventory/items/1", {"name": "y"}),
    ("POST", "/inventory/receipts", {"lines": [{"item_id": 1, "quantity": 1, "total_cost": 1}]}),
    ("POST", "/inventory/issues", {"quote_id": "q", "lines": [{"item_id": 1, "quantity": 1}]}),
    ("POST", "/inventory/adjustments", {"reason": "r", "lines": [{"item_id": 1, "quantity": -1}]}),
    ("POST", "/inventory/opening/preview", {"rows": []}), ("POST", "/inventory/opening", {"rows": []}),
    ("GET", "/inventory/movements", None), ("GET", "/inventory/movements/1", None),
    ("POST", "/inventory/movements/1/void", {"reason": "r"}),
]


@needs_db
@pytest.mark.parametrize("flag", ["absent", "false", "off", ""])
def test_switch_off_every_route_is_404_feature_disabled_and_nothing_is_written(db, monkeypatch, flag):
    if flag == "absent":
        monkeypatch.delenv(SWITCH)
    else:
        monkeypatch.setenv(SWITCH, flag)
    before = _counts(db)
    for method, path, body in ROUTES:
        for role in ("admin", "user", "superadmin"):                                          # 404 first, even for non-admins
            r = client.request(method, path, json=body, headers=H(role))
            assert r.status_code == 404 and r.json()["code"] == "FEATURE_DISABLED", (method, path, role, r.status_code, r.text)
    assert _counts(db) == before
    o = ok(get("/inventory/options"))
    assert o == {"enabled": False}                                                           # reports disabled, no database read needed
    assert ok(get("/inventory/options", "user")) == {"enabled": False}


@needs_db
def test_switch_off_still_requires_a_signed_in_user(db, monkeypatch):
    monkeypatch.delenv(SWITCH)
    assert client.get("/inventory/options").status_code == 401
    assert client.get("/inventory/items").status_code == 401


@needs_db
def test_switch_on_options_report_enabled_with_units_and_kinds(db):
    o = ok(get("/inventory/options", "user"))
    assert o == {"enabled": True, "units": ["Ft", "Lb", "Unidad"], "kinds": ["raw_material", "consumable"], "multi_warehouse": False}
    assert [w["code"] for w in ok(get("/inventory/warehouses", "user"))["warehouses"]] == ["PRINCIPAL"]


@needs_db
def test_switching_off_after_postings_hides_everything_and_keeps_the_ledger(db, monkeypatch):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    n = _counts(db)
    monkeypatch.delenv(SWITCH)
    refused(get("/inventory/items"), 404, "FEATURE_DISABLED")
    assert _counts(db) == n
    monkeypatch.setenv(SWITCH, "true")
    assert onhand("TUBO-1") == (D("10"), D("1000.00"))


# ================================================================================== roles (I-D8)

@needs_db
def test_any_signed_in_user_can_issue_to_a_job_and_sees_no_cost(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    m = ok(issue(db, t, 3, role="user"))
    assert m["kind"] == "issue_job" and m["lines"][0]["quantity"] == -3.0
    assert "value" not in m["lines"][0] and "unit_cost" not in m["lines"][0]
    assert m["created_by"] == "t_user"
    adm = ok(issue(db, t, 1, role="admin"))
    assert adm["lines"][0]["value"] == -100.0 and adm["lines"][0]["unit_cost"] == 100.0
    assert onhand("TUBO-1") == (D("6"), D("600.00"))                                          # the user's issue was valued at the average too


@needs_db
def test_everything_else_is_admin_only_and_superadmin_passes(db):
    t = mkitem()
    ok(receipt(t, 10, 1000.00))
    mid = ok(issue(db, t, 1))["id"]
    calls = [
        ("POST", "/inventory/items", {"sku": "N", "name": "n", "kind": "consumable", "stock_unit": "Unidad"}),
        ("PATCH", f"/inventory/items/{t}", {"name": "z"}),
        ("POST", "/inventory/receipts", {"lines": [{"item_id": t, "quantity": 1, "total_cost": 1}]}),
        ("POST", "/inventory/adjustments", {"reason": "r", "lines": [{"item_id": t, "quantity": -1}]}),
        ("POST", "/inventory/opening/preview", _opening_body()), ("POST", "/inventory/opening", _opening_body()),
        ("GET", "/inventory/movements", None), ("GET", f"/inventory/movements/{mid}", None),
        ("POST", f"/inventory/movements/{mid}/void", {"reason": "r"}),
    ]
    n = _counts(db)
    for method, path, body in calls:
        r = client.request(method, path, json=body, headers=H("user"))
        assert r.status_code == 403 and r.json()["code"] == "INSUFFICIENT_PERMISSIONS", (method, path, r.status_code)
    assert _counts(db) == n
    assert client.request("GET", "/inventory/movements", headers=H("superadmin")).status_code == 200
    assert client.request("POST", f"/inventory/movements/{mid}/void", json={"reason": "r"}, headers=H("superadmin")).status_code == 200


@needs_db
def test_cost_is_visible_to_admins_only_on_every_read(db):
    c = mkitem("COIL-24", lots=True)
    ok(receipt(c, 1000, 5000.00, lot_ref="C-001", weight_lb=2200))
    cost_keys = {"on_hand_value", "average_cost", "total_value", "entry_cost", "unit_cost", "value"}

    def keys(x, acc):
        if isinstance(x, dict):
            acc.update(x.keys())
            [keys(v, acc) for v in x.values()]
        elif isinstance(x, list):
            [keys(v, acc) for v in x]
        return acc
    user_view = [ok(get("/inventory/items", "user")), ok(get(f"/inventory/items/{c}", "user")), ok(get("/inventory/items?warehouse_id=1", "user"))]
    for v in user_view:
        assert not (keys(v, set()) & cost_keys), v
    assert user_view[0]["items"][0]["on_hand"] == 1000.0 and user_view[1]["lots"][0]["weight_lb"] == 2200.0
    admin_item = ok(get(f"/inventory/items/{c}"))
    assert admin_item["on_hand_value"] == 5000.0 and admin_item["average_cost"] == 5.0 and admin_item["lots"][0]["entry_cost"] == 5000.0
    assert admin_item["warehouses"][0]["on_hand_value"] == 5000.0
    assert ok(get("/inventory/items"))["total_value"] == 5000.0


@needs_db
def test_movement_list_filters_and_detail(db):
    a, b = mkitem("A"), mkitem("B")
    ok(receipt(a, 10, 100.00))
    ok(receipt(b, 10, 100.00))
    qid = mkquote(db)
    ok(issue(qid, a, 1))
    allm = ok(get("/inventory/movements"))["movements"]
    assert [m["kind"] for m in allm] == ["issue_job", "receipt", "receipt"] and re.fullmatch(r"M-\d{6}", allm[0]["label"])
    assert [m["kind"] for m in ok(get(f"/inventory/movements?item_id={a}"))["movements"]] == ["issue_job", "receipt"]
    assert [m["kind"] for m in ok(get("/inventory/movements?kind=receipt"))["movements"]] == ["receipt", "receipt"]
    assert [m["quote_id"] for m in ok(get(f"/inventory/movements?quote_id={qid}"))["movements"]] == [qid]
    assert len(ok(get("/inventory/movements?limit=1&offset=1"))["movements"]) == 1
    refused(get("/inventory/items/424242"), 404, "INVENTORY_ITEM_NOT_FOUND")


# ================================================================================== the migration (scratch database)

@pytest.mark.skipif(not _psql_available(), reason="needs psql and METPRO_LEDGER_TEST_DB")
def test_migration_touches_no_existing_table_and_rolls_back_only_while_empty():
    import psycopg2
    admin_dsn = os.environ["METPRO_LEDGER_TEST_DB"]
    name = "metpro_inv_scratch_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(admin_dsn); conn.autocommit = True
    conn.cursor().execute(f'CREATE DATABASE "{name}"'); conn.close()
    scratch = re.sub(r"dbname=\S+", "", admin_dsn).strip() + f" dbname={name}"

    def run(x, is_file=True, check=True):
        cmd = ["psql", scratch, "-v", "ON_ERROR_STOP=1", "-q", "-At"] + (["-f", x] if is_file else ["-c", x])
        p = subprocess.run(cmd, capture_output=True, text=True)
        if check:
            assert p.returncode == 0, p.stderr
        return p

    mig = lambda f: os.path.join(ROOT, "migrations", f)
    inv_tables = "(SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'inv\\_%')"
    inv_funcs = "(SELECT count(*) FROM pg_proc WHERE proname LIKE 'inv\\_%')"
    objects = lambda: run(f"SELECT {inv_tables} || ',' || {inv_funcs} || ',' || (SELECT count(*) FROM pg_class WHERE relname = 'inv_movement_number_seq')",
                          is_file=False).stdout.strip()
    # everything that is not an inventory object: columns, constraints, triggers, indexes
    existing = ("SELECT md5(string_agg(x, '|' ORDER BY x)) FROM ("
                "SELECT 'c:' || table_name || '.' || column_name || ':' || data_type || ':' || is_nullable || ':' || coalesce(column_default, '') AS x "
                "FROM information_schema.columns WHERE table_schema = 'public' AND table_name NOT LIKE 'inv\\_%' "
                "UNION ALL SELECT 'k:' || conrelid::regclass || ':' || conname || ':' || pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace AND conrelid::regclass::text NOT LIKE 'inv\\_%' AND confrelid::regclass::text NOT LIKE 'inv\\_%' "
                "UNION ALL SELECT 't:' || tgrelid::regclass || ':' || tgname FROM pg_trigger WHERE NOT tgisinternal AND tgrelid::regclass::text NOT LIKE 'inv\\_%' "
                "UNION ALL SELECT 'i:' || tablename || ':' || indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename NOT LIKE 'inv\\_%') s")
    try:
        run(os.path.join(HERE, "fixtures", "ci_schema.sql"))
        for f in MIGRATIONS_BEFORE:
            run(mig(f))
        assert objects() == "0,0,0"
        base = run(existing, is_file=False).stdout
        run(mig(MIG)); assert objects() == "5,4,1"
        assert run(existing, is_file=False).stdout == base                                    # NO existing table altered
        assert run("SELECT count(*) FROM inv_warehouses", is_file=False).stdout.strip() == "1"
        run(mig(MIG)); assert objects() == "5,4,1"                                            # idempotent
        assert run("SELECT count(*) FROM inv_warehouses", is_file=False).stdout.strip() == "1"   # the seed is not doubled
        run(mig(ROLL)); assert objects() == "0,0,0"                                           # no data: runs
        assert run(existing, is_file=False).stdout == base                                    # and nothing else moved
        run(mig(ROLL))                                                                        # a second run is harmless
        run(mig(MIG))
        run("INSERT INTO inv_items (sku, name, kind, stock_unit) VALUES ('A', 'a', 'raw_material', 'Ft')", is_file=False)
        run(mig(ROLL)); assert objects() == "0,0,0"                                           # items alone do not block (NOTICE only)
        run(mig(MIG))
        run("INSERT INTO inv_items (sku, name, kind, stock_unit) VALUES ('A', 'a', 'raw_material', 'Ft'); "
            "INSERT INTO inv_movements (kind, warehouse_id, occurred_on) VALUES ('receipt', 1, current_date); "
            "INSERT INTO inv_movement_lines (movement_id, kind, position, item_id, quantity, unit_cost, value) VALUES (1, 'receipt', 1, 1, 5, 2, 10)",
            is_file=False)
        p = run(mig(ROLL), check=False)
        assert p.returncode != 0 and "switch is the rollback" in p.stderr and objects() == "5,4,1"   # refused after the first movement
        assert run("SELECT count(*) FROM inv_movement_lines", is_file=False).stdout.strip() == "1"
        # the one-way door is real: not even a superuser DELETE gets past the guard
        p = run("DELETE FROM inv_movement_lines", is_file=False, check=False)
        assert p.returncode != 0
    finally:
        conn = psycopg2.connect(admin_dsn); conn.autocommit = True
        conn.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'); conn.close()
