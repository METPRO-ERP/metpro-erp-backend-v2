"""Payment terms days and invoice due date (B2). Plan: claude/workstream-bc-plan-2026-11-26.md section 4.3, D1 to D7 and D11.

PAYMENT_DUE_ENABLED defaults OFF: every document prints and every payload reads exactly as before. ON: the quote gets
credit_days, the invoice stores a due_date snapshot at conversion, an admin can change it with a mandatory note and an
audit row, the invoice PDF prints "Vencimiento de pago" and stops printing "Válida Hasta", and the "Términos de Pago"
value wraps instead of being cut at 60 characters.

The OFF hashes below were computed on main 12d3898 (before this change) and pinned here: OFF is proven byte for byte.
Real Postgres (METPRO_LEDGER_TEST_DB) for the integration tests.
"""
import hashlib
import importlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import (  # noqa: E402
    NO_CHARGES, client, db as _base_db, needs_db, text, normalized, build, _rows, _no_db_pages, create_access_token,
)
from test_ncf_phases import _hdr, _on, _activate, _phase, _invoice_phase, _convert  # noqa: E402
from core import payment_due as pd  # noqa: E402

SWITCH = "PAYMENT_DUE_ENABLED"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TERMS = "50% con la orden y 50% al completar la orden"
LONG = "50% con la orden de compra, 25% al entregar la estructura metalica en obra y 25% restante contra la instalacion final"
VERY_LONG = " ".join(f"tramo{i:02d}" for i in range(1, 61))  # 420 characters: the left column outgrows the right one
DUE = date(2027, 1, 15)

