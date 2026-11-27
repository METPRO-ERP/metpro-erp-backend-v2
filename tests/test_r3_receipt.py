"""R3 recibo de pago. RECEIPT_ENABLED default OFF; the snapshot write is always on (decision D-Y).

Pure rules and the PDF run without a database; the rest uses real Postgres (METPRO_LEDGER_TEST_DB).
"""
import hashlib
import importlib
import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import client, db as _base_db, needs_db, normalized, _rows, _no_db_pages  # noqa: F401,E402
from test_payment_due import (  # noqa: F401,E402
    db, _mkquote, _invoice_of, _col, _admin, _user, ptext,
)
from core import receipt  # noqa: E402
from pdf.builder_receipt import create_receipt_pdf, NOT_FISCAL  # noqa: E402

SWITCH = "RECEIPT_ENABLED"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv("FISCAL_NCF_ENABLED", raising=False)


def _sql(name):
    return open(os.path.join(ROOT, "migrations", name), encoding="utf-8").read()


# ============================================================ pure rules

def test_switch_default_off_and_parsing(monkeypatch):
    assert receipt.enabled() is False
    for v in ("true", "1", "YES", " on "):
        monkeypatch.setenv(SWITCH, v)
        assert receipt.enabled() is True
    for v in ("", "false", "0", "off", "no"):
        monkeypatch.setenv(SWITCH, v)
        assert receipt.enabled() is False


def test_receipt_number_scheme_cannot_be_an_ncf():
    assert receipt.receipt_number(12) == "REC-000012"
    assert receipt.receipt_number(1234567) == "REC-1234567"
    assert receipt.receipt_number(12) == receipt.receipt_number(12)          # stable: derived, nothing allocated
    assert not re.fullmatch(r"[BE]\d{2}\d{8}", receipt.receipt_number(12))   # an NCF is B01 + 8 digits


def test_migration_is_additive_and_rollback_is_guarded():
    up = _sql("2026-11-27_receipt_snapshot.sql").upper()
    for bad in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "UPDATE PUBLIC", "TRUNCATE", "ALTER COLUMN"):
        assert bad not in up
    assert "ADD COLUMN IF NOT EXISTS PAID_SO_FAR_AFTER" in up and "ADD COLUMN IF NOT EXISTS PENDING_AFTER" in up
    down = _sql("2026-11-27_receipt_snapshot.rollback.sql")
    assert "RAISE EXCEPTION" in down and "RECEIPT_ENABLED" in down
    assert down.index("RAISE EXCEPTION") < down.index("DROP COLUMN")          # the guard runs before any drop


# ============================================================ the PDF

PAY = {"amount": 400000.0, "method": "Transferencia", "notes": "Avance 1", "payment_date": date(2026, 11, 20),
       "paid_so_far_after": 400000.0, "pending_after": 1600000.0}
INV = {"invoice_number": "F-10042", "total_amount": 2000000.0, "company_name": "Cliente SRL",
       "company_tax_id": "131-00000-1"}
PAY_SETTLE = dict(PAY, amount=1600000.0, payment_date=date(2026, 12, 5), notes=None,
                  paid_so_far_after=2000000.0, pending_after=0.0)
GOLDEN = {
    "partial": "4f704248c02fee33829e7f6a62281469b7f7b8894c59eaa644c7b98c3785a405",
    "settle": "fb8a02f5f4cb45d391735a8d0155a78f769e6e7eaaa3b201d9e2543c0acbd659",
    "partial_ncf": "ccbf17d474dc2316c53c3c7fc28ff667deed25db8e22ae15f00223aa891c9022",
}


def _pdf(pay=PAY, inv=INV, number="REC-000007", ncf=None, signer="Fabian"):
    return create_receipt_pdf(number, pay, inv, ncf=ncf, signer_name=signer).getvalue()


def _h(b):
    return hashlib.sha256(normalized(b)).hexdigest()


def test_receipt_prints_every_field_from_stored_values():
    t = ptext(_pdf(ncf="B0100000123"))
    for s in ("RECIBO DE PAGO", "Recibo No.:", "REC-000007", "20/11/2026", "Cliente SRL", "131-00000-1", "F-10042",
              "B0100000123", "RD$ 400,000.00", "Transferencia", "Avance 1", "RD$ 2,000,000.00", "RD$ 1,600,000.00",
              "saldos al momento de registrar este pago", "no es un comprobante fiscal"):
        assert s in t, s
    assert "Pagado a la fecha:" in t and "Pendiente:" in t and "Total de la factura:" in t


