"""Conduce delivery ledger (CONDUCE_LEDGER_ENABLED). Plan: claude/conduce-module-plan-2026-11-26.md.

Rule 0: a conduce documents the physical delivery of the quote's items. Never payments, phase labels or amounts.
Real Postgres (METPRO_LEDGER_TEST_DB) for the integration tests. OFF identity is pinned in test_conduce_off_golden.py.
"""
import importlib
import os
import re
import sys
import threading
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import (  # noqa: E402
    client, db as _base_db, needs_db, text, _rows, _no_db_pages, create_access_token,
)
from test_ncf_phases import _hdr, _on, _activate, _phase, _invoice_phase, _convert  # noqa: E402
from core import conduce_ledger as cl  # noqa: E402

SWITCH = "CONDUCE_LEDGER_ENABLED"
CHARGES = {"supervision": True, "admin": True, "insurance": True, "transport": True, "contingency": True}
ITEMS = [  # name, qty, price, type, category, unit
    ("Viga IPE 200 x 6 m", 10, 450.0, "product", "ESTRUCTURA", "und"),
    ("Columna HEB 160 x 6 m", 5, 500.0, "product", "ESTRUCTURA", "und"),
    ("Pintura anticorrosiva", 100.5, 15.0, "product", "ACABADOS", "gal"),
    ("Instalacion y montaje", 1, 1500.0, "service", "ACABADOS", "gl"),
    ("Pieza especial a medida", 2, 300.0, "custom", "ACABADOS", "und"),
]


def ptext(b):
    """Readable strings only: the Tj/TJ operands of the content streams (font programs excluded)."""
    t = text(b).replace("\\(", "\x01").replace("\\)", "\x02")
    out = " ".join(re.findall(r"\(([^()]*)\)\s*Tj", t))
    return out.replace("\x01", "(").replace("\x02", ")")


def _admin():
    return _hdr("admin")


@pytest.fixture
def db(_base_db, monkeypatch):
    for mod in ("conduces.service", "fiscal.service", "phases.service"):
        monkeypatch.setattr(importlib.import_module(mod), "get_db_connection", _base_db["connect"], raising=False)
    conn = _base_db["connect"]()
    cur = conn.cursor()
    cur.execute("TRUNCATE fiscal_sequence_audit, fiscal_sequences, invoice_phases RESTART IDENTITY CASCADE")
    cur.execute("INSERT INTO users (id, username, password_hash, email, role, is_active) VALUES "
                "('00000000-0000-0000-0000-000000000002', 't_admin', 'x', 't_admin@test.local', 'admin', true) ON CONFLICT DO NOTHING")
    conn.commit()
    conn.close()
    for name in ("FISCAL_NCF_ENABLED", "PHASED_INVOICING_ENABLED", SWITCH, "ROLE_POLICY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRINT_CONTROLS_ENABLED", "true")
    return _base_db


def _make_quote(db, items=None, hidden=()):
    rows = []
    for i, (name, qty, price, kind, cat, unit) in enumerate(items or ITEMS):
        rows.append({"product_name": name, "quantity": qty, "unit_price": price, "item_type": kind, "category": cat,
                     "unit_of_measure": unit, "print_on_pdf": False if name in hidden else True})
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "project_name": "Nave de prueba",
            "included_charges": CHARGES, "items": rows}
    r = client.post("/quotes/", json=body, headers=_hdr())
    assert r.status_code in (200, 201), r.text
    qid = r.json()["data"]["quote_id"]
    client.post(f"/quotes/{qid}/finalize", headers=_hdr())
    assert client.patch(f"/quotes/{qid}/status", json={"status": "Approved"}, headers=_hdr()).status_code == 200
    return qid


def _invoice(qid):
    r = _convert(qid)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    return d.get("id") or d.get("invoice_id")


def _setup(db, monkeypatch, on=True, **kw):
    qid = _make_quote(db, **kw)
    iid = _invoice(qid)
    if on:
        monkeypatch.setenv(SWITCH, "true")
    ids = {r["product_name"]: r["id"] for r in _rows(db, "SELECT id, product_name FROM quote_items WHERE quote_id = %s", (qid,))}
    return qid, iid, ids


def _create(iid, lines, **extra):
    body = {"invoice_id": iid, "lines": [{"quote_item_id": i, "quantity": q} for i, q in lines]}
    body.update(extra)
    return client.post("/conduces", json=body, headers=_hdr())