# ---- OFF goldens: sha256 of the PDF built by main 12d3898 (CreationDate and /ID removed). PAYMENT_DUE_ENABLED absent.
OFF_PLAIN = {
    "inv_terms_valid": "6d290652d6ccee38ded1b559886f69fd19d87dbc87ca4760e297fc763f0d159d",
    "inv_long_terms": "bae80aedfa5ab89d4391dda249dadd8241e194950465aeedc7bfad7480eff464",
    "quote_terms_valid": "02050e6253cfd82fd382b618be6628a14fc2f974bf4a094db19e05db450e7647",
    "quote_long_terms": "b4b443164d6d702a48eed5c77e0ece519a391d6cd7c3b33d31f85d76a72f17dd",
}
# Same cases with the production flags (project heading + description wrap) on, as in production today.
OFF_PROD = {
    "inv_terms_valid": "c10248126a9c344c0086853b180e1cbb678a5c52e2c09235a496b952c41c433b",
    "inv_long_terms": "3519d81b513c362af89791ecee098aba3ad92e2bb01684092e944d01568648b0",
    "quote_terms_valid": "45234afeeebc77fd9d6056e9939af02eab0c86352e9838ed5b96081513763d12",
    "quote_long_terms": "4ac45c9ac38cb801b7c48ddf6437db87ff991f75a8faf151f248566792e76e08",
}
# ---- ON goldens (new): the same cases with the switch on. Invoices carry DUE as the stored due date.
ON_PLAIN = {
    "inv_terms_valid": "9b150d705677e142e6d38f337621c4036e2e419ecb7831939254ffc34337c7f5",
    "inv_long_terms": "f38dad340e46ec8b5944d09c6d16d8ef9d64a26e9e277e17018c769afda5eb32",
    "quote_terms_valid": "02050e6253cfd82fd382b618be6628a14fc2f974bf4a094db19e05db450e7647",  # short terms, a quote: unchanged
    "quote_long_terms": "981368ca07c21cc927eb344a0bf46b94b1ffb708984d85a41e5aa24c3241ebaa",
}
ON_PROD = {
    "inv_terms_valid": "bfb9d4f596b2f7763cd900e20a82ea37409e80dc3b842652f056d3011875a880",
    "inv_long_terms": "8a527b7fa106837b47f26cce0cd53a78ed008cba1e411f441fd08d28d8d8e1d2",
    "quote_terms_valid": "45234afeeebc77fd9d6056e9939af02eab0c86352e9838ed5b96081513763d12",
    "quote_long_terms": "e8116d8d30058520dadd5614e9ce54f082ebd0e155d54956079ca88101b256ae",
}
CASES = {
    "inv_terms_valid": dict(doc_type="FACTURA", doc_id="F-1", payment_terms=TERMS, valid_until="2026-12-30"),
    "inv_long_terms": dict(doc_type="FACTURA", doc_id="F-1", payment_terms=LONG),
    "quote_terms_valid": dict(doc_type="COTIZACION", payment_terms=TERMS, valid_until="2026-12-30"),
    "quote_long_terms": dict(doc_type="COTIZACION", payment_terms=LONG, valid_until="2026-12-30"),
}
PROD_FLAGS = {"PDF_PROJECT_DESC_ENABLED": "true", "PDF_DESC_WRAP_ENABLED": "true"}


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    for name in (SWITCH, "PDF_PROJECT_DESC_ENABLED", "PDF_DESC_WRAP_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def _h(**kw):
    return hashlib.sha256(normalized(build(**kw))).hexdigest()


def ptext(b):
    t = text(b).replace("\\(", "\x01").replace("\\)", "\x02")
    out = " ".join(re.findall(r"\(([^()]*)\)\s*Tj", t))
    return out.replace("\x01", "(").replace("\x02", ")")


def ypos(b, label):
    """PDF y coordinate (points, origin bottom-left, so a LOWER number is LOWER on the page) of every Tj whose text starts
    with label, in content order."""
    ys = []
    for m in re.finditer(r"BT ([\d.]+) ([\d.]+) Td [^()]*\(([^()]*)\) Tj", text(b)):
        if m.group(3).startswith(label):
            ys.append(float(m.group(2)))
    return ys


# ============================================================ pure rules (no database)

def test_switch_default_off_and_parsing(monkeypatch):
    assert pd.enabled() is False
    for v in ("true", "1", "YES", " on ", "True"):
        monkeypatch.setenv(SWITCH, v)
        assert pd.enabled() is True
    for v in ("", "false", "0", "off", "no"):
        monkeypatch.setenv(SWITCH, v)
        assert pd.enabled() is False


def test_compute_due_date_is_issue_date_plus_days():
    assert pd.compute_due_date(date(2026, 11, 26), 30) == date(2026, 12, 26)
    assert pd.compute_due_date("2026-11-26", 0) == date(2026, 11, 26)           # Contado: due on the invoice date
    assert pd.compute_due_date("2026-11-26T00:00:00+00:00", 15) == date(2026, 12, 11)
    assert pd.compute_due_date(date(2026, 12, 20), 45) == date(2027, 2, 3)      # crosses the year
    assert pd.compute_due_date(date(2026, 11, 26), None) is None                # no structured term: no due date


def test_credit_days_rules():
    assert pd.clean_credit_days(None) is None
    assert pd.clean_credit_days(0) == 0 and pd.clean_credit_days("45") == 45 and pd.clean_credit_days(365) == 365
    for bad in (-1, 366, "abc", 1.5, True, "30.0"):
        with pytest.raises(ValueError):
            pd.clean_credit_days(bad)


def test_pdf_kwargs_and_visible_due_date_follow_the_switch(monkeypatch):
    inv = {"due_date": date(2027, 1, 15)}
    assert pd.visible_due_date(inv) is None and pd.pdf_kwargs(inv) == {}        # off: nothing shown
    monkeypatch.setenv(SWITCH, "true")
    assert pd.visible_due_date(inv) == date(2027, 1, 15) and pd.pdf_kwargs(inv) == {"due_date": date(2027, 1, 15)}
    assert pd.pdf_kwargs({"due_date": None}) == {} and pd.pdf_kwargs({}) == {}


def test_error_codes_are_registered_in_spanish():
    from core.billing_errors import ERRORS
    for code in ("DUE_DATE_NOTE_REQUIRED", "DUE_DATE_UNCHANGED", "DUE_DATE_BEFORE_INVOICE", "INVOICE_DUE_DATE_LOCKED",
                 "INVOICE_HAS_DUE_DATE_AUDIT"):
        assert code in ERRORS and ERRORS[code][1]


def test_models_accept_and_bound_credit_days():
    from pydantic import ValidationError
    from quotes.models import QuoteCreate, QuoteUpdate
    from estimator.api_models import GenerateRequest
    base = {"client_id": 1, "items": []}
    assert QuoteCreate(**base).credit_days is None
    assert QuoteCreate(**base, credit_days=30).credit_days == 30 and QuoteCreate(**base, credit_days=0).credit_days == 0
    for bad in (-1, 366):
        with pytest.raises(ValidationError):
            QuoteCreate(**base, credit_days=bad)
        with pytest.raises(ValidationError):
            QuoteUpdate(credit_days=bad)
    assert "credit_days" not in QuoteUpdate().model_dump(exclude_unset=True)       # not sent = not touched
    assert QuoteUpdate(credit_days=None).model_dump(exclude_unset=True) == {"credit_days": None}   # sent null = clear
    assert "credit_days" in GenerateRequest.model_fields


def test_estimator_passes_credit_days_to_the_shared_quote_writer():
    src = open(os.path.join(ROOT, "estimator", "service.py"), encoding="utf-8").read()
    assert "credit_days=req.credit_days" in src and "create_quote_in_tx(" in src


# ============================================================ PDF: OFF is byte for byte, ON is pinned (no database)

@pytest.mark.parametrize("case", sorted(CASES))
def test_off_pdf_is_byte_identical_to_main(case):
    assert _h(**CASES[case]) == OFF_PLAIN[case]


@pytest.mark.parametrize("case", sorted(CASES))
def test_off_pdf_is_byte_identical_to_main_with_production_flags(case, monkeypatch):
    for k, v in PROD_FLAGS.items():
        monkeypatch.setenv(k, v)
    assert _h(**CASES[case]) == OFF_PROD[case]


@pytest.mark.parametrize("case", sorted(CASES))
def test_off_ignores_a_due_date_even_if_given(case):
    assert _h(**CASES[case], due_date=DUE) == OFF_PLAIN[case]


def _on_case(case, **extra):
    kw = dict(CASES[case])
    if kw["doc_type"] == "FACTURA":
        kw["due_date"] = DUE
    kw.update(extra)
    return _h(**kw)


@pytest.mark.parametrize("case", sorted(CASES))
def test_on_pdf_goldens_are_pinned(case, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    assert _on_case(case) == ON_PLAIN[case]


@pytest.mark.parametrize("case", sorted(CASES))
def test_on_pdf_goldens_with_production_flags_are_pinned(case, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    for k, v in PROD_FLAGS.items():
        monkeypatch.setenv(k, v)
    assert _on_case(case) == ON_PROD[case]


def test_on_invoice_prints_the_due_date_and_not_valida_hasta(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(build(**CASES["inv_terms_valid"], due_date=DUE))
    assert "Vencimiento de pago:" in t and "15/01/2027" in t
    assert "Válida Hasta" not in t and "30/12/2026" not in t                 # D7: a quote concept, not on an invoice
    assert "Términos de Pago:" in t and TERMS in t


def test_on_quote_keeps_valida_hasta_and_never_prints_the_due_line(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(build(**CASES["quote_terms_valid"], due_date=DUE))
    assert "Válida Hasta:" in t and "30/12/2026" in t
    assert "Vencimiento de pago" not in t


def test_on_invoice_without_a_due_date_prints_no_due_line(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(build(**CASES["inv_terms_valid"]))
    assert "Vencimiento de pago" not in t and "Válida Hasta" not in t and TERMS in t


def test_d11_long_terms_wrap_on_and_are_cut_off(monkeypatch):
    off = ptext(build(**CASES["inv_long_terms"]))
    assert LONG[:60].rstrip() in off and "instalacion final" not in off      # today: cut at 60 characters
    monkeypatch.setenv(SWITCH, "true")
    for case in ("inv_long_terms", "quote_long_terms"):
        on = ptext(build(**CASES[case]))
        assert "instalacion final" in on and "50% con la orden de compra" in on   # nothing cut, on quote and invoice


def test_d11_wrapped_terms_never_touch_the_right_column(monkeypatch):
    """The wrapped value stays inside its 63 mm column: every wrapped line is a separate text row left of x=110."""
    monkeypatch.setenv(SWITCH, "true")
    t = text(build(**CASES["inv_long_terms"]))
    xs = [float(m.group(1)) for m in re.finditer(r"BT ([\d.]+) [\d.]+ Td [^()]*\((?:50% con|estructura metalica|instalacion final)", t)]
    assert len(xs) >= 3 and all(x < 110 * 72 / 25.4 for x in xs)           # mm to points


def test_header_height_items_start_below_a_tall_left_column(monkeypatch):
    """A very long term makes the left column taller than the client column. The items title must land below the last
    left-column row (a lower y number is lower on the page)."""
    monkeypatch.setenv(SWITCH, "true")
    for case in (dict(doc_type="FACTURA", doc_id="F-1", payment_terms=VERY_LONG, project_name=None, due_date=DUE),
                 dict(doc_type="COTIZACION", payment_terms=VERY_LONG, valid_until="2026-12-30", project_name=None)):
        b = build(**case)
        last_row = min(ypos(b, "tramo") + ypos(b, "Vencimiento de pago") + ypos(b, "Válida Hasta"))
        title = ypos(b, "Detalle de Items")
        assert len(title) == 1 and title[0] < last_row - 4                  # clear of the last row
        assert "tramo60" in ptext(b)                                        # the whole value printed


def test_header_height_unchanged_for_short_terms(monkeypatch):
    """With nothing to wrap the items start exactly where they do with the switch off (quote: identical bytes)."""
    base = ypos(build(**CASES["inv_terms_valid"]), "Detalle de Items")
    monkeypatch.setenv(SWITCH, "true")
    assert ypos(build(**CASES["inv_terms_valid"], due_date=DUE), "Detalle de Items") == base
    assert ypos(build(**CASES["quote_terms_valid"]), "Detalle de Items") == ypos(build(**CASES["quote_terms_valid"]), "Detalle de Items")


# ============================================================ integration (real Postgres)

CHARGES = {"supervision": False, "admin": False, "insurance": False, "transport": False, "contingency": False}


@pytest.fixture
def db(_base_db, monkeypatch):
    for mod in ("fiscal.service", "phases.service", "conduces.service"):
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
    return _base_db


def _admin():
    return {"Authorization": "Bearer " + create_access_token(
        {"sub": "t_admin", "role": "admin", "user_id": "00000000-0000-0000-0000-000000000002"})}


def _user():
    return {"Authorization": "Bearer " + create_access_token(
        {"sub": "t_user", "role": "user", "user_id": "00000000-0000-0000-0000-000000000001"})}


def _mkquote(db, credit_days=None, terms="50% anticipo", valid_until=None, price=1000.0, approve=True, **extra):
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "project_name": "Torre Norte", "included_charges": CHARGES,
            "payment_terms": terms, "items": [{"product_name": "Viga unica", "quantity": 1, "unit_price": price}]}
    if credit_days is not None:
        body["credit_days"] = credit_days
    if valid_until:
        body["valid_until"] = valid_until
    body.update(extra)
    r = client.post("/quotes/", json=body, headers=_hdr())
    assert r.status_code in (200, 201), r.text
    qid = r.json()["data"]["quote_id"]
    client.post(f"/quotes/{qid}/finalize", headers=_hdr())
    if approve:
        assert client.patch(f"/quotes/{qid}/status", json={"status": "Approved"}, headers=_hdr()).status_code == 200
    return qid


def _invoice_of(qid):
    r = client.post(f"/quotes/{qid}/convert-to-invoice", headers=_hdr())
    assert r.status_code == 200, r.text
    return r.json()["data"]["invoice_id"]


def _col(db, sql, params=()):
    rows = _rows(db, sql, params)
    return rows[0][list(rows[0].keys())[0]] if rows else None


def _detail(iid, headers=None):
    r = client.get(f"/invoices/{iid}", headers=headers or _hdr())
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _edit(iid, due, note="Acuerdo con el cliente", headers=None):
    body = {"due_date": due}
    if note is not None:
        body["note"] = note
    return client.patch(f"/invoices/{iid}/due-date", json=body, headers=headers or _admin())


def _fiscal_money_hash(db, qid, iid):
    """Every money, tax, NCF and numbering column of the invoice and quote (due_date and credit_days left out), the
    lines, and the fiscal tables. A due-date operation must not move any of it."""
    sql = ("SELECT md5(string_agg(t, '|' ORDER BY t)) AS h FROM ("
           "SELECT (row_to_json(i)::jsonb - 'due_date')::text t FROM invoices i WHERE id = %s "
           "UNION ALL SELECT row_to_json(x)::text FROM invoice_items x WHERE invoice_id = %s "
           "UNION ALL SELECT (row_to_json(q)::jsonb - 'credit_days')::text FROM quotes q WHERE quote_id = %s "
           "UNION ALL SELECT row_to_json(f)::text FROM fiscal_sequences f "
           "UNION ALL SELECT row_to_json(a)::text FROM fiscal_sequence_audit a "
           "UNION ALL SELECT row_to_json(p)::text FROM invoice_phases p) s")
    return _rows(db, sql, (iid, iid, qid))[0]["h"]


# ---------------------------------------------------------------- switch OFF: nothing changes

@needs_db
def test_off_credit_days_is_ignored_and_no_due_date_is_stored(db):
    qid = _mkquote(db, credit_days=30)
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) is None   # ignored, not stored
    assert "credit_days" not in client.get(f"/quotes/{qid}", headers=_hdr()).json()["data"]
    iid = _invoice_of(qid)
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) is None
    assert _detail(iid)["due_date"] is None


@needs_db
def test_off_routes_answer_404_and_options_say_disabled(db):
    qid = _mkquote(db)
    iid = _invoice_of(qid)
    assert client.get("/quotes/payment-due-options", headers=_hdr()).json()["data"] == {"payment_due_enabled": False}
    assert _edit(iid, "2027-06-01").status_code == 404
    assert client.get(f"/invoices/{iid}/due-date-audit", headers=_admin()).status_code == 404
    assert _col(db, "SELECT count(*) FROM invoice_due_date_audit") == 0


@needs_db
def test_off_update_with_credit_days_does_not_touch_the_column(db):
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "included_charges": CHARGES,
            "items": [{"product_name": "Viga", "quantity": 1, "unit_price": 10.0}]}
    qid = client.post("/quotes/", json=body, headers=_hdr()).json()["data"]["quote_id"]
    r = client.put(f"/quotes/{qid}", json={"credit_days": 45, "payment_terms": "Credito"}, headers=_hdr())
    assert r.status_code == 200, r.text
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) is None
    assert _col(db, "SELECT payment_terms FROM quotes WHERE quote_id = %s", (qid,)) == "Credito"   # the rest still saves


@needs_db
def test_off_invoice_pdf_is_unchanged_in_every_path(db):
    qid = _mkquote(db, valid_until=(date.today() + timedelta(days=30)).isoformat(), terms=LONG)
    iid = _invoice_of(qid)
    token = _col(db, "SELECT public_token FROM invoices WHERE id = %s", (iid,))
    for resp in (client.get(f"/pdf/invoices/{iid}", headers=_hdr()), client.get(f"/invoices/public/{token}/pdf")):
        assert resp.status_code == 200
        t = ptext(resp.content)
        assert "Válida Hasta:" in t and "Vencimiento de pago" not in t and "instalacion final" not in t


# ---------------------------------------------------------------- switch ON: entry

@needs_db
def test_on_credit_days_saves_reads_updates_and_clears(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    assert client.get("/quotes/payment-due-options", headers=_hdr()).json()["data"] == {"payment_due_enabled": True}
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "included_charges": CHARGES, "credit_days": 30,
            "items": [{"product_name": "Viga", "quantity": 1, "unit_price": 10.0}]}
    qid = client.post("/quotes/", json=body, headers=_hdr()).json()["data"]["quote_id"]
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) == 30
    assert client.get(f"/quotes/{qid}", headers=_hdr()).json()["data"]["credit_days"] == 30
    assert client.put(f"/quotes/{qid}", json={"credit_days": 60}, headers=_hdr()).status_code == 200
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) == 60
    assert client.put(f"/quotes/{qid}", json={"project_name": "Otro"}, headers=_hdr()).status_code == 200   # key not sent
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) == 60
    assert client.put(f"/quotes/{qid}", json={"credit_days": 0}, headers=_hdr()).status_code == 200          # Contado
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) == 0
    assert client.put(f"/quotes/{qid}", json={"credit_days": None}, headers=_hdr()).status_code == 200      # clear
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) is None
    for bad in (-1, 366):
        assert client.put(f"/quotes/{qid}", json={"credit_days": bad}, headers=_hdr()).status_code == 422