def test_receipt_without_ncf_or_note_omits_those_lines():
    t = ptext(_pdf(pay=dict(PAY, notes=None)))
    assert "NCF de la factura" not in t and "Nota:" not in t


def test_settling_receipt_shows_zero_pending():
    t = ptext(_pdf(pay=PAY_SETTLE))
    assert "RD$ 0.00" in t and "RD$ 2,000,000.00" in t and "05/12/2026" in t


def test_long_values_print_in_full():
    long = "Corporacion Industrial Metalmecanica del Caribe y Servicios Generales de Construccion SRL"
    t = ptext(_pdf(inv=dict(INV, company_name=long),
                   pay=dict(PAY, method="Transferencia bancaria BHD Leon cuenta corriente en pesos dominicanos",
                            notes="Transferencia ref. 884412990033 correspondiente al primer avance de obra segun acuerdo "
                                  "del 15 de noviembre con la gerencia")))
    for tail in ("Servicios Generales de Construccion SRL", "en pesos dominicanos", "noviembre con la gerencia"):
        assert tail in t, tail


def test_receipt_is_one_page_and_deterministic():
    a, b = _pdf(), _pdf()
    assert normalized(a) == normalized(b)
    assert len(re.findall(rb"/Type /Page\b", a)) == 1


@pytest.mark.parametrize("name,kw", [("partial", {}), ("settle", {"pay": PAY_SETTLE}), ("partial_ncf", {"ncf": "B0100000123"})])
def test_receipt_goldens_are_pinned(name, kw):
    assert _h(_pdf(**kw)) == GOLDEN[name]


# ============================================================ integration

def _invoice(db):
    iid = _invoice_of(_mkquote(db, price=1000.0))
    return iid, float(_col(db, "SELECT total_amount FROM invoices WHERE id = %s", (iid,)))


def _pay(iid, amount, day="2026-11-20", method="Transferencia", notes=None, headers=None):
    return client.post(f"/invoices/{iid}/payments", json={"amount": amount, "method": method, "notes": notes,
                       "payment_date": day}, headers=headers or _admin())


def _snap(db, pid):
    r = _rows(db, "SELECT paid_so_far_after AS a, pending_after AS p FROM invoice_payments WHERE id = %s", (pid,))[0]
    return (None if r["a"] is None else float(r["a"]), None if r["p"] is None else float(r["p"]))


@needs_db
@pytest.mark.parametrize("on", [False, True])
def test_snapshot_is_written_with_the_switch_on_or_off_and_response_is_unchanged(db, monkeypatch, on):
    if on:
        monkeypatch.setenv(SWITCH, "true")
    iid, total = _invoice(db)
    r = _pay(iid, 400.0)
    assert r.status_code == 200, r.text
    assert set(r.json()["data"]) == {"payment_id"}                      # the record-payment response is identical
    p1 = r.json()["data"]["payment_id"]
    assert _snap(db, p1) == (400.0, round(total - 400.0, 2))
    p2 = _pay(iid, round(total - 400.0, 2), day="2026-12-05").json()["data"]["payment_id"]
    assert _snap(db, p2) == (total, 0.0)                                # the settling payment closes at zero
    assert _col(db, "SELECT status FROM invoices WHERE id = %s", (iid,)) == "Paid"


@needs_db
def test_voids_never_rewrite_stored_figures_and_later_figures_exclude_the_voided_payment(db):
    iid, total = _invoice(db)
    a = _pay(iid, 300.0).json()["data"]["payment_id"]
    b = _pay(iid, 200.0).json()["data"]["payment_id"]
    assert _snap(db, a) == (300.0, round(total - 300.0, 2))
    assert _snap(db, b) == (500.0, round(total - 500.0, 2))
    assert client.post(f"/invoices/{iid}/payments/{a}/void", json={"reason": "error de monto"},
                       headers=_admin()).status_code == 200
    assert _snap(db, a) == (300.0, round(total - 300.0, 2))             # stored, untouched
    assert _snap(db, b) == (500.0, round(total - 500.0, 2))             # B still shows what was true when recorded
    c = _pay(iid, 100.0).json()["data"]["payment_id"]
    assert _snap(db, c) == (300.0, round(total - 300.0, 2))             # live payments now: B 200 + C 100