def _state(iid):
    r = client.get(f"/conduces/invoice/{iid}/state", headers=_hdr())
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _line(state, name):
    return next(l for l in state["lines"] if l["product_name"] == name)


def _snapshot(db, qid):
    sql = ("SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM ("
           "SELECT row_to_json(i)::text t FROM invoices i UNION ALL SELECT row_to_json(x)::text FROM invoice_items x "
           "UNION ALL SELECT row_to_json(q)::text FROM quotes q UNION ALL SELECT row_to_json(f)::text FROM fiscal_sequences f "
           "UNION ALL SELECT row_to_json(p)::text FROM invoice_phases p) s")
    return _rows(db, sql)[0]["h"]


# ============================================================ no database

def test_switch_default_off_and_parsing(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert cl.ledger_enabled() is False
    for v in ("true", "1", "YES", " on "):
        monkeypatch.setenv(SWITCH, v)
        assert cl.ledger_enabled() is True
    for v in ("", "false", "0", "off"):
        monkeypatch.setenv(SWITCH, v)
        assert cl.ledger_enabled() is False


def test_quantity_rules():
    assert cl.parse_quantity("4") == Decimal("4.00") and cl.parse_quantity(60.5) == Decimal("60.50")
    for bad in (0, -1, "0", "abc", "1.005", None, float("nan")):
        assert cl.parse_quantity(bad) is None


def test_state_labels():
    L = lambda q, d: {"quoted": q, "delivered": d}
    assert cl.state_of([]) == "Sin items entregables"
    assert cl.state_of([L(10, 0), L(5, 0)]) == "Sin entregas"
    assert cl.state_of([L(10, 6), L(5, 5)]) == "Entrega parcial"
    assert cl.state_of([L(10, 10), L(5, 5)]) == "Entregado"


def test_deliverable_lines_are_physical_and_printable():
    assert cl.is_deliverable({"item_type": "product"}) and cl.is_deliverable({"item_type": "custom"}) and cl.is_deliverable({})
    assert not cl.is_deliverable({"item_type": "service"})
    assert not cl.is_deliverable({"item_type": "product", "print_on_pdf": False})


def test_number_scheme_never_collides_with_legacy_or_reinvoice():
    assert cl.conduce_number({"quote_number": 10036}, 1) == "C-10036-E1"
    assert cl.conduce_number({"quote_number": 10002}, 2) == "C-10002-E2"   # legacy is C-10002, re-invoice C-10002-2


def test_ledger_pdf_header_repeats_and_numbers_pages():
    from pdf.builder_conduce import create_conduce_ledger_pdf
    lines = [{"category": None, "name": f"Item {i}", "unit": "und", "quoted": 10, "now": 10, "cum": 10} for i in range(80)]
    out = create_conduce_ledger_pdf("C-1-E1", 1, "2026-11-26", "Q-1", "F-1", {"company_name": "Cliente"}, "P", None, lines, "TOTAL").getvalue()
    t = ptext(out)
    assert t.count("COTIZADO") >= 2                        # the column header opens page 2 as well
    assert re.search(r"gina 1 de\s+2", t) and re.search(r"gina 2 de\s+2", t)


def test_ledger_pdf_carries_no_money_phase_or_payment_words():
    from pdf.builder_conduce import create_conduce_ledger_pdf
    lines = [{"category": "ESTRUCTURA", "name": "Viga", "unit": "und", "quoted": 10, "now": 4, "cum": 4}]
    t = ptext(create_conduce_ledger_pdf("C-1-E1", 1, "2026-11-26", "Q-1", "F-1-2", {"company_name": "Cliente"}, "P", None, lines, "PARTIAL").getvalue())
    for word in ("$", "Total Facturado", "Pagado", "Pendiente:", "Pago inicial", "Fase", "ITBIS", "NCF", "PRECIO", "CANT."):
        assert word not in t, word
    assert "ENTREGA PARCIAL" in t and "ESTA ENTREGA" in t and "ACUMULADO" in t and "PENDIENTE" in t


# ============================================================ OFF: today's behaviour

@needs_db
def test_off_new_routes_are_closed_and_options_say_disabled(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch, on=False)
    assert client.get("/conduces/options", headers=_hdr()).json()["data"]["enabled"] is False
    assert client.get(f"/conduces/invoice/{iid}/state", headers=_hdr()).status_code == 404
    assert _create(iid, [(ids["Viga IPE 200 x 6 m"], 1)]).status_code == 404
    assert client.get("/conduces/00000000-0000-0000-0000-000000000000/pdf", headers=_hdr()).status_code == 404


@needs_db
def test_off_old_route_still_creates_its_row_on_view_and_prints_invoice_lines(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch, on=False)
    r = client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr())
    assert r.status_code == 200
    rows = _rows(db, "SELECT conduce_number, delivery_seq, quote_id FROM conduces")
    assert len(rows) == 1 and rows[0]["delivery_seq"] is None and rows[0]["quote_id"] is None
    assert "Instalacion y montaje" in ptext(r.content)       # today's source: every invoice line, services included
    assert "ESTA ENTREGA" not in ptext(r.content)