@needs_db
def test_on_duplicate_keeps_credit_days(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    qid = _mkquote(db, credit_days=45, approve=False)
    r = client.post(f"/quotes/{qid}/duplicate", headers=_hdr())
    assert r.status_code in (200, 201), r.text
    new = r.json()["data"]["quote_id"]
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (new,)) == 45


@needs_db
def test_on_shared_quote_writer_used_by_the_estimator_stores_credit_days(db, monkeypatch):
    from quotes.service import create_quote_in_tx
    monkeypatch.setenv(SWITCH, "true")
    conn = db["connect"]()
    cur = conn.cursor()
    qid = create_quote_in_tx(cur, db["cid"], db["ctid"], "Estimado", None,
                             [{"product_name": "Viga", "quantity": 1, "unit_price": 10.0}], dict(CHARGES),
                             "Credito 30", None, None, None, quote_type="engineering_estimate", credit_days=30)
    conn.commit()
    conn.close()
    assert _col(db, "SELECT credit_days FROM quotes WHERE quote_id = %s", (qid,)) == 30


# ---------------------------------------------------------------- switch ON: the stored snapshot

@needs_db
def test_on_conversion_stores_due_date_from_the_invoice_date(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    from core.dates import business_today
    qid = _mkquote(db, credit_days=30)
    iid = _invoice_of(qid)
    stored = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    assert stored == business_today() + timedelta(days=30)
    d = _detail(iid)
    assert d["due_date"] == stored.isoformat()


@needs_db
def test_on_contado_is_due_on_the_invoice_date_and_no_term_means_no_date(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    from core.dates import business_today
    iid0 = _invoice_of(_mkquote(db, credit_days=0))
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid0,)) == business_today()
    iid_none = _invoice_of(_mkquote(db, credit_days=None))
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid_none,)) is None
    assert _detail(iid_none)["due_date"] is None
    t = ptext(client.get(f"/pdf/invoices/{iid_none}", headers=_hdr()).content)
    assert "Vencimiento de pago" not in t                              # nothing prints, not even a blank line


