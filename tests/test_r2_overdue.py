"""R2: read-only overdue view (GET /invoices/overdue). Rides PAYMENT_DUE_ENABLED; no schema; writes nothing.

Pure rules run without a database; the integration tests use real Postgres (METPRO_LEDGER_TEST_DB).
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import client, db as _base_db, needs_db, _rows, _no_db_pages  # noqa: F401,E402
from test_payment_due import (  # noqa: F401,E402
    db, SWITCH, _mkquote, _invoice_of, _col, _admin, _user,
)
from test_ncf_phases import _hdr, _on, _activate, _phase, _invoice_phase, _convert  # noqa: E402
from core import overdue  # noqa: E402
from core.dates import business_today  # noqa: E402

TODAY = date(2026, 11, 27)


def raw(i, due, status="Pending", due_amount="1000.00", issued=date(2026, 9, 1), phase=False, name="Cliente"):
    return {"id": i, "invoice_number": f"F-{i}", "invoice_date": issued, "client_id": 1, "client_name": name,
            "total_amount": Decimal("1000.00"), "amount_paid": Decimal("0"), "amount_due": Decimal(due_amount),
            "status": status, "due_date": due, "is_phase": phase}


def ids(rows):
    return [r["id"] for r in rows]


# ============================================================ pure rules

@pytest.mark.parametrize("days,key", [(1, "1-30"), (30, "1-30"), (31, "31-60"), (60, "31-60"), (61, "61-90"),
                                      (90, "61-90"), (91, "90+"), (400, "90+"), (0, None), (-5, None)])
def test_bucket_boundaries(days, key):
    assert overdue.bucket_key(days) == key


def test_overdue_test_date_balance_status():
    rows = [
        raw(1, TODAY - timedelta(days=1)),                                   # in: one day past due
        raw(2, TODAY),                                                       # out: due today is not overdue
        raw(3, TODAY + timedelta(days=1)),                                   # out: future
        raw(4, TODAY - timedelta(days=5), status="Paid"),                    # out
        raw(5, TODAY - timedelta(days=5), status="Cancelled"),               # out
        raw(6, TODAY - timedelta(days=5), due_amount="0"),                   # out: no balance
        raw(7, TODAY - timedelta(days=5), status="Overdue"),                 # in: stored Overdue with a past date
        raw(8, TODAY - timedelta(days=5), due_amount="-1.00"),               # out: negative balance
    ]
    rep = overdue.build_report(rows, TODAY)
    assert sorted(ids(rep["overdue"]["rows"])) == [1, 7]
    assert ids(rep["sin_plazo"]["rows"]) == []                               # all of them have a due date


def test_sort_most_overdue_first_then_larger_balance():
    rows = [raw(1, TODAY - timedelta(days=10), due_amount="500.00"),
            raw(2, TODAY - timedelta(days=40), due_amount="100.00"),
            raw(3, TODAY - timedelta(days=10), due_amount="900.00"),
            raw(4, TODAY - timedelta(days=95), due_amount="50.00")]
    assert ids(overdue.build_report(rows, TODAY)["overdue"]["rows"]) == [4, 2, 3, 1]


def test_bucket_totals():
    rows = [raw(1, TODAY - timedelta(days=1), due_amount="100.00"), raw(2, TODAY - timedelta(days=30), due_amount="200.00"),
            raw(3, TODAY - timedelta(days=31), due_amount="300.00"), raw(4, TODAY - timedelta(days=90), due_amount="400.00"),
            raw(5, TODAY - timedelta(days=91), due_amount="500.00")]
    rep = overdue.build_report(rows, TODAY)["overdue"]
    got = {b["key"]: (b["count"], b["amount_due"]) for b in rep["buckets"]}
    assert got == {"1-30": (2, 300.0), "31-60": (1, 300.0), "61-90": (1, 400.0), "90+": (1, 500.0)}
    assert rep["count"] == 5 and rep["amount_due"] == 1500.0
    assert [b["key"] for b in rep["buckets"]] == ["1-30", "31-60", "61-90", "90+"]


def test_sin_plazo_is_split_out_and_sorted_by_days_since_issue():
    rows = [raw(1, None, issued=date(2026, 11, 1)), raw(2, None, issued=date(2026, 8, 1)),
            raw(3, None, issued=date(2026, 8, 1), due_amount="9000.00"),
            raw(4, None, status="Overdue", issued=date(2026, 10, 1)),
            raw(5, None, status="Paid", issued=date(2026, 8, 1)), raw(6, None, due_amount="0"),
            raw(7, TODAY - timedelta(days=3))]
    rep = overdue.build_report(rows, TODAY)
    assert ids(rep["sin_plazo"]["rows"]) == [3, 2, 4, 1]                     # oldest first, bigger balance breaks the tie
    assert ids(rep["overdue"]["rows"]) == [7]                                # a dateless invoice is never overdue
    by = {r["id"]: r for r in rep["sin_plazo"]["rows"]}
    assert by[4]["marked_overdue"] is True and by[1]["marked_overdue"] is False
    assert all(r["days_past_due"] is None and r["due_date"] is None for r in rep["sin_plazo"]["rows"])
    assert rep["sin_plazo"]["count"] == 4 and rep["sin_plazo"]["amount_due"] == 12000.0


def test_phase_invoices_age_on_their_own_due_date():
    rows = [raw(1, TODAY - timedelta(days=5), phase=True), raw(2, TODAY - timedelta(days=65), phase=True),
            raw(3, TODAY + timedelta(days=20), phase=True)]
    rep = overdue.build_report(rows, TODAY)["overdue"]["rows"]
    assert [(r["id"], r["days_past_due"], r["bucket"], r["is_phase"]) for r in rep] == [(2, 65, "61-90", True),
                                                                                       (1, 5, "1-30", True)]


def test_timestamp_is_read_in_the_business_time_zone():
    late = datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)                  # 22:00 on the 26th in Santo Domingo (UTC-4)
    r = overdue.build_report([raw(1, None, issued=late)], TODAY)["sin_plazo"]["rows"][0]
    assert r["invoice_date"] == "2026-11-26" and r["days_since_issue"] == 1


def test_today_is_the_business_calendar_date():
    assert business_today(datetime(2026, 11, 27, 3, 0, tzinfo=timezone.utc)) == date(2026, 11, 26)
    assert business_today(datetime(2026, 11, 27, 5, 0, tzinfo=timezone.utc)) == date(2026, 11, 27)


def test_money_is_rounded_to_cents_and_rows_carry_no_internal_fields():
    row = overdue.build_report([raw(1, TODAY - timedelta(days=2), due_amount="1234.5")], TODAY)["overdue"]["rows"][0]
    assert row["amount_due"] == 1234.5
    assert set(row) == {"id", "invoice_number", "client_id", "client_name", "invoice_date", "due_date", "days_past_due",
                        "days_since_issue", "total_amount", "amount_paid", "amount_due", "status", "is_phase", "bucket"}


def test_the_module_never_writes():
    src = open(os.path.join(os.path.dirname(__file__), "..", "core", "overdue.py"), encoding="utf-8").read()
    code = src.split('"""', 2)[2]                                             # skip the docstring, which names the rules
    for word in ("INSERT", "UPDATE", "DELETE", "commit(", "executemany", "execute_values"):
        assert word not in code, word
    assert code.count("cursor.execute(") == 1