@needs_db
def test_backdated_payment_does_not_rewrite_earlier_figures(db):
    iid, total = _invoice(db)
    a = _pay(iid, 300.0, day="2026-11-20").json()["data"]["payment_id"]
    b = _pay(iid, 100.0, day="2026-11-01").json()["data"]["payment_id"]  # dated before A, recorded after
    assert _snap(db, a) == (300.0, round(total - 300.0, 2))
    assert _snap(db, b) == (400.0, round(total - 400.0, 2))             # recording-time order, not date order


@needs_db
def test_correction_writes_its_own_figures_and_leaves_the_old_row(db):
    iid, total = _invoice(db)
    a = _pay(iid, 300.0).json()["data"]["payment_id"]
    b = _pay(iid, 200.0).json()["data"]["payment_id"]
    r = client.post(f"/invoices/{iid}/payments/{a}/correct", headers=_admin(), json={
        "amount": 250.0, "method": "Efectivo", "notes": None, "payment_date": "2026-11-21", "reason": "monto correcto"})
    assert r.status_code == 200, r.text
    new = r.json()["data"]["payment_id"]
    assert _snap(db, a) == (300.0, round(total - 300.0, 2))
    assert _snap(db, b) == (500.0, round(total - 500.0, 2))
    assert _snap(db, new) == (450.0, round(total - 450.0, 2))           # B 200 + corrected 250


@needs_db
def test_check_constraint_rejects_half_set_and_negative_figures(db):
    import psycopg2
    iid, _ = _invoice(db)
    for a, p in (("10", "NULL"), ("NULL", "10"), ("-1", "10"), ("10", "-1")):
        conn = db["connect"]()
        try:
            with pytest.raises(psycopg2.errors.CheckViolation):
                conn.cursor().execute(
                    f"INSERT INTO invoice_payments (invoice_id, amount, method, payment_date, paid_so_far_after, pending_after) "
                    f"VALUES ({iid}, 1, 'x', CURRENT_DATE, {a}, {p})")
        finally:
            conn.rollback()
            conn.close()


@pytest.fixture
def guarded(db):
    conn = db["connect"]()
    conn.cursor().execute(_sql("2026-10-22_payment_immutability.sql"))
    conn.commit()
    conn.close()
    yield db
    conn = db["connect"]()
    conn.cursor().execute(_sql("2026-10-22_payment_immutability.rollback.sql"))
    conn.commit()
    conn.close()


@needs_db
def test_existing_immutability_trigger_freezes_the_two_figures_and_the_payment_flow_still_works(guarded):
    import psycopg2
    db = guarded
    iid, total = _invoice(db)
    a = _pay(iid, 300.0).json()["data"]["payment_id"]                   # the INSERT carries the figures: allowed
    for sql in ("UPDATE invoice_payments SET paid_so_far_after = 1 WHERE id = %s",
                "UPDATE invoice_payments SET pending_after = 1 WHERE id = %s",
                "UPDATE invoice_payments SET paid_so_far_after = NULL, pending_after = NULL WHERE id = %s"):
        conn = db["connect"]()
        try:
            with pytest.raises(psycopg2.errors.IntegrityConstraintViolation):
                conn.cursor().execute(sql, (a,))
        finally:
            conn.rollback()
            conn.close()
    assert client.post(f"/invoices/{iid}/payments/{a}/void", json={"reason": "error de monto"},
                       headers=_admin()).status_code == 200            # the void is still the only edit
    assert _snap(db, a) == (300.0, round(total - 300.0, 2))
    r = client.post(f"/invoices/{iid}/payments/{a}/correct".replace(str(a), str(_pay(iid, 50.0).json()["data"]["payment_id"])),
                    headers=_admin(), json={"amount": 60.0, "method": "x", "notes": None,
                                            "payment_date": "2026-11-22", "reason": "correccion"})
    assert r.status_code == 200, r.text