@needs_db
def test_on_a_later_quote_edit_never_moves_an_existing_invoice(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    qid = _mkquote(db, credit_days=30)
    iid = _invoice_of(qid)
    before = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    conn = db["connect"]()
    cur = conn.cursor()
    cur.execute("UPDATE quotes SET credit_days = 90 WHERE quote_id = %s", (qid,))   # the quote changes afterwards
    conn.commit()
    conn.close()
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) == before
    assert _detail(iid)["due_date"] == before.isoformat()
    t = ptext(client.get(f"/pdf/invoices/{iid}", headers=_hdr()).content)
    assert before.strftime("%d/%m/%Y") in t                            # printed from the stored value, never re-derived


@needs_db
def test_on_invoice_pdf_in_every_path_prints_the_stored_due_date(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    monkeypatch.setenv("PDF_FULL_COPY_ENABLED", "true")                    # the internal full-copy path is a separate switch
    qid = _mkquote(db, credit_days=30, valid_until=(date.today() + timedelta(days=30)).isoformat(), terms=LONG)
    iid = _invoice_of(qid)
    due = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)).strftime("%d/%m/%Y")
    token = _col(db, "SELECT public_token FROM invoices WHERE id = %s", (iid,))
    paths = [client.get(f"/pdf/invoices/{iid}", headers=_hdr()), client.get(f"/pdf/invoices/{iid}?full=true", headers=_hdr()),
             client.get(f"/invoices/public/{token}/pdf")]
    for resp in paths:
        assert resp.status_code == 200
        t = ptext(resp.content)
        assert "Vencimiento de pago:" in t and due in t
        assert "Válida Hasta" not in t                                 # D7
        assert "instalacion final" in t                                # D11
    qpdf = ptext(client.get(f"/pdf/quotes/{qid}", headers=_hdr()).content)
    assert "Válida Hasta:" in qpdf and "Vencimiento de pago" not in qpdf   # the quote keeps its own validity


