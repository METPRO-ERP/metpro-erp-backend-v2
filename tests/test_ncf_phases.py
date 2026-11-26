"""NCF fiscal sequence and phased invoicing (plan v2.1 section 8). Real Postgres (METPRO_LEDGER_TEST_DB).

Both switches default OFF. Tests turn them on with monkeypatch.setenv.
"""
import hashlib
import importlib
import os
import sys
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import (  # noqa: E402  (shared builders, client and DB fixture)
    NO_CHARGES, client, db as _base_db, needs_db, text, normalized, build, _rows, _no_db_pages,
)
from auth.service import create_access_token  # noqa: E402
from core.dates import business_today  # noqa: E402
from core import phased_invoicing as pi  # noqa: E402

GOLDEN_PAYMENTS_BUILD = "cfbf3aafdc02f45a99e815f8372241063fda21a0bb92d0698629a0fb5f64f34e"  # FACTURA with a payment, main ea07edf
GOLDEN_PLAIN_BUILD = "b414d6381150447637ff13f7305e8260dff1a9ef274d06d36ad4d906c1d2a733"      # FACTURA, no payments, main ea07edf
GOLDEN_ITEMS = [{"product_name": "Viga W10x12 largo 6 m", "quantity": 10, "unit_price": 105.0, "unit_of_measure": "und"},
                {"product_name": "Tornillo autoperforante", "quantity": 500, "unit_price": 0.25}]


