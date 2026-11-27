"""R4: client-level default payment terms. Plan: claude/four-items-scoping-plan-2026-11-27.md section 5.

Rides PAYMENT_DUE_ENABLED (no new switch). Off: the client payload is byte-identical to main 24da77e and the new
fields are ignored on write. On: the client stores and returns default_credit_days / default_payment_terms.
The default is a PREFILL done by the form (D-R): the backend never inherits on its own, never touches an existing
quote or invoice (snapshot discipline), and a duplicate keeps the source quote's own term (D-U).
Real Postgres (METPRO_LEDGER_TEST_DB).
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import client, db as _base_db, needs_db, _rows  # noqa: E402
from test_ncf_phases import _hdr  # noqa: E402
from test_payment_due import (  # noqa: E402
    SWITCH, CHARGES, _col, _mkquote, _invoice_of, MIGRATIONS_BEFORE, ROOT, HERE, _psql_available,
)

MIG = "2026-11-27_clients_default_terms.sql"
ROLL = "2026-11-27_clients_default_terms.rollback.sql"


@pytest.fixture
def db(_base_db, monkeypatch):
    monkeypatch.setattr(importlib.import_module("clients.service"), "get_db_connection", _base_db["connect"])
    monkeypatch.setattr(importlib.import_module("quotes.service"), "get_db_connection", _base_db["connect"], raising=False)
    monkeypatch.setenv("DUPLICATE_CHANGE_CLIENT_ENABLED", "true")
    for name in (SWITCH, "PHASED_INVOICING_ENABLED", "FISCAL_NCF_ENABLED", "ROLE_POLICY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return _base_db


def _mk_client(name="Cliente R4", **extra):
    body = {"company_name": name}
    body.update(extra)
    return client.post("/clients/", json=body, headers=_hdr())


def _cid(r):
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]["id"]


def _row(db, cid):
    return _rows(db, "SELECT default_credit_days, default_payment_terms FROM clients WHERE id = %s", (cid,))[0]


# ============================================================ switch OFF: fail closed, payload unchanged

@needs_db
def test_off_payload_has_no_default_fields_and_input_is_ignored(db):
    r = _mk_client(default_credit_days=30, default_payment_terms="30 dias")
    cid = _cid(r)
    assert set(r.json()["data"].keys()) == {"id", "company_name", "contact_name", "email", "phone", "address", "tax_id", "notes"}
    assert _row(db, cid) == {"default_credit_days": None, "default_payment_terms": None}
    for url in (f"/clients/{cid}", "/clients/"):
        d = client.get(url, headers=_hdr()).json()["data"]
        d = d if isinstance(d, dict) else d[0]
        assert "default_credit_days" not in d and "default_payment_terms" not in d
    r = client.put(f"/clients/{cid}", json={"company_name": "Cliente R4", "default_credit_days": 45}, headers=_hdr())
    assert r.status_code == 200 and "default_credit_days" not in r.json()["data"]
    assert _row(db, cid)["default_credit_days"] is None


@needs_db
def test_off_leaves_a_stored_default_untouched_and_unseen(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    cid = _cid(_mk_client(default_credit_days=30))
    monkeypatch.delenv(SWITCH)
    client.put(f"/clients/{cid}", json={"company_name": "Renombrado", "default_credit_days": 90}, headers=_hdr())
    assert _row(db, cid)["default_credit_days"] == 30                       # off: no write
    assert "default_credit_days" not in client.get(f"/clients/{cid}", headers=_hdr()).json()["data"]


# ============================================================ switch ON

@needs_db
def test_on_default_is_stored_returned_and_listed(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    r = _mk_client(default_credit_days=30, default_payment_terms="  30 dias neto  ")
    cid = _cid(r)
    assert r.json()["success"] is True
    assert r.json()["data"]["default_credit_days"] == 30 and r.json()["data"]["default_payment_terms"] == "30 dias neto"
    assert _row(db, cid) == {"default_credit_days": 30, "default_payment_terms": "30 dias neto"}
    assert client.get(f"/clients/{cid}", headers=_hdr()).json()["data"]["default_credit_days"] == 30
    listed = {c["id"]: c for c in client.get("/clients/", headers=_hdr()).json()["data"]}
    assert listed[cid]["default_credit_days"] == 30
    other = _cid(_mk_client("Sin termino"))                                 # no default: nulls, not missing
    assert _row(db, other) == {"default_credit_days": None, "default_payment_terms": None}
    assert client.get(f"/clients/{other}", headers=_hdr()).json()["data"]["default_credit_days"] is None


@needs_db
def test_on_put_not_sent_is_untouched_sent_null_clears_zero_is_contado(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    cid = _cid(_mk_client(default_credit_days=30, default_payment_terms="30 dias"))
    r = client.put(f"/clients/{cid}", json={"company_name": "Cliente R4", "phone": "809"}, headers=_hdr())
    assert r.status_code == 200 and _row(db, cid) == {"default_credit_days": 30, "default_payment_terms": "30 dias"}
    client.put(f"/clients/{cid}", json={"company_name": "Cliente R4", "default_credit_days": 0}, headers=_hdr())
    assert _row(db, cid)["default_credit_days"] == 0 and _row(db, cid)["default_payment_terms"] == "30 dias"
    client.put(f"/clients/{cid}", json={"company_name": "Cliente R4", "default_credit_days": None, "default_payment_terms": "  "}, headers=_hdr())
    assert _row(db, cid) == {"default_credit_days": None, "default_payment_terms": None}


@needs_db
def test_on_validation_bounds(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    cid = _cid(_mk_client(default_credit_days=365))
    for bad in (-1, 366, "abc", 1.5):
        assert _mk_client("X", default_credit_days=bad).status_code == 422
        assert client.put(f"/clients/{cid}", json={"company_name": "C", "default_credit_days": bad}, headers=_hdr()).status_code == 422
    assert _row(db, cid)["default_credit_days"] == 365


# ============================================================ D-R: prefill only, snapshot discipline

@needs_db
def test_backend_does_not_inherit_and_quote_level_value_wins(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    client.put(f"/clients/{db['cid']}", json={"company_name": "Cliente M2", "default_credit_days": 30, "default_payment_terms": "30 dias"}, headers=_hdr())
    bare = _mkquote(db, approve=False, terms=None)
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (bare,)) is None      # no server-side inheritance
    own = _mkquote(db, credit_days=10, terms="10 dias", approve=False)
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (own,)) == 10          # the quote's own value wins
    assert _col(db, "SELECT payment_terms FROM quotes WHERE quote_id = %s", (own,)) == "10 dias"
    form = _mkquote(db, credit_days=30, terms="30 dias", approve=False)                          # what the prefilled form sends
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (form,)) == 30


def _snapshot(db, qid, iid):
    q = _rows(db, "SELECT to_jsonb(quotes) AS j FROM quotes WHERE quote_id = %s", (qid,))[0]["j"]
    i = _rows(db, "SELECT to_jsonb(invoices) AS j FROM invoices WHERE id = %s", (iid,))[0]["j"]
    q.pop("updated_at", None); i.pop("updated_at", None)
    return json.dumps([q, i], sort_keys=True, default=str)


@needs_db
def test_editing_a_client_default_leaves_quotes_and_invoices_byte_identical(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    client.put(f"/clients/{db['cid']}", json={"company_name": "Cliente M2", "default_credit_days": 30}, headers=_hdr())
    qid = _mkquote(db, credit_days=30, terms="30 dias")
    iid = _invoice_of(qid)
    due = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    before = _snapshot(db, qid, iid)
    for payload in ({"default_credit_days": 90, "default_payment_terms": "90 dias"}, {"default_credit_days": None}, {"default_credit_days": 0}):
        assert client.put(f"/clients/{db['cid']}", json={"company_name": "Cliente M2", **payload}, headers=_hdr()).status_code == 200
        assert _snapshot(db, qid, iid) == before
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) == due
    # even a rename of the client does not move the stored snapshot
    client.put(f"/clients/{db['cid']}", json={"company_name": "Otro Nombre", "default_credit_days": 7}, headers=_hdr())
    assert _snapshot(db, qid, iid) == before


@needs_db
def test_duplicate_for_another_client_keeps_the_source_term(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    b = _cid(_mk_client("Cliente B", default_credit_days=60, default_payment_terms="60 dias"))
    conn = db["connect"](); cur = conn.cursor()
    cur.execute("INSERT INTO contacts (company_id, name, email) VALUES (%s, 'Contacto B', 'b@x.com') RETURNING id", (b,))
    bct = cur.fetchone()["id"]; conn.commit(); conn.close()
    src = _mkquote(db, credit_days=15, terms="15 dias", approve=False)
    r = client.post(f"/quotes/{src}/duplicate", json={"client_id": b, "contact_id": bct}, headers=_hdr())
    assert r.status_code in (200, 201), r.text
    new = r.json()["data"]["quote_id"]
    assert _col(db, "SELECT client_id FROM quotes WHERE quote_id = %s", (new,)) == b
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (new,)) == 15          # not B's 60
    assert _col(db, "SELECT payment_terms FROM quotes WHERE quote_id = %s", (new,)) == "15 dias"


# ============================================================ migration and guarded rollback (scratch database)

@pytest.mark.skipif(not _psql_available(), reason="needs psql and METPRO_LEDGER_TEST_DB")
def test_migration_and_guarded_rollback_on_a_scratch_database():
    import psycopg2
    admin_dsn = os.environ["METPRO_LEDGER_TEST_DB"]
    name = "metpro_r4_scratch_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(admin_dsn); conn.autocommit = True
    conn.cursor().execute(f'CREATE DATABASE "{name}"'); conn.close()
    scratch = re.sub(r"dbname=\S+", "", admin_dsn).strip() + f" dbname={name}"

    def run(x, is_file=True, check=True):
        cmd = ["psql", scratch, "-v", "ON_ERROR_STOP=1", "-q", "-At"] + (["-f", x] if is_file else ["-c", x])
        p = subprocess.run(cmd, capture_output=True, text=True)
        if check:
            assert p.returncode == 0, p.stderr
        return p

    def objects():
        return run("SELECT (SELECT count(*) FROM information_schema.columns WHERE table_name = 'clients' AND column_name = 'default_credit_days') "
                   "|| ',' || (SELECT count(*) FROM information_schema.columns WHERE table_name = 'clients' AND column_name = 'default_payment_terms') "
                   "|| ',' || (SELECT count(*) FROM pg_constraint WHERE conname = 'clients_default_credit_days_check')", is_file=False).stdout.strip()

    mig = lambda f: os.path.join(ROOT, "migrations", f)
    cols = ("SELECT string_agg(table_name || '.' || column_name, ',' ORDER BY table_name, column_name) "
            "FROM information_schema.columns WHERE table_schema = 'public'")
    try:
        run(os.path.join(HERE, "fixtures", "ci_schema.sql"))
        for f in MIGRATIONS_BEFORE:
            run(mig(f))
        run(mig("2026-11-26_payment_due.sql"))
        assert objects() == "0,0,0"
        base_cols = run(cols, is_file=False).stdout
        run(mig(MIG)); assert objects() == "1,1,1"
        run(mig(MIG)); assert objects() == "1,1,1"                       # idempotent
        run(mig(ROLL)); assert objects() == "0,0,0"                      # no data: runs
        assert run(cols, is_file=False).stdout == base_cols              # nothing else touched
        run(mig(ROLL))                                                   # a second run is harmless
        run(mig(MIG))
        run("INSERT INTO clients (company_name) VALUES ('C')", is_file=False)
        run("UPDATE clients SET default_credit_days = 0", is_file=False) # 0 (Contado) counts as a stored default
        p = run(mig(ROLL), check=False)
        assert p.returncode != 0 and "default payment term" in p.stderr and objects() == "1,1,1"
        run("UPDATE clients SET default_credit_days = NULL, default_payment_terms = '30 dias'", is_file=False)
        p = run(mig(ROLL), check=False)
        assert p.returncode != 0 and objects() == "1,1,1"
        assert run("SELECT default_payment_terms FROM clients", is_file=False).stdout.strip() == "30 dias"
        run("UPDATE clients SET default_payment_terms = NULL", is_file=False)
        run(mig(ROLL)); assert objects() == "0,0,0"                      # empty again: runs
    finally:
        conn = psycopg2.connect(admin_dsn); conn.autocommit = True
        conn.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'); conn.close()