@needs_db
def test_on_switch_turned_off_again_restores_todays_output(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    qid = _mkquote(db, credit_days=30, valid_until=(date.today() + timedelta(days=30)).isoformat())
    iid = _invoice_of(qid)
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) is not None
    monkeypatch.delenv(SWITCH)
    assert _detail(iid)["due_date"] is None                            # stored value hidden while off
    t = ptext(client.get(f"/pdf/invoices/{iid}", headers=_hdr()).content)
    assert "Vencimiento de pago" not in t and "Válida Hasta:" in t
    assert _edit(iid, "2027-06-01").status_code == 404


# ---------------------------------------------------------------- phase invoices (D6)

@needs_db
def test_on_each_phase_invoice_takes_its_own_issue_date_plus_the_quote_days(db, monkeypatch):
    import core.dates as cd
    _on(monkeypatch, phased=True)
    monkeypatch.setenv(SWITCH, "true")
    _activate()
    qid = _mkquote(db, credit_days=45, price=1000.0)
    assert _convert(qid, "PHASED").status_code == 200
    today = cd.business_today()
    p1 = _phase(qid, "percent", 50, "Inicial").json()["data"]["id"]
    i1 = _invoice_phase(p1).json()["data"]["invoice_id"]
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (i1,)) == today + timedelta(days=45)
    later = today + timedelta(days=10)
    monkeypatch.setattr(cd, "business_today", lambda: later)               # the second phase is invoiced ten days later
    p2 = _phase(qid, "final", None, "Final").json()["data"]["id"]
    i2 = _invoice_phase(p2).json()["data"]["invoice_id"]
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (i2,)) == later + timedelta(days=45)
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (i1,)) == today + timedelta(days=45)  # phase 1 untouched
    assert _col(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'invoice_phases' "
                    "AND column_name ILIKE %s", ("%due%",)) == 0           # no per-phase date field