def _hdr(role="admin"):
    token = create_access_token({"sub": f"t_{role}", "role": role, "user_id": "00000000-0000-0000-0000-000000000001"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def db(_base_db, monkeypatch):
    """The shared DB fixture, plus the fiscal and phase modules on the test connection and clean fiscal tables."""
    for mod in ("fiscal.service", "phases.service"):
        monkeypatch.setattr(importlib.import_module(mod), "get_db_connection", _base_db["connect"], raising=False)
    conn = _base_db["connect"]()
    cur = conn.cursor()
    cur.execute("TRUNCATE fiscal_sequence_audit, fiscal_sequences, invoice_phases RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
    for name in ("FISCAL_NCF_ENABLED", "PHASED_INVOICING_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return _base_db


def _on(monkeypatch, phased=False):
    monkeypatch.setenv("FISCAL_NCF_ENABLED", "true")
    if phased:
        monkeypatch.setenv("PHASED_INVOICING_ENABLED", "true")


def _batch(next_number=319, end=399, expiry=None, start=None, **over):
    body = {"series": "B", "next_number": next_number, "next_number_confirm": next_number,
            "range_start": start or next_number, "range_end": end,
            "expiry_date": (expiry or (business_today() + timedelta(days=365))).isoformat(), "confirm": True}
    body.update(over)
    return body


def _activate(**kw):
    r = client.post("/fiscal/sequence/activate", json=_batch(**kw), headers=_hdr())
    assert r.status_code == 200, r.text
    return r


def _quote(db, price=1000.0, approve=True):
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "project_name": "Torre Norte", "included_charges": NO_CHARGES,
            "items": [{"product_name": "Viga unica", "quantity": 1, "unit_price": price}]}
    qid = client.post("/quotes/", json=body, headers=_hdr()).json()["data"]["quote_id"]
    client.post(f"/quotes/{qid}/finalize", headers=_hdr())
    if approve:
        assert client.patch(f"/quotes/{qid}/status", json={"status": "Approved"}, headers=_hdr()).status_code == 200
    return qid


def _convert(qid, mode=None):
    body = {"billing_mode": mode} if mode else None
    return client.post(f"/quotes/{qid}/convert-to-invoice", json=body, headers=_hdr()) if body else \
        client.post(f"/quotes/{qid}/convert-to-invoice", headers=_hdr())


def _phased_quote(db, price=1000.0):
    qid = _quote(db, price)
    r = _convert(qid, "PHASED")
    assert r.status_code == 200 and r.json()["data"]["billing_mode"] == "PHASED", r.text
    return qid


def _phase(qid, kind="percent", value=None, label="Pago"):
    return client.post(f"/quotes/{qid}/phases", headers=_hdr(),
                       json={"label": label, "amount_input_type": kind, "amount_input_value": value})


def _invoice_phase(phase_id):
    return client.post(f"/phases/{phase_id}/invoice", headers=_hdr())


def _quote_totals(db, qid):
    row = _rows(db, "SELECT grand_total FROM quotes WHERE quote_id = %s", (qid,))[0]
    return Decimal(str(row["grand_total"]))


def _status(db, qid):
    return _rows(db, "SELECT status FROM quotes WHERE quote_id = %s", (qid,))[0]["status"]


# ============================================================ pure rules (no database)

def test_phase_itbis_pure_specification_30_40_30():
    total, itbis = Decimal("10000.01"), Decimal("1525.43")
    p1 = pi.amount_from_percent(30, total)
    p2 = pi.amount_from_percent(40, total)
    p3 = total - p1 - p2
    assert (p1, p2, p3) == (Decimal("3000.00"), Decimal("4000.00"), Decimal("3000.01"))
    t1 = pi.phase_itbis(p1, total, itbis, [], False)
    t2 = pi.phase_itbis(p2, total, itbis, [t1], False)
    t3 = pi.phase_itbis(p3, total, itbis, [t1, t2], True)
    assert (t1, t2, t3) == (Decimal("457.63"), Decimal("610.17"), Decimal("457.63"))
    assert t1 + t2 + t3 == itbis
    assert [p1 - t1, p2 - t2, p3 - t3] == [Decimal("2542.37"), Decimal("3389.83"), Decimal("2542.38")]


@pytest.mark.parametrize("total,itbis,pcts", [
    ("1239.00", "189.00", (33, 33)), ("98765.43", "15066.62", (10, 25, 15)), ("0.07", "0.01", (50,)),
    ("5000000.99", "762711.00", (12, 37, 21, 9)),
])
def test_phase_itbis_always_sums_exactly(total, itbis, pcts):
    total, itbis = Decimal(total), Decimal(itbis)
    amounts, taxes = [], []
    for p in pcts:
        a = pi.amount_from_percent(p, total)
        if a <= 0:
            continue
        taxes.append(pi.phase_itbis(a, total, itbis, taxes, False))
        amounts.append(a)
    final = total - sum(amounts)
    taxes.append(pi.phase_itbis(final, total, itbis, taxes, True))
    amounts.append(final)
    assert sum(amounts) == total and sum(taxes) == itbis and all(t <= a for t, a in zip(taxes, amounts))


def test_phased_requires_fiscal_switch(monkeypatch):
    monkeypatch.delenv("FISCAL_NCF_ENABLED", raising=False)
    monkeypatch.setenv("PHASED_INVOICING_ENABLED", "true")
    assert pi.phased_enabled() is False
    monkeypatch.setenv("FISCAL_NCF_ENABLED", "yes")
    assert pi.phased_enabled() is True
    monkeypatch.setenv("PHASED_INVOICING_ENABLED", "off")
    assert pi.phased_enabled() is False


def test_ncf_format():
    from core.fiscal_ncf import format_ncf
    assert format_ncf("B", "01", 319) == "B0100000319"
    assert format_ncf("B", "01", 99999999) == "B0199999999"


def test_new_error_codes_are_registered_with_spanish_messages():
    from core.billing_errors import ERRORS, billing_error
    for code, (status, message) in ERRORS.items():
        exc = billing_error(code)
        assert exc.status_code == status and exc.code == code and exc.detail == message
    assert ERRORS["INVOICE_HAS_NCF"][1] == "Esta factura tiene NCF. Use Anular en lugar de Eliminar"


# ============================================================ golden hashes (switch OFF = old bytes)

def _golden_build(**kw):
    return hashlib.sha256(normalized(build(doc_type="FACTURA", doc_id="F-10002", items=[dict(i) for i in GOLDEN_ITEMS], **kw))).hexdigest()


def test_switch_off_pdf_byte_identical():
    got = _golden_build(payments=[{"payment_date": "2026-10-24", "amount": 500.0, "method": "Transferencia"}],
                        amount_paid=500.0, amount_due=739.0, valid_until="", payment_terms="Contado")
    assert got == GOLDEN_PAYMENTS_BUILD


def test_switch_off_no_ncf_pdf_byte_identical():
    assert _golden_build() == GOLDEN_PLAIN_BUILD
    # An explicit "no NCF" is the same bytes as not passing it.
    assert _golden_build(ncf=None, ncf_valid_until=None) == GOLDEN_PLAIN_BUILD


def test_pdf_with_ncf_shows_title_ncf_and_expiry_and_keeps_footer_free_of_legends():
    from datetime import date
    t = text(build(doc_type="FACTURA", doc_id="F-10002", ncf="B0100000319", ncf_valid_until=date(2027, 3, 31)))
    assert "Factura de Cr" in t and "NCF: B0100000319" in t and "Vencimiento NCF: 31/03/2027" in t
    assert "lido hasta" not in t   # the old fiscal label is gone (the quote line reads "Válida Hasta", capital H)
    assert "F-10002" in t
    for banned in ("32-23", "QR", "Firma Digital", "digo de Seguridad"):
        assert banned not in t


# ============================================================ activation (admin)

@needs_db
def test_activation_blocked_when_next_number_entries_differ(db):
    r = client.post("/fiscal/sequence/activate", json=_batch(next_number_confirm=320), headers=_hdr())
    assert r.status_code == 400 and r.json()["code"] == "NCF_NEXT_NUMBER_MISMATCH"
    assert _rows(db, "SELECT count(*) AS n FROM fiscal_sequences")[0]["n"] == 0


@needs_db
def test_activation_blocked_when_values_missing(db):
    body = _batch()
    body.pop("expiry_date")
    r = client.post("/fiscal/sequence/activate", json=body, headers=_hdr())
    assert r.status_code in (400, 422) and r.json()["code"] == "VALIDATION_ERROR"
    assert _rows(db, "SELECT count(*) AS n FROM fiscal_sequences")[0]["n"] == 0


@needs_db
def test_activation_confirmation_screen_writes_nothing_until_confirmed(db):
    preview = client.post("/fiscal/sequence/activate", json=_batch(confirm=False), headers=_hdr()).json()["data"]
    assert preview["confirmed"] is False and preview["batch"]["next_ncf"] == "B0100000319"
    assert preview["batch"]["last_ncf"] == "B0100000399" and preview["batch"]["quantity"] == 81
    assert _rows(db, "SELECT count(*) AS n FROM fiscal_sequences")[0]["n"] == 0
    done = _activate()
    assert done.json()["data"]["confirmed"] is True
    row = _rows(db, "SELECT * FROM fiscal_sequences")[0]
    assert row["active"] and not row["locked"] and row["next_number"] == 319
    assert _rows(db, "SELECT action FROM fiscal_sequence_audit")[0]["action"] == "activate"
    assert os.environ.get("FISCAL_NCF_ENABLED") is None  # activation never turns the switch on


@needs_db
def test_activation_admin_only(db):
    r = client.post("/fiscal/sequence/activate", json=_batch(), headers=_hdr("user"))
    assert r.status_code == 403


@needs_db
def test_activation_correction_allowed_before_first_ncf(db):
    _activate()
    r = client.post("/fiscal/sequence/correct", json=_batch(next_number=320, end=410), headers=_hdr())
    assert r.status_code == 200, r.text
    row = _rows(db, "SELECT * FROM fiscal_sequences")[0]
    assert row["next_number"] == 320 and row["range_end"] == 410
    assert [a["action"] for a in _rows(db, "SELECT action FROM fiscal_sequence_audit ORDER BY id")] == ["activate", "correct"]


@needs_db
def test_activation_correction_blocked_after_first_ncf(db, monkeypatch):
    _on(monkeypatch)
    _activate()
    assert _convert(_quote(db)).status_code == 200
    r = client.post("/fiscal/sequence/correct", json=_batch(next_number=500, end=600), headers=_hdr())
    assert r.status_code == 400 and r.json()["code"] == "NCF_BATCH_LOCKED"
    again = client.post("/fiscal/sequence/activate", json=_batch(), headers=_hdr())
    assert again.status_code == 400 and again.json()["code"] == "NCF_BATCH_LOCKED"
    # The database enforces the same lock, whatever the application does.
    import psycopg2
    conn = db["connect"]()
    cur = conn.cursor()
    with pytest.raises(psycopg2.Error):
        cur.execute("UPDATE fiscal_sequences SET range_end = 9999")
    conn.rollback()
    with pytest.raises(psycopg2.Error):
        cur.execute("DELETE FROM fiscal_sequences")
    conn.rollback()
    conn.close()


# ============================================================ assignment, FULL

@needs_db
def test_full_conversion_unchanged(db, monkeypatch):
    qid = _quote(db, 1000.0)
    r = _convert(qid)
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == {"invoice_id", "invoice_number", "quote_id", "client_id", "total_amount", "status", "invoice_date", "message"}
    inv = _rows(db, "SELECT * FROM invoices WHERE id = %s", (data["invoice_id"],))[0]
    assert inv["ncf"] is None and inv["fiscal_sequence_id"] is None and inv["ncf_assigned_at"] is None and inv["phase_id"] is None
    assert inv["status"] == "Pending" and Decimal(str(inv["total_amount"])) == _quote_totals(db, qid)
    assert _rows(db, "SELECT count(*) AS n FROM invoice_items WHERE invoice_id = %s", (inv["id"],))[0]["n"] == 1
    assert _status(db, qid) == "Invoiced"
    assert _rows(db, "SELECT billing_mode FROM quotes WHERE quote_id = %s", (qid,))[0]["billing_mode"] == "FULL"


@needs_db
def test_no_backfill_pre_activation_invoice_has_null_ncf(db, monkeypatch):
    old = _convert(_quote(db)).json()["data"]["invoice_id"]
    _activate()
    _on(monkeypatch)
    assert _rows(db, "SELECT ncf FROM invoices WHERE id = %s", (old,))[0]["ncf"] is None
    new = _convert(_quote(db)).json()["data"]
    assert new["ncf"] == "B0100000319"
    assert _rows(db, "SELECT ncf FROM invoices WHERE id = %s", (old,))[0]["ncf"] is None


@needs_db
def test_switch_off_assigns_no_ncf_even_with_an_active_batch(db):
    _activate()
    data = _convert(_quote(db)).json()["data"]
    assert "ncf" not in data
    assert _rows(db, "SELECT next_number, locked FROM fiscal_sequences")[0] == {"next_number": 319, "locked": False}


@needs_db
def test_conversion_blocked_without_an_active_sequence(db, monkeypatch):
    _on(monkeypatch)
    qid = _quote(db)
    r = _convert(qid)
    assert r.status_code == 400 and r.json()["code"] == "NCF_NO_ACTIVE_SEQUENCE"
    assert _status(db, qid) == "Approved"


@needs_db
def test_full_and_phase_get_distinct_consecutive_ncf(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    a = _convert(_quote(db)).json()["data"]
    qb = _phased_quote(db)
    p1 = _phase(qb, "percent", 50).json()["data"]["id"]
    b1 = _invoice_phase(p1).json()["data"]
    p2 = _phase(qb, "final", None, "Pago final").json()["data"]["id"]
    b2 = _invoice_phase(p2).json()["data"]
    assert [a["ncf"], b1["ncf"], b2["ncf"]] == ["B0100000319", "B0100000320", "B0100000321"]
    assert _rows(db, "SELECT next_number FROM fiscal_sequences")[0]["next_number"] == 322


@needs_db
def test_cancelled_ncf_not_reused_reinvoice_gets_new(db, monkeypatch):
    _on(monkeypatch)
    _activate()
    qid = _quote(db)
    first = _convert(qid).json()["data"]
    assert client.patch(f"/invoices/{first['invoice_id']}/status", json={"status": "Cancelled"}, headers=_hdr()).status_code == 200
    assert _rows(db, "SELECT ncf, status FROM invoices WHERE id = %s", (first["invoice_id"],))[0] == {"ncf": "B0100000319", "status": "Cancelled"}
    second = _convert(qid).json()["data"]
    assert second["ncf"] == "B0100000320" and second["invoice_number"].endswith("-2")


@needs_db
def test_delete_blocked_when_ncf_set(db, monkeypatch):
    _on(monkeypatch)
    _activate()
    iid = _convert(_quote(db)).json()["data"]["invoice_id"]
    r = client.delete(f"/invoices/{iid}", headers=_hdr())
    assert r.status_code == 400 and r.json()["code"] == "INVOICE_HAS_NCF"
    assert _rows(db, "SELECT count(*) AS n FROM invoices WHERE id = %s", (iid,))[0]["n"] == 1
    # Switch off now: an invoice that holds an NCF is still protected.
    monkeypatch.delenv("FISCAL_NCF_ENABLED")
    assert client.delete(f"/invoices/{iid}", headers=_hdr()).json()["code"] == "INVOICE_HAS_NCF"


@needs_db
def test_delete_without_ncf_behaves_as_before(db):
    iid = _convert(_quote(db)).json()["data"]["invoice_id"]
    assert client.delete(f"/invoices/{iid}", headers=_hdr()).status_code == 200


# ============================================================ expiry, exhaustion, warnings

@needs_db
def test_expired_batch_blocks_full_and_phase(db, monkeypatch):
    _on(monkeypatch, phased=True)
    qp = _phased_quote(db)
    pid = _phase(qp, "percent", 50).json()["data"]["id"]
    _activate(expiry=business_today() - timedelta(days=1))
    qf = _quote(db)
    r = _convert(qf)
    assert r.status_code == 400 and r.json()["code"] == "NCF_SEQUENCE_EXPIRED"
    r = _invoice_phase(pid)
    assert r.status_code == 400 and r.json()["code"] == "NCF_SEQUENCE_EXPIRED"
    assert _status(db, qf) == "Approved"
    assert _rows(db, "SELECT count(*) AS n FROM invoices")[0]["n"] == 0


@needs_db
def test_exhausted_batch_blocks_full_and_phase(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate(next_number=319, end=319)   # exactly one number left
    qp = _phased_quote(db)
    pid = _phase(qp, "percent", 50).json()["data"]["id"]
    assert _convert(_quote(db)).status_code == 200            # takes 319
    r = _invoice_phase(pid)
    assert r.status_code == 400 and r.json()["code"] == "NCF_SEQUENCE_EXHAUSTED"
    r = _convert(_quote(db))
    assert r.status_code == 400 and r.json()["code"] == "NCF_SEQUENCE_EXHAUSTED"
    assert _rows(db, "SELECT next_number FROM fiscal_sequences")[0]["next_number"] == 320


@needs_db
@pytest.mark.parametrize("days,warns", [(30, True), (31, False), (1, True)])
def test_near_expiry_warning_fires(db, monkeypatch, days, warns):
    _on(monkeypatch)
    _activate(expiry=business_today() + timedelta(days=days))
    status = client.get("/fiscal/sequence", headers=_hdr()).json()["data"]
    assert status["warn_expiry"] is warns and status["days_to_expiry"] == days
    out = _convert(_quote(db)).json()["data"]
    assert (out.get("ncf_warning") or {}).get("expiry", False) is warns


@needs_db
@pytest.mark.parametrize("remaining,warns", [(20, True), (21, False), (1, True)])
def test_near_exhaustion_warning_fires(db, monkeypatch, remaining, warns):
    _on(monkeypatch)
    _activate(next_number=319, end=319 + remaining - 1)
    status = client.get("/fiscal/sequence", headers=_hdr()).json()["data"]
    assert status["remaining"] == remaining and status["warn_exhaustion"] is warns
    out = _convert(_quote(db)).json()["data"]
    assert (out.get("ncf_warning") or {}).get("exhaustion", False) is warns


@needs_db
def test_status_panel_reports_the_batch(db):
    assert client.get("/fiscal/sequence", headers=_hdr()).json()["data"]["configured"] is False
    _activate(next_number=319, end=399)
    s = client.get("/fiscal/sequence", headers=_hdr()).json()["data"]
    assert (s["series"], s["next_ncf"], s["remaining"], s["locked"], s["state"]) == ("B", "B0100000319", 81, False, "ok")
    assert client.get("/fiscal/sequence", headers=_hdr("user")).status_code == 403


# ============================================================ concurrency and rollback

@needs_db
def test_concurrent_conversions_never_share_ncf(db, monkeypatch):
    _on(monkeypatch)
    _activate()
    qids = [_quote(db, 100.0 + n) for n in range(8)]
    from quotes.service import convert_quote_to_invoice
    results, errors = [], []

    def run(qid):
        try:
            results.append(convert_quote_to_invoice(qid, "00000000-0000-0000-0000-000000000001")["ncf"])
        except Exception as e:  # pragma: no cover - reported by the assertion below
            errors.append(repr(e))

    threads = [threading.Thread(target=run, args=(q,)) for q in qids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(set(results)) == 8
    assert sorted(results) == [f"B01{n:08d}" for n in range(319, 327)]
    assert _rows(db, "SELECT next_number FROM fiscal_sequences")[0]["next_number"] == 327


@needs_db
def test_failed_conversion_does_not_burn_number(db, monkeypatch):
    _on(monkeypatch)
    _activate()
    qid = _quote(db)
    import quotes.service as qsvc

    def boom(*a, **k):
        raise RuntimeError("insert failed after the NCF was taken")

    with monkeypatch.context() as m:
        m.setattr(qsvc, "execute_values", boom)
        r = _convert(qid)
    assert r.status_code >= 400
    assert _rows(db, "SELECT next_number, locked FROM fiscal_sequences")[0] == {"next_number": 319, "locked": False}
    assert _rows(db, "SELECT count(*) AS n FROM invoices")[0]["n"] == 0 and _status(db, qid) == "Approved"
    assert _convert(qid).json()["data"]["ncf"] == "B0100000319"   # the same number is issued next


# ============================================================ phases

@needs_db
def test_switch_off_hides_mode_and_blocks_phase_endpoints(db, monkeypatch):
    qid = _quote(db)
    assert client.get("/phases/options", headers=_hdr()).json()["data"]["phased_enabled"] is False
    for r in (_convert(qid, "PHASED"), client.get(f"/quotes/{qid}/phases", headers=_hdr()), _phase(qid, "percent", 30),
              _invoice_phase(1)):
        assert r.status_code == 404 and r.json()["code"] == "FEATURE_DISABLED"
    monkeypatch.setenv("PHASED_INVOICING_ENABLED", "true")   # phases on, NCF still off: still unavailable
    assert _convert(qid, "PHASED").status_code == 404
    assert _status(db, qid) == "Approved"


@needs_db
def test_phase_worked_example_30_40_30(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 8474.61)
    conn = db["connect"]()
    conn.cursor().execute("UPDATE quotes SET grand_total = 10000.01 WHERE quote_id = %s", (qid,))
    conn.commit()
    conn.close()
    out = []
    for kind, value, label in (("percent", 30, "Pago inicial / depósito"), ("percent", 40, "Segundo pago"), ("final", None, "Pago final")):
        phase = _phase(qid, kind, value, label)
        assert phase.status_code == 200, phase.text
        out.append(phase.json()["data"])
        assert _invoice_phase(out[-1]["id"]).status_code == 200
    assert [o["amount"] for o in out] == [3000.00, 4000.00, 3000.01]
    assert [o["itbis"] for o in out] == [457.63, 610.17, 457.63]
    assert [o["base"] for o in out] == [2542.37, 3389.83, 2542.38]
    rows = _rows(db, "SELECT total_amount, itbis_total, subtotal, grand_total FROM invoices ORDER BY id")
    assert [float(r["total_amount"]) for r in rows] == [3000.00, 4000.00, 3000.01]
    assert [float(r["itbis_total"]) for r in rows] == [457.63, 610.17, 457.63]
    assert [float(r["subtotal"]) for r in rows] == [2542.37, 3389.83, 2542.38]
    assert sum(Decimal(str(r["total_amount"])) for r in rows) == Decimal("10000.01")


@needs_db
def test_phase_itbis_sums_exactly_to_quote(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 3333.33)
    for kind, value in (("percent", 33), ("percent", 33), ("final", None)):
        pid = _phase(qid, kind, value).json()["data"]["id"]
        assert _invoice_phase(pid).status_code == 200
    rows = _rows(db, "SELECT total_amount, itbis_total FROM invoices")
    quote = _rows(db, "SELECT grand_total FROM quotes WHERE quote_id = %s", (qid,))[0]
    assert sum(Decimal(str(r["total_amount"])) for r in rows) == Decimal(str(quote["grand_total"]))
    from quotes.service import calculate_quote_totals
    expected_itbis = round(calculate_quote_totals([{"quantity": 1, "unit_price": 3333.33}], NO_CHARGES, 0.18)["itbis"], 2)
    assert sum(Decimal(str(r["itbis_total"])) for r in rows) == Decimal(str(expected_itbis))


@needs_db
def test_phase_amount_above_remaining_blocked(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    total = _quote_totals(db, qid)
    r = _phase(qid, "fixed", float(total) + 0.01)
    assert r.status_code == 400 and r.json()["code"] == "PHASE_EXCEEDS_BALANCE"
    first = _phase(qid, "fixed", float(total) - 100)
    assert first.status_code == 200
    assert _invoice_phase(first.json()["data"]["id"]).status_code == 200
    r = _phase(qid, "fixed", 100.01)
    assert r.status_code == 400 and r.json()["code"] == "PHASE_EXCEEDS_BALANCE"
    assert _phase(qid, "fixed", 100.0).status_code == 200


@needs_db
def test_zero_phase_amount_blocked(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    for kind, value in (("fixed", 0), ("percent", 0), ("fixed", -5)):
        r = _phase(qid, kind, value)
        assert r.status_code == 400 and r.json()["code"] == "PHASE_AMOUNT_ZERO"
    assert _phase(qid, "percent", 0.0001).status_code == 400   # rounds to 0.00
    assert _rows(db, "SELECT count(*) AS n FROM invoice_phases")[0]["n"] == 0


@needs_db
def test_phase_cannot_be_invoiced_twice(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    pid = _phase(qid, "percent", 40).json()["data"]["id"]
    assert _invoice_phase(pid).status_code == 200
    r = _invoice_phase(pid)
    assert r.status_code == 400 and r.json()["code"] == "PHASE_ALREADY_INVOICED"
    assert _rows(db, "SELECT count(*) AS n FROM invoices")[0]["n"] == 1
    assert _rows(db, "SELECT next_number FROM fiscal_sequences")[0]["next_number"] == 320   # no NCF taken by the refusal
    assert _invoice_phase(99999).json()["code"] == "PHASE_NOT_FOUND"


@needs_db
def test_one_open_phase_at_a_time(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    assert _phase(qid, "percent", 10).status_code == 200
    r = _phase(qid, "percent", 10)
    assert r.status_code == 400 and r.json()["code"] == "PHASE_PLANNED_EXISTS"


@needs_db
def test_cumulative_invoiced_never_exceeds_total(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 777.77)
    total = _quote_totals(db, qid)
    for value in (25, 25, 25, 20):
        pid = _phase(qid, "percent", value).json()["data"]["id"]
        assert _invoice_phase(pid).status_code == 200
        invoiced = sum(Decimal(str(r["total_amount"])) for r in _rows(db, "SELECT total_amount FROM invoices WHERE status <> 'Cancelled'"))
        assert invoiced <= total
    last = _phase(qid, "final")
    assert last.status_code == 200 and _invoice_phase(last.json()["data"]["id"]).status_code == 200
    assert sum(Decimal(str(r["total_amount"])) for r in _rows(db, "SELECT total_amount FROM invoices")) == total
    r = _phase(qid, "percent", 1)
    assert r.status_code == 400 and r.json()["code"] in ("PHASE_NOTHING_REMAINING", "QUOTE_NOT_APPROVED")


@needs_db
def test_final_phase_takes_exact_remaining(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 1234.57)
    total = _quote_totals(db, qid)
    p1 = _phase(qid, "percent", 33.33).json()["data"]
    assert _invoice_phase(p1["id"]).status_code == 200
    fin = _phase(qid, "final", 12345).json()["data"]    # the typed value is ignored
    assert Decimal(str(fin["amount"])) == total - Decimal(str(p1["amount"])) and fin["is_final"] is True
    assert fin["remaining_after"] == 0


@needs_db
def test_phase_invoice_has_no_quote_lines(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _quote(db, 500.0)
    # a second quote line so there is something to (not) copy
    conn = db["connect"]()
    conn.cursor().execute("INSERT INTO quote_items (quote_id, product_name, quantity, unit_price) VALUES (%s, 'Tornillo', 3, 10)", (qid,))
    conn.commit()
    conn.close()
    assert _convert(qid, "PHASED").status_code == 200
    pid = _phase(qid, "percent", 30, "Pago inicial / depósito").json()["data"]["id"]
    iid = _invoice_phase(pid).json()["data"]["invoice_id"]
    lines = _rows(db, "SELECT description, quantity, total FROM invoice_items WHERE invoice_id = %s", (iid,))
    assert len(lines) == 1 and lines[0]["description"] == "Pago inicial / depósito"
    inv = _rows(db, "SELECT subtotal, itbis_total, total_amount, charges_total, discount_total, phase_id FROM invoices WHERE id = %s", (iid,))[0]
    assert Decimal(str(lines[0]["total"])) == Decimal(str(inv["subtotal"])) and inv["phase_id"] == pid
    assert inv["charges_total"] == 0 and inv["discount_total"] == 0


@needs_db
def test_quote_invoiced_only_after_final_phase(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    assert _status(db, qid) == "Approved"
    p1 = _phase(qid, "percent", 50).json()["data"]["id"]
    assert _invoice_phase(p1).json()["data"]["quote_fully_invoiced"] is False
    assert _status(db, qid) == "Approved"
    p2 = _phase(qid, "final").json()["data"]["id"]
    assert _invoice_phase(p2).json()["data"]["quote_fully_invoiced"] is True
    assert _status(db, qid) == "Invoiced"
    # A cancelled phase invoice reopens the quote and returns its amount to the balance.
    iid = _rows(db, "SELECT invoice_id FROM invoice_phases WHERE id = %s", (p2,))[0]["invoice_id"]
    assert client.patch(f"/invoices/{iid}/status", json={"status": "Cancelled"}, headers=_hdr()).status_code == 200
    assert _status(db, qid) == "Approved"
    assert _phase(qid, "final").json()["data"]["amount"] > 0


@needs_db
def test_quote_total_change_blocked_after_first_phase(db, monkeypatch):
    import quotes.service as qsvc
    monkeypatch.setattr(qsvc, "ALLOW_EDIT_NON_DRAFT_QUOTES", True)
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    body = {"items": [{"product_name": "Viga unica", "quantity": 2, "unit_price": 1000.0}]}
    pid = _phase(qid, "percent", 50).json()["data"]["id"]
    assert client.put(f"/quotes/{qid}", json=body, headers=_hdr()).status_code == 200   # no phase invoice yet
    assert _invoice_phase(pid).status_code == 200
    r = client.put(f"/quotes/{qid}", json=body, headers=_hdr())
    assert r.status_code == 400 and r.json()["code"] == "QUOTE_TOTAL_LOCKED_PHASED"
    # A change that is not money (project name) is still allowed.
    assert client.put(f"/quotes/{qid}", json={"project_name": "Torre Sur"}, headers=_hdr()).status_code == 200


@needs_db
def test_mode_cannot_change_after_first_phase(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    pid = _phase(qid, "percent", 50).json()["data"]["id"]
    r = _convert(qid)   # FULL on a phased quote that already has a phase
    assert r.status_code == 400 and r.json()["code"] == "BILLING_MODE_LOCKED"
    assert _invoice_phase(pid).status_code == 200
    assert _convert(qid).status_code == 400
    assert _rows(db, "SELECT billing_mode FROM quotes WHERE quote_id = %s", (qid,))[0]["billing_mode"] == "PHASED"


@needs_db
def test_phased_mode_without_phases_can_return_to_full(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    r = _convert(qid)
    assert r.status_code == 200 and r.json()["data"]["ncf"] == "B0100000319"
    assert _rows(db, "SELECT billing_mode FROM quotes WHERE quote_id = %s", (qid,))[0]["billing_mode"] == "FULL"


@needs_db
def test_phase_invoice_pdf_uses_stored_itbis_and_prints_the_ncf(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 1000.0)
    iid = _invoice_phase(_phase(qid, "percent", 40, "Pago inicial / depósito").json()["data"]["id"]).json()["data"]["invoice_id"]
    pdf = client.get(f"/pdf/invoices/{iid}", headers=_hdr())
    t = text(pdf.content)
    assert pdf.status_code == 200 and "NCF: B0100000319" in t and "Factura de Cr" in t and "Vencimiento NCF" in t
    assert "Pago inicial" in t and "Supervisi" not in t
    # Tamper with the stored ITBIS: the PDF follows the stored value, it never recomputes tax from the summary line.
    conn = db["connect"]()
    conn.cursor().execute("UPDATE invoices SET itbis_total = 123.45, subtotal = total_amount - 123.45 WHERE id = %s", (iid,))
    conn.commit()
    conn.close()
    assert "123.45" in text(client.get(f"/pdf/invoices/{iid}", headers=_hdr()).content)


@needs_db
def test_phase_itbis_stored_and_not_recomputed_in_pdf(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 1000.0)
    iid = _invoice_phase(_phase(qid, "percent", 40).json()["data"]["id"]).json()["data"]["invoice_id"]
    stored = _rows(db, "SELECT itbis_total, subtotal, total_amount FROM invoices WHERE id = %s", (iid,))[0]
    pitbis = _rows(db, "SELECT itbis_snapshot FROM invoice_phases WHERE invoice_id = %s", (iid,))[0]["itbis_snapshot"]
    assert stored["itbis_total"] == pitbis
    import invoices.service as isvc
    inv = isvc.get_invoice_with_contact(iid)
    items, totals = isvc.invoice_lines_and_totals(inv, inv["included_charges"])
    assert len(items) == 1 and totals["lines_source"] == "phase_stored"
    assert Decimal(str(totals["itbis"])) == Decimal(str(stored["itbis_total"]))
    assert Decimal(str(totals["grand_total"])) == Decimal(str(stored["total_amount"]))
    # The tampered-value case (test_phase_invoice_pdf_uses_stored_itbis...) proves the stored value wins over any recomputation.
    assert totals["itbis"] == float(stored["itbis_total"])


@needs_db
def test_phase_rows_are_immutable_once_invoiced(db, monkeypatch):
    import psycopg2
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db)
    pid = _phase(qid, "percent", 40).json()["data"]["id"]
    _invoice_phase(pid)
    conn = db["connect"]()
    cur = conn.cursor()
    with pytest.raises(psycopg2.Error):
        cur.execute("UPDATE invoice_phases SET amount_snapshot = 1 WHERE id = %s", (pid,))
    conn.rollback()
    with pytest.raises(psycopg2.Error):
        cur.execute("DELETE FROM invoice_phases WHERE id = %s", (pid,))
    conn.rollback()
    conn.close()


@needs_db
def test_phases_list_and_aggregate(db, monkeypatch):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _phased_quote(db, 1000.0)
    total = float(_quote_totals(db, qid))
    iid = _invoice_phase(_phase(qid, "percent", 40).json()["data"]["id"]).json()["data"]["invoice_id"]
    client.post(f"/invoices/{iid}/payments", json={"amount": 100.0, "method": "cash"}, headers=_hdr())
    out = client.get(f"/quotes/{qid}/phases", headers=_hdr()).json()["data"]
    agg = out["aggregate"]
    assert out["billing_mode"] == "PHASED" and len(out["phases"]) == 1 and out["phases"][0]["ncf"] == "B0100000319"
    assert agg["total"] == total and agg["invoiced_to_date"] == round(total * 0.4, 2)
    assert agg["pending_to_invoice"] == round(total - agg["invoiced_to_date"], 2)
    assert agg["pending_to_pay"] == round(agg["invoiced_to_date"] - agg["paid_to_date"], 2)


@needs_db
def test_migration_files_exist_with_rollbacks():
    base = os.path.join(os.path.dirname(__file__), "..", "migrations")
    for name in ("2026-11-25_fiscal_sequence_ncf", "2026-11-25_phased_invoicing"):
        assert os.path.isfile(os.path.join(base, name + ".sql")) and os.path.isfile(os.path.join(base, name + ".rollback.sql"))