def _fiscal_hash(db, iid, qid):
    """Invoice money/tax/NCF columns (the balance columns a payment legitimately moves are left out), the lines, the
    quote and the fiscal tables."""
    sql = ("SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM ("
           "SELECT (row_to_json(i)::jsonb - 'amount_paid' - 'amount_due' - 'status' - 'updated_at')::text t "
           "FROM invoices i WHERE id = %s "
           "UNION ALL SELECT row_to_json(x)::text FROM invoice_items x WHERE invoice_id = %s "
           "UNION ALL SELECT row_to_json(q)::text FROM quotes q WHERE quote_id = %s "
           "UNION ALL SELECT row_to_json(f)::text FROM fiscal_sequences f "
           "UNION ALL SELECT row_to_json(a)::text FROM fiscal_sequence_audit a "
           "UNION ALL SELECT row_to_json(p)::text FROM invoice_phases p) s")
    return _rows(db, sql, (iid, iid, qid))[0]["h"]


@needs_db
def test_fiscal_isolation_recording_a_payment_leaves_invoice_totals_ncf_and_fiscal_tables_identical(db):
    qid = _mkquote(db, price=1000.0)
    iid = _invoice_of(qid)
    before = _fiscal_hash(db, iid, qid)
    inv_before = _rows(db, "SELECT total_amount, subtotal, itbis_total, grand_total, to_jsonb(invoices)->>'ncf' AS ncf "
                           "FROM invoices WHERE id = %s", (iid,))
    assert _pay(iid, 400.0).status_code == 200
    assert _pay(iid, 100.0).status_code == 200
    assert _fiscal_hash(db, iid, qid) == before
    assert _rows(db, "SELECT total_amount, subtotal, itbis_total, grand_total, to_jsonb(invoices)->>'ncf' AS ncf "
                     "FROM invoices WHERE id = %s", (iid,)) == inv_before


@needs_db
def test_payment_flow_is_unchanged_apart_from_the_two_stored_figures(db):
    iid, total = _invoice(db)
    p = _pay(iid, 400.0, method="Cheque", notes="n1").json()["data"]["payment_id"]
    row = dict(_rows(db, "SELECT row_to_json(p)::jsonb - 'paid_so_far_after' - 'pending_after' AS j "
                         "FROM invoice_payments p WHERE id = %s", (p,))[0]["j"])
    assert row["amount"] == 400.0 and row["method"] == "Cheque" and row["notes"] == "n1"
    assert row["voided_at"] is None and row["corrects_payment_id"] is None and row["backfilled"] is False
    inv = _rows(db, "SELECT amount_paid, amount_due, status FROM invoices WHERE id = %s", (iid,))[0]
    assert (float(inv["amount_paid"]), float(inv["amount_due"]), inv["status"]) == (400.0, round(total - 400.0, 2), "Pending")
    assert _pay(iid, total).status_code == 400                         # still refused: exceeds the pending amount
    listed = client.get(f"/invoices/{iid}/payments", headers=_admin()).json()["data"]
    assert [x["id"] for x in listed] == [p]
    assert "paid_so_far_after" not in listed[0] and "pending_after" not in listed[0]   # the figures stay out of the list
    assert listed[0]["has_receipt"] is True                            # one additive flag for the screen


@needs_db
def test_list_flags_which_payments_can_have_a_receipt(db):
    iid, _ = _invoice(db)
    new = _pay(iid, 100.0).json()["data"]["payment_id"]
    voided = _pay(iid, 50.0).json()["data"]["payment_id"]
    client.post(f"/invoices/{iid}/payments/{voided}/void", json={"reason": "error"}, headers=_admin())
    conn = db["connect"]()
    cur = conn.cursor()
    cur.execute("INSERT INTO invoice_payments (invoice_id, amount, method, payment_date) VALUES (%s, 10, 'x', CURRENT_DATE) "
                "RETURNING id", (iid,))
    old = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    flags = {x["id"]: x["has_receipt"] for x in client.get(f"/invoices/{iid}/payments", headers=_admin()).json()["data"]}
    assert flags == {new: True, voided: False, old: False}             # live+stored, voided, pre-R3


# ---- the route

def _receipt(iid, pid, headers=None):
    return client.get(f"/invoices/{iid}/payments/{pid}/receipt", headers=headers or _admin())