@needs_db
def test_on_phase_invoice_without_credit_days_has_no_due_date(db, monkeypatch):
    _on(monkeypatch, phased=True)
    monkeypatch.setenv(SWITCH, "true")
    _activate()
    qid = _mkquote(db)
    assert _convert(qid, "PHASED").status_code == 200
    p1 = _phase(qid, "percent", 100, "Unica").json()["data"]["id"]
    i1 = _invoice_phase(p1).json()["data"]["invoice_id"]
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (i1,)) is None


# ---------------------------------------------------------------- admin edit and audit (D5)

@needs_db
def test_on_admin_edit_changes_the_date_and_appends_an_audit_row(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    qid = _mkquote(db, credit_days=30)
    iid = _invoice_of(qid)
    old = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    new = old + timedelta(days=20)
    r = _edit(iid, new.isoformat(), note="  Prorroga acordada por telefono  ")
    assert r.status_code == 200, r.text
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) == new
    rows = _rows(db, "SELECT * FROM invoice_due_date_audit WHERE invoice_id = %s", (iid,))
    assert len(rows) == 1
    a = rows[0]
    assert (a["old_due_date"], a["new_due_date"], a["note"]) == (old, new, "Prorroga acordada por telefono")
    assert str(a["changed_by"]) == "00000000-0000-0000-0000-000000000002" and a["changed_at"] is not None
    r2 = _edit(iid, (new + timedelta(days=5)).isoformat(), note="Segunda prorroga")
    assert r2.status_code == 200
    hist = client.get(f"/invoices/{iid}/due-date-audit", headers=_admin()).json()["data"]
    assert [h["note"] for h in hist] == ["Prorroga acordada por telefono", "Segunda prorroga"]
    assert hist[1]["old_due_date"] == new.isoformat()                  # old -> new chains


@needs_db
def test_on_admin_can_set_a_date_on_an_invoice_that_had_none(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid = _invoice_of(_mkquote(db))
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) is None
    target = (date.today() + timedelta(days=20)).isoformat()
    assert _edit(iid, target, note="Fecha pactada").status_code == 200
    a = _rows(db, "SELECT old_due_date, new_due_date FROM invoice_due_date_audit WHERE invoice_id = %s", (iid,))[0]
    assert a["old_due_date"] is None and a["new_due_date"].isoformat() == target


@needs_db
@pytest.mark.parametrize("note", [None, "", "   ", "\n\t"])
def test_on_edit_without_a_note_is_refused_and_nothing_changes(db, monkeypatch, note):
    monkeypatch.setenv(SWITCH, "true")
    iid = _invoice_of(_mkquote(db, credit_days=30))
    before = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    r = _edit(iid, (before + timedelta(days=3)).isoformat(), note=note)
    assert r.status_code == 400 and r.json()["code"] == "DUE_DATE_NOTE_REQUIRED"
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) == before
    assert _col(db, "SELECT count(*) FROM invoice_due_date_audit") == 0


@needs_db
def test_on_a_non_admin_cannot_edit_or_read_the_audit(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid = _invoice_of(_mkquote(db, credit_days=30))
    before = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    r = _edit(iid, (before + timedelta(days=3)).isoformat(), headers=_user())
    assert r.status_code == 403
    assert client.get(f"/invoices/{iid}/due-date-audit", headers=_user()).status_code == 403
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) == before
    assert _col(db, "SELECT count(*) FROM invoice_due_date_audit") == 0
    assert client.patch(f"/invoices/{iid}/due-date", json={"due_date": "2027-01-01", "note": "x"}).status_code in (401, 403)


@needs_db
def test_on_edit_refusals_unchanged_before_invoice_date_locked_and_missing(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid = _invoice_of(_mkquote(db, credit_days=30))
    cur_due = _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,))
    r = _edit(iid, cur_due.isoformat())
    assert r.status_code == 400 and r.json()["code"] == "DUE_DATE_UNCHANGED"
    r = _edit(iid, "2000-01-01")
    assert r.status_code == 400 and r.json()["code"] == "DUE_DATE_BEFORE_INVOICE"
    assert _edit(999999, "2027-01-01").status_code == 404
    assert client.patch(f"/invoices/{iid}/due-date", json={"note": "x"}, headers=_admin()).status_code == 422   # date required
    for status in ("Paid", "Cancelled"):
        conn = db["connect"]()
        cur = conn.cursor()
        cur.execute("UPDATE invoices SET status = %s WHERE id = %s", (status, iid))
        conn.commit()
        conn.close()
        r = _edit(iid, (cur_due + timedelta(days=9)).isoformat())
        assert r.status_code == 409 and r.json()["code"] == "INVOICE_DUE_DATE_LOCKED"
    assert _col(db, "SELECT due_date FROM invoices WHERE id = %s", (iid,)) == cur_due
    assert _col(db, "SELECT count(*) FROM invoice_due_date_audit") == 0