# ============================================================ ON: source, ledger math

@needs_db
def test_state_lists_only_deliverable_lines_with_quoted_delivered_pending(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch, hidden=("Pieza especial a medida",))
    st = _state(iid)
    names = [l["product_name"] for l in st["lines"]]
    assert names == ["Viga IPE 200 x 6 m", "Columna HEB 160 x 6 m", "Pintura anticorrosiva"]   # no service, no hidden line
    assert st["state"] == "Sin entregas" and _line(st, "Pintura anticorrosiva") == {
        "quote_item_id": ids["Pintura anticorrosiva"], "product_name": "Pintura anticorrosiva", "unit_of_measure": "gal",
        "category": "ACABADOS", "quoted": 100.5, "delivered": 0.0, "pending": 100.5}


@needs_db
def test_partial_then_completing_delivery(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v, c, p, k = (ids["Viga IPE 200 x 6 m"], ids["Columna HEB 160 x 6 m"], ids["Pintura anticorrosiva"], ids["Pieza especial a medida"])
    r1 = _create(iid, [(v, 6), (c, 5), (p, 40)])
    assert r1.status_code == 200, r1.text
    d1 = r1.json()["data"]
    assert d1["conduce_number"].endswith("-E1") and d1["state"] == "Entrega parcial"
    st = _state(iid)
    assert _line(st, "Viga IPE 200 x 6 m")["pending"] == 4.0 and _line(st, "Pintura anticorrosiva")["delivered"] == 40.0
    r2 = _create(iid, [(v, 4), (p, 60.5), (k, 2)])
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["conduce_number"].endswith("-E2") and r2.json()["data"]["state"] == "Entregado"
    st = _state(iid)
    assert all(l["pending"] == 0 for l in st["lines"]) and st["state"] == "Entregado"
    assert [c["delivery_seq"] for c in st["conduces"]] == [1, 2]


@needs_db
def test_over_delivery_is_blocked_and_nothing_is_written(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v, c = ids["Viga IPE 200 x 6 m"], ids["Columna HEB 160 x 6 m"]
    assert _create(iid, [(v, 6)]).status_code == 200
    r = _create(iid, [(v, 5), (c, 1)])                       # viga has 4 pending; columna would be fine
    assert r.status_code == 409 and r.json()["code"] == "CONDUCE_OVER_DELIVERY"
    assert "Viga IPE 200 x 6 m: pendiente 4.00, solicitado 5.00" in r.json()["detail"]
    st = _state(iid)
    assert _line(st, "Columna HEB 160 x 6 m")["delivered"] == 0 and len(st["conduces"]) == 1   # the valid line was not saved either


@needs_db
def test_exact_pending_is_allowed_and_one_more_cent_is_not(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    p = ids["Pintura anticorrosiva"]
    assert _create(iid, [(p, 100.51)]).status_code == 409
    assert _create(iid, [(p, 100.5)]).status_code == 200
    assert _create(iid, [(p, 0.01)]).status_code == 409


@needs_db
@pytest.mark.parametrize("qty,code", [(0, "CONDUCE_QUANTITY_INVALID"), (-1, "CONDUCE_QUANTITY_INVALID"), (1.005, "CONDUCE_QUANTITY_INVALID")])
def test_quantity_validation(db, monkeypatch, qty, code):
    qid, iid, ids = _setup(db, monkeypatch)
    r = _create(iid, [(ids["Viga IPE 200 x 6 m"], qty)])
    assert r.status_code == 400 and r.json()["code"] == code


@needs_db
def test_line_validation(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v = ids["Viga IPE 200 x 6 m"]
    assert _create(iid, []).json()["code"] == "CONDUCE_NO_LINES"
    assert _create(iid, [(v, 1), (v, 1)]).json()["code"] == "CONDUCE_DUPLICATE_LINE"
    assert _create(iid, [(ids["Instalacion y montaje"], 1)]).json()["code"] == "CONDUCE_LINE_NOT_DELIVERABLE"   # service
    other = _make_quote(db)
    foreign = _rows(db, "SELECT id FROM quote_items WHERE quote_id = %s LIMIT 1", (other,))[0]["id"]
    assert _create(iid, [(foreign, 1)]).json()["code"] == "CONDUCE_LINE_NOT_DELIVERABLE"                        # another quote's line


@needs_db
def test_concurrent_deliveries_cannot_over_deliver(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v = ids["Viga IPE 200 x 6 m"]
    results = []

    def go():
        results.append(_create(iid, [(v, 7)]).status_code)
    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409]
    assert _line(_state(iid), "Viga IPE 200 x 6 m")["delivered"] == 7.0


@needs_db
def test_delivery_date_defaults_to_today_may_be_earlier_never_later(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v = ids["Viga IPE 200 x 6 m"]
    assert _create(iid, [(v, 1)], delivery_date="2099-01-01").json()["code"] == "CONDUCE_DATE_INVALID"
    assert _create(iid, [(v, 1)], delivery_date="2026-01-05").status_code == 200
    assert _create(iid, [(v, 1)]).status_code == 200
    rows = _rows(db, "SELECT delivery_date, created_by FROM conduces ORDER BY delivery_seq")
    assert rows[0]["delivery_date"].isoformat() == "2026-01-05" and rows[1]["delivery_date"] is not None and rows[0]["created_by"]


# ============================================================ source for FULL and phase invoices

@needs_db
def test_pdf_lists_quote_lines_not_invoice_lines_and_marks_partial_then_total(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v, c, p, k = (ids["Viga IPE 200 x 6 m"], ids["Columna HEB 160 x 6 m"], ids["Pintura anticorrosiva"], ids["Pieza especial a medida"])
    c1 = _create(iid, [(v, 6), (c, 5), (p, 40)]).json()["data"]["conduce_id"]
    c2 = _create(iid, [(v, 4), (p, 60.5), (k, 2)]).json()["data"]["conduce_id"]
    t1 = ptext(client.get(f"/conduces/{c1}/pdf", headers=_hdr()).content)
    t2 = ptext(client.get(f"/conduces/{c2}/pdf", headers=_hdr()).content)
    assert "ENTREGA PARCIAL" in t1 and "ENTREGA TOTAL" not in t1 and "No. 1 de esta cotizaci" in t1
    assert "Viga IPE 200 x 6 m" in t1 and "Pieza especial" not in t1 and "Instalacion" not in t1
    assert "ENTREGA TOTAL" in t2 and "No. 2 de esta cotizaci" in t2 and "Pieza especial" in t2 and "Columna" not in t2
    assert "Instalacion y montaje" not in t2 and "$" not in t2
    assert "10.00" in t2.split() and "100.50" in t2.split()                                  # cumulative reached the quoted quantity


@needs_db
def test_phase_invoice_conduce_has_real_items_and_no_phase_label(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _make_quote(db)
    assert _convert(qid, "PHASED").status_code == 200
    pid = _phase(qid, "percent", 50, "Pago inicial / depósito").json()["data"]["id"]
    iid = _invoice_phase(pid).json()["data"]["invoice_id"]
    monkeypatch.setenv(SWITCH, "true")
    ids = {r["product_name"]: r["id"] for r in _rows(db, "SELECT id, product_name FROM quote_items WHERE quote_id = %s", (qid,))}
    cid = _create(iid, [(ids["Viga IPE 200 x 6 m"], 10)]).json()["data"]["conduce_id"]
    t = ptext(client.get(f"/conduces/{cid}/pdf", headers=_hdr()).content)
    assert "Viga IPE 200 x 6 m" in t and "Pago inicial" not in t and "Fase" not in t and "$" not in t and "NCF" not in t
    assert "CANT." not in t


# ============================================================ viewing never creates; old route

@needs_db
def test_viewing_never_creates_a_record_on_any_route(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    before = _rows(db, "SELECT count(*) AS n FROM conduces")[0]["n"]
    assert client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr()).status_code == 404       # nothing to serve yet
    assert client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr()).json()["code"] == "CONDUCE_NOT_FOUND"
    _state(iid)
    cid = _create(iid, [(ids["Viga IPE 200 x 6 m"], 1)]).json()["data"]["conduce_id"]
    client.get(f"/conduces/{cid}/pdf", headers=_hdr())
    old = client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr())
    assert old.status_code == 200 and "C-" in old.headers["content-disposition"] and "-E1" in old.headers["content-disposition"]
    assert _rows(db, "SELECT count(*) AS n FROM conduces")[0]["n"] == before + 1


@needs_db
def test_old_route_serves_the_latest_active_conduce(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    v = ids["Viga IPE 200 x 6 m"]
    _create(iid, [(v, 1)])
    c2 = _create(iid, [(v, 1)]).json()["data"]["conduce_id"]
    assert "-E2" in client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr()).headers["content-disposition"]
    assert client.post(f"/conduces/{c2}/void", json={"reason": "error de digitación"}, headers=_admin()).status_code == 200
    assert "-E1" in client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr()).headers["content-disposition"]


# ============================================================ void, cancel, attachment

@needs_db
def test_void_is_admin_only_needs_a_reason_restores_pending_and_keeps_the_number(db, monkeypatch):
    monkeypatch.setenv("ROLE_POLICY_ENABLED", "true")
    qid, iid, ids = _setup(db, monkeypatch)
    v = ids["Viga IPE 200 x 6 m"]
    cid = _create(iid, [(v, 10)]).json()["data"]["conduce_id"]
    assert client.post(f"/conduces/{cid}/void", json={"reason": "x"}, headers=_hdr("user")).status_code == 403
    assert client.post(f"/conduces/{cid}/void", json={"reason": "  "}, headers=_admin()).json()["code"] == "CONDUCE_VOID_REASON_REQUIRED"
    assert client.post(f"/conduces/{cid}/void", json={"reason": "entrega rechazada"}, headers=_admin()).status_code == 200
    assert _line(_state(iid), "Viga IPE 200 x 6 m")["pending"] == 10.0
    assert client.post(f"/conduces/{cid}/void", json={"reason": "otra vez"}, headers=_admin()).json()["code"] == "CONDUCE_ALREADY_VOID"
    again = _create(iid, [(v, 10)]).json()["data"]
    assert again["conduce_number"].endswith("-E2")                              # E1 is not reused
    st = _state(iid)
    assert [(c["delivery_seq"], c["voided"]) for c in st["conduces"]] == [(1, True), (2, False)]
    assert "CONDUCE ANULADO" in ptext(client.get(f"/conduces/{cid}/pdf", headers=_hdr()).content)


@needs_db
def test_cancelled_invoice_cannot_create_but_its_conduces_stay_and_the_quote_ledger_is_shared(db, monkeypatch):
    qid, iid1, ids = _setup(db, monkeypatch)
    v = ids["Viga IPE 200 x 6 m"]
    assert _create(iid1, [(v, 6)]).status_code == 200
    assert client.patch(f"/invoices/{iid1}/status", json={"status": "Cancelled"}, headers=_hdr()).status_code == 200
    assert _create(iid1, [(v, 1)]).json()["code"] == "CONDUCE_INVOICE_CANCELLED"
    iid2 = _invoice(qid)                                                          # re-invoice (B-06), same quote
    st2 = _state(iid2)
    assert _line(st2, "Viga IPE 200 x 6 m")["delivered"] == 6.0 and len(st2["conduces"]) == 1
    assert _create(iid2, [(v, 5)]).status_code == 409 and _create(iid2, [(v, 4)]).status_code == 200


@needs_db
def test_quote_without_deliverable_lines(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch, items=[("Montaje", 1, 1000.0, "service", None, "gl")])
    assert _state(iid)["state"] == "Sin items entregables"
    assert _create(iid, [(ids["Montaje"], 1)]).json()["code"] == "CONDUCE_NO_DELIVERABLE_LINES"


@needs_db
def test_quote_state_route_matches_the_invoice_state(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    _create(iid, [(ids["Viga IPE 200 x 6 m"], 3)])
    q = client.get(f"/conduces/quote/{qid}/state", headers=_hdr()).json()["data"]
    assert q["state"] == "Entrega parcial" and q["lines"] == _state(iid)["lines"]


@needs_db
def test_legacy_conduces_are_listed_not_counted(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch, on=False)
    client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr())                    # OFF: creates the legacy row
    monkeypatch.setenv(SWITCH, "true")
    st = _state(iid)
    assert st["state"] == "Sin entregas" and len(st["legacy_conduces"]) == 1 and st["conduces"] == []
    assert _create(iid, [(ids["Viga IPE 200 x 6 m"], 1)]).json()["data"]["conduce_number"].endswith("-E1")


# ============================================================ integrity: database guards, delete, isolation

@needs_db
def test_database_refuses_edits_and_deletes_of_ledger_records(db, monkeypatch):
    import psycopg2
    qid, iid, ids = _setup(db, monkeypatch)
    cid = _create(iid, [(ids["Viga IPE 200 x 6 m"], 2)]).json()["data"]["conduce_id"]
    conn = db["connect"]()
    cur = conn.cursor()
    for sql in ("UPDATE conduce_lines SET quantity = 1", "DELETE FROM conduce_lines",
                "DELETE FROM conduces WHERE delivery_seq IS NOT NULL", "UPDATE conduces SET notes = 'x' WHERE delivery_seq IS NOT NULL",
                "UPDATE conduces SET delivery_date = '2020-01-01' WHERE delivery_seq IS NOT NULL"):
        with pytest.raises(psycopg2.Error):
            cur.execute(sql)
        conn.rollback()
    cur.execute("UPDATE conduces SET voided_at = now(), voided_by = 'x', void_reason = 'r' WHERE id::text = %s", (cid,))   # void allowed
    conn.commit()
    with pytest.raises(psycopg2.Error):
        cur.execute("UPDATE conduces SET void_reason = 'again' WHERE id::text = %s", (cid,))
    conn.rollback()
    conn.close()


@needs_db
def test_invoice_with_a_ledger_conduce_cannot_be_deleted_a_legacy_one_can(db, monkeypatch):
    qid, iid, ids = _setup(db, monkeypatch)
    _create(iid, [(ids["Viga IPE 200 x 6 m"], 1)])
    r = client.delete(f"/invoices/{iid}", headers=_admin())
    assert r.status_code == 409 and r.json()["code"] == "INVOICE_HAS_DELIVERIES"
    qid2, iid2, _ = _setup(db, monkeypatch, on=False)
    client.get(f"/pdf/invoices/{iid2}/conduce", headers=_hdr())                   # legacy row
    assert client.delete(f"/invoices/{iid2}", headers=_admin()).status_code == 200


@needs_db
def test_creating_voiding_and_viewing_touch_no_money_tax_or_fiscal_data(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _make_quote(db)
    assert _convert(qid, "PHASED").status_code == 200
    pid = _phase(qid, "percent", 50, "Inicial").json()["data"]["id"]
    iid = _invoice_phase(pid).json()["data"]["invoice_id"]
    monkeypatch.setenv(SWITCH, "true")
    ids = {r["product_name"]: r["id"] for r in _rows(db, "SELECT id, product_name FROM quote_items WHERE quote_id = %s", (qid,))}
    before = _snapshot(db, qid)
    cid = _create(iid, [(ids["Viga IPE 200 x 6 m"], 3)]).json()["data"]["conduce_id"]
    client.get(f"/conduces/{cid}/pdf", headers=_hdr())
    client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr())
    _state(iid)
    client.post(f"/conduces/{cid}/void", json={"reason": "prueba"}, headers=_admin())
    assert _snapshot(db, qid) == before


@needs_db
def test_migration_files_exist_with_a_guarded_rollback():
    base = os.path.join(os.path.dirname(__file__), "..", "migrations")
    up = open(os.path.join(base, "2026-11-26_conduce_ledger.sql")).read()
    down = open(os.path.join(base, "2026-11-26_conduce_ledger.rollback.sql")).read()
    assert "ADD COLUMN IF NOT EXISTS" in up and "DROP COLUMN" not in up and "DELETE FROM" not in up
    assert "RAISE EXCEPTION" in down and "delivery_seq IS NOT NULL" in down