@needs_db
def test_off_route_and_options_fail_closed(db):
    iid, _ = _invoice(db)
    p = _pay(iid, 100.0).json()["data"]["payment_id"]
    assert _receipt(iid, p).status_code == 404                         # admin, switch off
    assert _receipt(iid, p, _user()).status_code == 404                # not admin: the switch answers first
    assert _receipt(iid, 99999).status_code == 404
    assert client.get("/quotes/receipt-options", headers=_admin()).json()["data"] == {"receipt_enabled": False}


@needs_db
def test_on_route_serves_the_recibo_to_admins_only(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid, total = _invoice(db)
    p = _pay(iid, 400.0, notes="Avance uno").json()["data"]["payment_id"]
    assert client.get("/quotes/receipt-options", headers=_admin()).json()["data"] == {"receipt_enabled": True}
    assert _receipt(iid, p, _user()).status_code == 403
    assert client.get(f"/invoices/{iid}/payments/{p}/receipt").status_code in (401, 403)
    r = _receipt(iid, p)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert f"REC-{p:06d}" in r.headers["content-disposition"]
    t = ptext(r.content)
    assert f"REC-{p:06d}" in t and "RECIBO DE PAGO" in t and "Avance uno" in t and "Cliente M2" in t
    assert f"RD$ {400:,.2f}" in t and f"RD$ {total - 400:,.2f}" in t
    assert NOT_FISCAL.split(".")[0] in t


@needs_db
def test_receipt_prints_the_stored_figures_not_a_recomputation(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid, total = _invoice(db)
    a = _pay(iid, 300.0).json()["data"]["payment_id"]
    b = _pay(iid, 200.0).json()["data"]["payment_id"]
    client.post(f"/invoices/{iid}/payments/{a}/void", json={"reason": "error"}, headers=_admin())
    r = _receipt(iid, b)
    assert r.status_code == 200, r.text                                # live total is now 200, stored figure is 500
    t = ptext(r.content)
    assert f"RD$ {500:,.2f}" in t and f"RD$ {total - 500:,.2f}" in t


@needs_db
def test_refusals_voided_pre_r3_and_foreign_payment(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid, _ = _invoice(db)
    iid2, _ = _invoice(db)
    a = _pay(iid, 100.0).json()["data"]["payment_id"]
    client.post(f"/invoices/{iid}/payments/{a}/void", json={"reason": "error"}, headers=_admin())
    r = _receipt(iid, a)
    assert r.status_code == 409 and r.json()["code"] == "RECEIPT_PAYMENT_VOIDED"
    assert _snap(db, a)[0] == 100.0                                    # the stored figures stay in the row
    conn = db["connect"]()
    cur = conn.cursor()
    cur.execute("INSERT INTO invoice_payments (invoice_id, amount, method, payment_date) VALUES (%s, 50, 'x', CURRENT_DATE) "
                "RETURNING id", (iid,))
    old = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    r = _receipt(iid, old)
    assert r.status_code == 409 and r.json()["code"] == "RECEIPT_NO_SNAPSHOT"
    assert _receipt(iid2, old).status_code == 404                      # another invoice's payment id


@needs_db
def test_receipt_route_writes_nothing(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid, _ = _invoice(db)
    p = _pay(iid, 100.0).json()["data"]["payment_id"]
    q = ("SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM (SELECT row_to_json(p)::text t FROM invoice_payments p "
         "UNION ALL SELECT row_to_json(i)::text FROM invoices i UNION ALL SELECT row_to_json(s)::text FROM status_history s) x")
    before = _rows(db, q)[0]["h"]
    assert _receipt(iid, p).status_code == 200
    assert _rows(db, q)[0]["h"] == before


@needs_db
def test_receipt_shows_the_ncf_when_the_invoice_has_one(db, monkeypatch):
    iid, _ = _invoice(db)
    p = _pay(iid, 100.0).json()["data"]["payment_id"]
    monkeypatch.setenv(SWITCH, "true")
    monkeypatch.setenv("FISCAL_NCF_ENABLED", "true")
    conn = db["connect"]()
    conn.cursor().execute("UPDATE invoices SET ncf = 'B0100000042' WHERE id = %s", (iid,))
    conn.commit()
    conn.close()
    assert "B0100000042" in ptext(_receipt(iid, p).content)