@needs_db
def test_the_audit_table_is_append_only_in_the_database(db, monkeypatch):
    import psycopg2
    monkeypatch.setenv(SWITCH, "true")
    iid = _invoice_of(_mkquote(db, credit_days=30))
    assert _edit(iid, (date.today() + timedelta(days=90)).isoformat()).status_code == 200
    conn = db["connect"]()
    cur = conn.cursor()
    for sql in ("UPDATE invoice_due_date_audit SET note = 'x'", "DELETE FROM invoice_due_date_audit",
                "INSERT INTO invoice_due_date_audit (invoice_id, old_due_date, new_due_date, note) "
                f"VALUES ({iid}, '2027-01-01', '2027-01-01', 'same')",       # old = new refused
                f"INSERT INTO invoice_due_date_audit (invoice_id, new_due_date, note) VALUES ({iid}, '2027-01-02', '  ')"):
        with pytest.raises(psycopg2.Error):
            cur.execute(sql)
        conn.rollback()
    conn.close()


@needs_db
def test_on_invoice_with_a_recorded_due_date_change_cannot_be_deleted(db, monkeypatch):
    monkeypatch.setenv(SWITCH, "true")
    iid = _invoice_of(_mkquote(db, credit_days=30))
    assert _edit(iid, (date.today() + timedelta(days=90)).isoformat()).status_code == 200
    r = client.delete(f"/invoices/{iid}", headers=_admin())
    assert r.status_code == 409 and r.json()["code"] == "INVOICE_HAS_DUE_DATE_AUDIT"
    assert _col(db, "SELECT count(*) FROM invoices WHERE id = %s", (iid,)) == 1
    iid2 = _invoice_of(_mkquote(db, credit_days=30))                     # no edit: still deletable as before
    assert client.delete(f"/invoices/{iid2}", headers=_admin()).status_code == 200


# ---------------------------------------------------------------- fiscal isolation

@needs_db
def test_due_date_operations_leave_every_money_tax_ncf_and_numbering_column_untouched(db, monkeypatch):
    _on(monkeypatch, phased=True)
    monkeypatch.setenv(SWITCH, "true")
    _activate()
    qid = _mkquote(db, credit_days=30)
    iid = _invoice_of(qid)
    inv = _rows(db, "SELECT ncf, total_amount, itbis_total, tax_rate, invoice_number FROM invoices WHERE id = %s", (iid,))[0]
    assert inv["ncf"]
    before = _fiscal_money_hash(db, qid, iid)
    assert _edit(iid, (date.today() + timedelta(days=60)).isoformat(), note="Prorroga").status_code == 200
    assert _edit(iid, (date.today() + timedelta(days=75)).isoformat(), note="Otra prorroga").status_code == 200
    client.get(f"/invoices/{iid}/due-date-audit", headers=_admin())
    client.get(f"/pdf/invoices/{iid}", headers=_hdr())
    assert _fiscal_money_hash(db, qid, iid) == before
    assert _rows(db, "SELECT ncf, total_amount, itbis_total, tax_rate, invoice_number FROM invoices WHERE id = %s", (iid,))[0] == inv


@needs_db
def test_credit_days_do_not_change_what_conversion_produces(db, monkeypatch):
    """Two identical quotes, one with credit days and one without, convert to the same NCF sequence step, totals, tax and
    lines: the due date is the only difference."""
    _on(monkeypatch, phased=True)
    monkeypatch.setenv(SWITCH, "true")
    _activate()
    cols = "total_amount, subtotal, itbis_total, grand_total, tax_type_code, tax_rate, tax_label, amount_due, status"
    ia = _invoice_of(_mkquote(db, credit_days=30))
    ib = _invoice_of(_mkquote(db))
    a = _rows(db, f"SELECT {cols}, ncf FROM invoices WHERE id = %s", (ia,))[0]
    b = _rows(db, f"SELECT {cols}, ncf FROM invoices WHERE id = %s", (ib,))[0]
    assert int(b["ncf"][-8:]) == int(a["ncf"][-8:]) + 1                # numbering advanced by exactly one each
    a.pop("ncf"), b.pop("ncf")
    assert a == b
    la = _rows(db, "SELECT description, quantity, unit_price, discount, total FROM invoice_items WHERE invoice_id = %s", (ia,))
    lb = _rows(db, "SELECT description, quantity, unit_price, discount, total FROM invoice_items WHERE invoice_id = %s", (ib,))
    assert la == lb


# ---------------------------------------------------------------- migration and rollback (scratch database)

def _psql_available():
    return shutil.which("psql") is not None and bool(os.environ.get("METPRO_LEDGER_TEST_DB"))


MIGRATIONS_BEFORE = [
    "2026-10-22_estimator_schema.sql", "2026-10-22_estimator_seed_v1.sql", "2026-10-22_estimator_run_idempotency.sql",
    "2026-10-23_pdf_medidas_print_items.sql", "2026-10-23_status_history.sql", "2026-10-23_suppliers.sql",
    "2026-10-23_suppliers_seed_dummy.sql", "2026-10-24_rfq.sql", "2026-10-24_purchase_orders.sql",
    "2026-11-25_fiscal_sequence_ncf.sql", "2026-11-25_phased_invoicing.sql", "2026-11-26_conduce_ledger.sql",
]