def test_route_needs_a_token(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert client.get("/invoices/overdue").status_code in (401, 403)         # auth first, like every invoice route


# ============================================================ integration (real Postgres)

def _seed(db, number, due, status="Pending", total=1000.0, paid=0.0, issued=None):
    conn = db["connect"]()
    try:
        cur = conn.cursor()
        issued = issued or (business_today() - timedelta(days=100))
        cur.execute("INSERT INTO invoices (invoice_number, status, total_amount, amount_paid, amount_due, client_id, "
                    "invoice_date, due_date, tax_type_code, tax_rate, tax_label) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0.18,%s) RETURNING id",
                    (number, status, total, paid, round(total - paid, 2), db["cid"],
                     datetime.combine(issued, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=16), due, "itbis_18", "ITBIS (18%)"))
        iid = cur.fetchone()["id"]
        conn.commit()
        return iid
    finally:
        conn.close()


def _snapshot(db):
    """Every table the view could conceivably touch, as one hash."""
    sql = ("SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM ("
           "SELECT 'i'||row_to_json(a)::text t FROM invoices a "
           "UNION ALL SELECT 'p'||row_to_json(a)::text FROM invoice_payments a "
           "UNION ALL SELECT 'x'||row_to_json(a)::text FROM invoice_items a "
           "UNION ALL SELECT 'q'||row_to_json(a)::text FROM quotes a "
           "UNION ALL SELECT 'f'||row_to_json(a)::text FROM fiscal_sequences a "
           "UNION ALL SELECT 'g'||row_to_json(a)::text FROM fiscal_sequence_audit a "
           "UNION ALL SELECT 'h'||row_to_json(a)::text FROM invoice_phases a "
           "UNION ALL SELECT 'd'||row_to_json(a)::text FROM invoice_due_date_audit a "
           "UNION ALL SELECT 'c'||row_to_json(a)::text FROM clients a) s")
    return _rows(db, sql)[0]["h"]


@needs_db
def test_off_the_route_is_404_and_on_it_answers(db, monkeypatch):
    t = business_today()
    _seed(db, "F-R2-1", t - timedelta(days=3))
    monkeypatch.delenv(SWITCH, raising=False)
    off = client.get("/invoices/overdue", headers=_hdr())
    assert off.status_code == 404
    monkeypatch.setenv(SWITCH, "true")
    on = client.get("/invoices/overdue", headers=_hdr())
    assert on.status_code == 200, on.text
    body = on.json() if "overdue" in on.json() else on.json()["data"]
    assert body["as_of"] == t.isoformat() and ids(body["overdue"]["rows"]) != []


@needs_db
def test_on_end_to_end_overdue_sin_plazo_and_exclusions(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    t = business_today()
    a = _seed(db, "F-A", t - timedelta(days=12), total=500.0)
    b = _seed(db, "F-B", t - timedelta(days=45), total=700.0, paid=200.0)        # partial payment: balance 500
    c = _seed(db, "F-C", t, total=100.0)                                          # due today
    d = _seed(db, "F-D", t + timedelta(days=9), total=100.0)                      # future
    e = _seed(db, "F-E", t - timedelta(days=50), status="Paid", total=100.0, paid=100.0)
    f = _seed(db, "F-F", t - timedelta(days=50), status="Cancelled", total=100.0)
    g = _seed(db, "F-G", t - timedelta(days=50), total=100.0, paid=100.0)         # balance 0 while still Pending
    h = _seed(db, "F-H", None, total=300.0, issued=t - timedelta(days=200))
    i = _seed(db, "F-I", None, total=300.0, status="Overdue", issued=t - timedelta(days=20))
    r = client.get("/invoices/overdue", headers=_hdr())
    assert r.status_code == 200, r.text
    body = r.json() if "overdue" in r.json() else r.json()["data"]
    assert ids(body["overdue"]["rows"]) == [b, a]
    assert [x["amount_due"] for x in body["overdue"]["rows"]] == [500.0, 500.0]
    assert {x["key"]: x["count"] for x in body["overdue"]["buckets"]} == {"1-30": 1, "31-60": 1, "61-90": 0, "90+": 0}
    assert ids(body["sin_plazo"]["rows"]) == [h, i]
    assert body["sin_plazo"]["rows"][1]["marked_overdue"] is True
    for gone in (c, d, e, f, g):
        assert gone not in ids(body["overdue"]["rows"]) + ids(body["sin_plazo"]["rows"])


@needs_db
def test_on_phase_invoices_age_individually(db, monkeypatch):
    _on(monkeypatch, phased=True)
    monkeypatch.setenv(SWITCH, "true")
    _activate()
    qid = _mkquote(db, credit_days=30, price=1000.0)
    assert _convert(qid, "PHASED").status_code == 200
    p1 = _phase(qid, "percent", 50, "Inicial").json()["data"]["id"]
    i1 = _invoice_phase(p1).json()["data"]["invoice_id"]
    p2 = _phase(qid, "final", None, "Final").json()["data"]["id"]
    i2 = _invoice_phase(p2).json()["data"]["invoice_id"]
    t = business_today()
    conn = db["connect"]()
    cur = conn.cursor()
    cur.execute("UPDATE invoices SET due_date = %s WHERE id = %s", (t - timedelta(days=5), i1))
    cur.execute("UPDATE invoices SET due_date = %s WHERE id = %s", (t - timedelta(days=70), i2))
    conn.commit()
    conn.close()
    r = client.get("/invoices/overdue", headers=_hdr())
    body = r.json() if "overdue" in r.json() else r.json()["data"]
    rows = {x["id"]: x for x in body["overdue"]["rows"]}
    assert rows[i1]["days_past_due"] == 5 and rows[i1]["bucket"] == "1-30" and rows[i1]["is_phase"] is True
    assert rows[i2]["days_past_due"] == 70 and rows[i2]["bucket"] == "61-90" and rows[i2]["is_phase"] is True
    assert ids(body["overdue"]["rows"])[:2] == [i2, i1]


@needs_db
def test_the_view_writes_nothing(db, monkeypatch):
    _on(monkeypatch, phased=True)
    monkeypatch.setenv(SWITCH, "true")
    _activate()
    qid = _mkquote(db, credit_days=30, price=1000.0)
    iid = _invoice_of(qid)
    t = business_today()
    _seed(db, "F-W1", t - timedelta(days=40), status="Pending")
    _seed(db, "F-W2", None, status="Pending")
    before = _snapshot(db)
    list_before = client.get("/invoices/", headers=_hdr()).text
    for _ in range(3):
        assert client.get("/invoices/overdue", headers=_hdr()).status_code == 200
    assert _snapshot(db) == before                                                # fiscal and invoice tables byte-identical
    assert client.get("/invoices/", headers=_hdr()).text == list_before          # the list payload is untouched
    assert _col(db, "SELECT count(*) FROM invoice_due_date_audit") == 0          # no audit row either
    assert _col(db, "SELECT status FROM invoices WHERE invoice_number = 'F-W1'") == "Pending"   # status never flipped
    assert iid


@needs_db
def test_any_signed_in_user_can_read_it(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    assert client.get("/invoices/overdue").status_code in (401, 403)
    r = client.get("/invoices/overdue", headers=_user())
    assert r.status_code == 200, r.text