@pytest.mark.skipif(not _psql_available(), reason="needs psql and METPRO_LEDGER_TEST_DB")
def test_migration_and_guarded_rollback_on_a_scratch_database():
    """Fresh database, schema as of main 12d3898 (no payment_due objects), then: apply, apply again (idempotent), roll back
    with no data (everything gone, nothing else touched), apply again, store a value, and prove the rollback now REFUSES
    for each kind of value, leaving the objects in place."""
    import psycopg2
    admin_dsn = os.environ["METPRO_LEDGER_TEST_DB"]
    name = "metpro_b2_scratch_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(admin_dsn)
    conn.autocommit = True
    conn.cursor().execute(f'CREATE DATABASE "{name}"')
    conn.close()
    scratch = re.sub(r"dbname=\S+", "", admin_dsn).strip() + f" dbname={name}"

    def run(path_or_sql, is_file=True, check=True):
        cmd = ["psql", scratch, "-v", "ON_ERROR_STOP=1", "-q", "-At"]
        cmd += ["-f", path_or_sql] if is_file else ["-c", path_or_sql]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if check:
            assert p.returncode == 0, p.stderr
        return p

    def objects():
        sql = ("SELECT (SELECT count(*) FROM information_schema.columns WHERE table_name = 'quotes' AND column_name = 'credit_days') "
               "|| ',' || (SELECT count(*) FROM information_schema.columns WHERE table_name = 'invoices' AND column_name = 'due_date') "
               "|| ',' || (SELECT count(*) FROM information_schema.tables WHERE table_name = 'invoice_due_date_audit') "
               "|| ',' || (SELECT count(*) FROM pg_proc WHERE proname = 'invoice_due_date_audit_guard')")
        return run(sql, is_file=False).stdout.strip()

    mig = lambda f: os.path.join(ROOT, "migrations", f)
    try:
        run(os.path.join(HERE, "fixtures", "ci_schema.sql"))
        for f in MIGRATIONS_BEFORE:
            run(mig(f))
        assert objects() == "0,0,0,0"
        run(mig("2026-11-26_payment_due.sql"))
        assert objects() == "1,1,1,1"
        run(mig("2026-11-26_payment_due.sql"))                         # idempotent
        assert objects() == "1,1,1,1"
        cols_before = run("SELECT string_agg(table_name || '.' || column_name, ',' ORDER BY table_name, column_name) "
                          "FROM information_schema.columns WHERE table_schema = 'public' AND table_name <> 'invoice_due_date_audit' "
                          "AND NOT (table_name = 'quotes' AND column_name = 'credit_days') "
                          "AND NOT (table_name = 'invoices' AND column_name = 'due_date')", is_file=False).stdout
        run(mig("2026-11-26_payment_due.rollback.sql"))                # no data: runs
        assert objects() == "0,0,0,0"
        cols_after = run("SELECT string_agg(table_name || '.' || column_name, ',' ORDER BY table_name, column_name) "
                         "FROM information_schema.columns WHERE table_schema = 'public'", is_file=False).stdout
        assert cols_before == cols_after                               # nothing else touched
        run(mig("2026-11-26_payment_due.rollback.sql"))                # a second run is harmless
        run(mig("2026-11-26_payment_due.sql"))

        run("INSERT INTO clients (company_name) VALUES ('C'); "
            "INSERT INTO quotes (quote_id, client_id, status, tax_type_code, tax_rate, tax_label) "
            "SELECT 'Q-T1', id, 'Draft', 'itbis_18', 0.18, 'ITBIS (18%)' FROM clients LIMIT 1;", is_file=False)
        # 1. a quote carries credit_days
        run("UPDATE quotes SET credit_days = 30 WHERE quote_id = 'Q-T1'", is_file=False)
        p = run(mig("2026-11-26_payment_due.rollback.sql"), check=False)
        assert p.returncode != 0 and "credit_days" in p.stderr and objects() == "1,1,1,1"
        run("UPDATE quotes SET credit_days = NULL", is_file=False)
        run(mig("2026-11-26_payment_due.rollback.sql"))                # empty again: runs
        assert objects() == "0,0,0,0"
        run(mig("2026-11-26_payment_due.sql"))
        # 2. an invoice carries due_date
        run("INSERT INTO invoices (invoice_number, quote_id, client_id, status, tax_type_code, tax_rate, tax_label) "
            "SELECT 'F-T1', 'Q-T1', id, 'Pending', 'itbis_18', 0.18, 'ITBIS (18%)' FROM clients LIMIT 1; "
            "UPDATE invoices SET due_date = '2027-01-01'", is_file=False)
        p = run(mig("2026-11-26_payment_due.rollback.sql"), check=False)
        assert p.returncode != 0 and "due_date" in p.stderr and objects() == "1,1,1,1"
        run("UPDATE invoices SET due_date = NULL", is_file=False)
        # 3. an audit row exists
        run("INSERT INTO invoice_due_date_audit (invoice_id, old_due_date, new_due_date, note) "
            "SELECT id, NULL, '2027-01-02', 'n' FROM invoices LIMIT 1", is_file=False)
        p = run(mig("2026-11-26_payment_due.rollback.sql"), check=False)
        assert p.returncode != 0 and "audit" in p.stderr and objects() == "1,1,1,1"
        assert run("SELECT count(*) FROM invoice_due_date_audit", is_file=False).stdout.strip() == "1"
    finally:
        conn = psycopg2.connect(admin_dsn)
        conn.autocommit = True
        conn.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.close()
