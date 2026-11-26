"""Phase invoice layout (Option 2) + NCF on the invoice detail page. Plan: claude/phase-invoice-layout-plan-2026-11-26.md.

PHASE_INVOICE_LAYOUT_ENABLED defaults OFF: a phase invoice prints exactly as before. ON: the source quote's lines and
summary print, then a "Facturación por fases" block with the STORED phase figures. Nothing here changes tax, NCF,
phase math or totals. Real Postgres (METPRO_LEDGER_TEST_DB) for the integration tests.
"""
import hashlib
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import (  # noqa: E402  (shared builders, client and DB fixture)
    client, db as _base_db, needs_db, text, normalized, _rows, _no_db_pages, build,
)
from test_ncf_phases import _hdr, _on, _activate, _phase, _invoice_phase, _convert  # noqa: E402
import pdf.utils.layout_utils as layout  # noqa: E402


def ptext(b):
    """PDF content text with the stream's escaped parentheses restored."""
    return text(b).replace("\\(", "(").replace("\\)", ")")

from core import pdf_options  # noqa: E402

SWITCH = "PHASE_INVOICE_LAYOUT_ENABLED"
CHARGES = {"supervision": True, "admin": True, "insurance": True, "transport": True, "contingency": True}
ITEMS = [
    {"product_name": "Viga IPE 200 x 6 m", "quantity": 10, "unit_price": 450.0, "category": "ESTRUCTURA", "unit_of_measure": "und"},
    {"product_name": "Columna HEB 160 x 6 m", "quantity": 5, "unit_price": 500.0, "category": "ESTRUCTURA", "unit_of_measure": "und"},
    {"product_name": "Pintura anticorrosiva", "quantity": 100, "unit_price": 15.0, "category": "ACABADOS", "unit_of_measure": "m2"},
    {"product_name": "Instalacion y montaje", "quantity": 1, "unit_price": 1500.0, "category": "ACABADOS", "unit_of_measure": "gl"},
]
# Quote: items 10,000.00 + charges 21% (2,100.00) = Subtotal General 12,100.00; ITBIS 18% 2,178.00; total 14,278.00.
# 50% phase: total 7,139.00 = ITBIS 1,089.00 + base 6,050.00.


@pytest.fixture
def db(_base_db, monkeypatch):
    for mod in ("fiscal.service", "phases.service", "pdf.phase_document"):
        monkeypatch.setattr(importlib.import_module(mod), "get_db_connection", _base_db["connect"], raising=False)
    conn = _base_db["connect"]()
    cur = conn.cursor()
    cur.execute("TRUNCATE fiscal_sequence_audit, fiscal_sequences, invoice_phases RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
    for name in ("FISCAL_NCF_ENABLED", "PHASED_INVOICING_ENABLED", SWITCH):
        monkeypatch.delenv(name, raising=False)
    return _base_db


def _rich_quote(db, items=None, print_items=True, tax_code=None, charges=None):
    body = {"client_id": db["cid"], "contact_id": db["ctid"], "project_name": "Nave de prueba",
            "included_charges": CHARGES if charges is None else charges, "items": [dict(i) for i in (items or ITEMS)]}
    if tax_code:
        body["tax_type_code"] = tax_code
    if print_items is False:
        body["print_items"] = False
    r = client.post("/quotes/", json=body, headers=_hdr())
    assert r.status_code in (200, 201), r.text
    qid = r.json()["data"]["quote_id"]
    client.post(f"/quotes/{qid}/finalize", headers=_hdr())
    assert client.patch(f"/quotes/{qid}/status", json={"status": "Approved"}, headers=_hdr()).status_code == 200
    assert _convert(qid, "PHASED").status_code == 200
    return qid


def _setup(db, monkeypatch, kind="percent", value=50, label="Pago inicial", **quote_kw):
    _on(monkeypatch, phased=True)
    _activate()
    qid = _rich_quote(db, **quote_kw)
    pid = _phase(qid, kind, value, label).json()["data"]["id"]
    iid = _invoice_phase(pid).json()["data"]["invoice_id"]
    return qid, pid, iid


def _download(iid, path="/pdf/invoices/{}"):
    r = client.get(path.format(iid), headers=_hdr())
    assert r.status_code == 200, r.text
    return r.content


def _pages(pdf_bytes):
    import re
    import zlib
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S):
        try:
            s = zlib.decompress(m.group(1)).decode("latin-1")
        except Exception:
            continue
        if "Tj" in s or "TJ" in s:
            out.append(s)
    return out


# ============================================================ switch and OFF identity (no database)

def test_switch_is_off_by_default_and_parsed_like_the_others(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert pdf_options.phase_layout_enabled() is False
    for v in ("true", "1", "yes", "on", "True", " ON "):
        monkeypatch.setenv(SWITCH, v)
        assert pdf_options.phase_layout_enabled() is True
    for v in ("", "false", "0", "off", "no"):
        monkeypatch.setenv(SWITCH, v)
        assert pdf_options.phase_layout_enabled() is False


def test_builder_without_phase_doc_is_byte_identical():
    assert normalized(build(phase_doc=None)) == normalized(build())
    t = ptext(build())
    assert "Facturaci" not in t and "TOTAL COTIZACI" not in t and "TOTAL GENERAL" in t


def test_loader_never_raises_and_falls_back_to_the_old_layout(monkeypatch):
    import pdf.phase_document as pd

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(pd, "get_db_connection", boom)
    assert pd.phase_document_override({"id": 1, "quote_id": "q", "phase_id": 1}) == {}
    monkeypatch.setenv(SWITCH, "true")
    assert pd.phase_layout_override({"id": 1, "quote_id": "q", "phase_id": None}) == {}   # not a phase invoice
    monkeypatch.setenv(SWITCH, "false")
    assert pd.phase_layout_override({"id": 1, "quote_id": "q", "phase_id": 5}) == {}      # switch off


def test_quote_lines_cannot_change_after_approval():
    """The plan reads quote_items live because approved quotes are not editable. Pin that rule."""
    import quotes.service as qsvc
    assert qsvc.ALLOW_EDIT_NON_DRAFT_QUOTES is False


GOLDEN_PHASE_DOC = "8f65bf6703c9aeffb92794b77b7b65265921935e23ba4ad10cb13d135009fa09"


def _golden_args(**doc_over):
    items = [dict(i) for i in ITEMS]
    doc = {"quote_label": "Q-10050", "number": 1, "label": "Pago inicial / depósito", "input_type": "percent",
           "input_value": 50.0, "base": 6050.0, "itbis": 1089.0, "total": 7139.0, "quote_total": 14278.0,
           "cumulative": 7139.0, "pending": 7139.0, "tax_word": "ITBIS"}
    doc.update(doc_over)
    return dict(
        doc_type="FACTURA", doc_id="F-10050", doc_date="26/11/2026", client={"company_name": "Cliente"},
        project_name="Nave de prueba", notes=None, items=items, charges=dict(CHARGES, supervision_percentage=10.0),
        items_total=10000.0, total_discounts=0, items_after_discount=10000.0, supervision=1000.0, supervision_pct=10,
        admin=400.0, admin_pct=4, insurance=100.0, insurance_pct=1, transport=300.0, transport_pct=3,
        contingency=300.0, contingency_pct=3, subtotal_general=12100.0, itbis=2178.0, tax_label="ITBIS (18%)",
        grand_total=7139.0, ncf="B0100000331", phase_doc=doc,
    )


def _golden(**doc_over):
    return layout.build_quote_invoice_pdf(**_golden_args(**doc_over)).getvalue()


def test_phase_document_golden(monkeypatch):
    for n in ("PDF_MEDIDAS_ENABLED", "PDF_PROJECT_DESC_ENABLED", "PDF_DESC_WRAP_ENABLED"):
        monkeypatch.delenv(n, raising=False)
    assert hashlib.sha256(normalized(_golden())).hexdigest() == GOLDEN_PHASE_DOC


def test_block_text_for_each_basis_and_tax_type():
    t = ptext(_golden())
    assert "Fase 1 - Pago inicial" in t and "50% del total de la cotizaci" in t
    assert "TOTAL COTIZACI" in t and "TOTAL GENERAL" not in t and "TOTAL DE ESTA FACTURA" in t
    assert "ITBIS de la fase (parte proporcional)" in t
    fixed = ptext(_golden(input_type="fixed", input_value=5000.0))
    assert "Monto fijo acordado" in fixed and "del total de la cotizaci" not in fixed and "%" not in fixed.split("Monto fijo")[1][:60]
    final = ptext(_golden(input_type="final", input_value=None))
    assert "Saldo final" in final
    other = ptext(_golden(tax_word="Impuesto"))
    assert "Impuesto de la fase (parte proporcional)" in other and "ITBIS de la fase" not in other
    assert " de 2" not in ptext(_golden(number=2)).split("Fase 2")[1][:40]   # never "Fase N de M"


def test_phase_tax_label_carries_no_rate():
    """The phase tax is a stored share. Its line must not show a rate that invites rate x printed lines."""
    t = ptext(_golden())
    line = t.split("ITBIS de la fase")[1][:60]
    assert "%" not in line.split(")Tj")[0]
    assert "no se calcula sobre los renglones impresos" in t.lower()


def test_block_stays_together_on_a_long_quote():
    many = [dict(ITEMS[2], product_name=f"Linea {i}") for i in range(34)]
    pdf = layout.build_quote_invoice_pdf(**dict(_golden_args(), items=many)).getvalue()
    pages = _pages(pdf)
    holder = [p for p in pages if "TOTAL DE ESTA FACTURA" in p]
    assert len(holder) == 1
    for needle in ("Facturaci", "Base de la fase", "Facturado acumulado", "Pendiente de facturar"):
        assert needle in holder[0], needle


# ============================================================ integration

@needs_db
def test_off_prints_the_old_phase_layout(db, monkeypatch):
    _, _, iid = _setup(db, monkeypatch)
    t = ptext(_download(iid))
    assert "Pago inicial" in t and "Supervisi" not in t and "Facturaci" not in t and "TOTAL GENERAL" in t
    monkeypatch.setenv(SWITCH, "false")
    assert normalized(_download(iid)) == normalized(_download(iid))   # stable
    assert ptext(_download(iid)) == t


@needs_db
def test_on_prints_lines_sections_charges_and_the_phase_block(db, monkeypatch):
    qid, _, iid = _setup(db, monkeypatch, label="Pago inicial / deposito")
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(_download(iid))
    for needle in ("ESTRUCTURA", "ACABADOS", "Viga IPE 200", "Instalacion y montaje", "Cargos Indirectos", "Supervisi",
                   "Contingencia", "10,000.00", "12,100.00", "2,178.00", "TOTAL COTIZACI", "14,278.00",
                   "Fase 1 - Pago inicial / deposito", "50% del total de la cotizaci", "6,050.00", "1,089.00",
                   "TOTAL DE ESTA FACTURA", "7,139.00", "Facturado acumulado (incl. esta fase)", "Pendiente de facturar"):
        assert needle in t, needle
    assert "TOTAL GENERAL" not in t and " de 2" not in t.split("Fase 1")[1][:40]
    # Resumen de Pagos still shows the invoice's own total
    assert t.index("TOTAL DE ESTA FACTURA") < t.index("Resumen de Pagos")
    # accumulated and pending after phase 1 of 50%
    tail = t.split("Facturado acumulado")[1]
    assert "7,139.00" in tail and "Pendiente de facturar" in tail


@needs_db
def test_on_changes_nothing_in_stored_data_or_in_the_conduce(db, monkeypatch):
    import invoices.service as isvc
    _, _, iid = _setup(db, monkeypatch)
    conduce_off = ptext(client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr()).content)
    rows_before = _rows(db, "SELECT subtotal, itbis_total, total_amount, tax_label FROM invoices WHERE id = %s", (iid,))
    monkeypatch.setenv(SWITCH, "true")
    _download(iid)
    conduce_on = ptext(client.get(f"/pdf/invoices/{iid}/conduce", headers=_hdr()).content)
    assert conduce_on == conduce_off and "Pago inicial" in conduce_on
    assert _rows(db, "SELECT subtotal, itbis_total, total_amount, tax_label FROM invoices WHERE id = %s", (iid,)) == rows_before
    inv = isvc.get_invoice_with_contact(iid)
    items, totals = isvc.invoice_lines_and_totals(inv, inv["included_charges"])
    assert len(items) == 1 and totals["lines_source"] == "phase_stored"   # unchanged for the conduce and the old path


@needs_db
def test_stored_phase_tax_wins_and_the_quote_tax_line_is_the_quotes(db, monkeypatch):
    qid, _, iid = _setup(db, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    conn = db["connect"]()
    conn.cursor().execute("UPDATE invoices SET itbis_total = 123.45, subtotal = total_amount - 123.45 WHERE id = %s", (iid,))
    conn.commit()
    conn.close()
    t = ptext(_download(iid))
    assert "123.45" in t                      # the phase line follows the stored column
    assert "2,178.00" in t                    # the quote summary still prints the quote's own tax
    assert t.count("123.45") == 1             # not repeated as the quote's tax


@needs_db
def test_fixed_and_final_phase_basis(db, monkeypatch):
    qid, _, iid = _setup(db, monkeypatch, kind="fixed", value=5000, label="Anticipo")
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(_download(iid))
    assert "Monto fijo acordado" in t and "del total de la cotizaci" not in t
    assert "762.71" in t and "4,237.29" in t and "5,000.00" in t and "9,278.00" in t   # tax share, base, total, pending
    final = _invoice_phase(_phase(qid, "final", None, "Pago final").json()["data"]["id"]).json()["data"]["invoice_id"]
    tf = ptext(_download(final))
    assert "Saldo final" in tf and "Fase 2 - Pago final" in tf


@needs_db
def test_accumulated_counts_earlier_non_cancelled_phases_and_the_phases_own_amount(db, monkeypatch):
    qid, p1, i1 = _setup(db, monkeypatch, kind="percent", value=40, label="Inicial")
    p2 = _phase(qid, "final", None, "Final").json()["data"]["id"]
    i2 = _invoice_phase(p2).json()["data"]["invoice_id"]
    monkeypatch.setenv(SWITCH, "true")
    t2 = ptext(_download(i2))
    acc2 = t2.split("Facturado acumulado")[1]
    assert "14,278.00" in acc2                               # 5,711.20 + 8,566.80
    assert client.patch(f"/invoices/{i1}/status", json={"status": "Cancelled"}, headers=_hdr()).status_code == 200
    t2 = ptext(_download(i2))
    acc2 = t2.split("Facturado acumulado")[1]
    assert "8,566.80" in acc2 and "5,711.20" in acc2          # accumulated = own phase only, pending = the cancelled share
    t1 = ptext(_download(i1))                                  # a cancelled invoice still counts its own phase
    acc1 = t1.split("Facturado acumulado")[1]
    assert "5,711.20" in acc1 and "8,566.80" in acc1


@needs_db
def test_quote_print_items_false_is_honored(db, monkeypatch):
    monkeypatch.setenv("PDF_PRINT_ITEMS_ENABLED", "true")
    _, _, iid = _setup(db, monkeypatch, print_items=False)
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(_download(iid))
    assert "Detalle de Items" not in t and "Viga IPE" not in t
    assert "Resumen Financiero" in t and "TOTAL DE ESTA FACTURA" in t


@needs_db
def test_hidden_lines_stay_hidden_but_count(db, monkeypatch):
    monkeypatch.setenv("PDF_FULL_COPY_ENABLED", "true")
    items = [dict(i) for i in ITEMS]
    items[3]["print_on_pdf"] = False
    _, _, iid = _setup(db, monkeypatch, items=items)
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(_download(iid))
    assert "Instalacion y montaje" not in t and "10,000.00" in t and "14,278.00" in t   # hidden line still in the totals
    full = ptext(client.get(f"/pdf/invoices/{iid}?full=true", headers=_hdr()).content)
    assert "Instalacion y montaje" in full


@needs_db
def test_non_itbis_tax_type_uses_the_generic_word(db, monkeypatch):
    _, _, iid = _setup(db, monkeypatch, tax_code="construction_17_2017")
    monkeypatch.setenv(SWITCH, "true")
    t = ptext(_download(iid))
    assert "Impuesto de la fase (parte proporcional)" in t and "ITBIS de la fase" not in t


@needs_db
def test_download_email_and_public_link_print_the_same_phase_document(db, monkeypatch):
    irouter = importlib.import_module("invoices.router")
    qid, _, iid = _setup(db, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    sent = []
    monkeypatch.setattr(irouter, "send_invoice_email", lambda **kw: sent.append(kw))
    assert client.post(f"/invoices/{iid}/send", headers=_hdr()).status_code == 200
    token = _rows(db, "SELECT public_token FROM invoices WHERE id = %s", (iid,))[0]["public_token"]
    public = client.get(f"/invoices/public/{token}/pdf")
    assert public.status_code == 200
    download, emailed = ptext(_download(iid)), ptext(sent[0]["pdf_bytes"])
    import re
    words = lambda t: [w for w in re.findall(r"\((.*?)\) ?Tj", t) if w.strip() and "\x00" not in w]   # spacers and glyph-id (signer) strings differ by path
    assert words(ptext(public.content)) == words(download), "public link"
    assert words(emailed) == words(download), "email"
    assert download.count("Supervisi") == 1


@needs_db
def test_email_and_public_paths_stay_as_today_when_off(db, monkeypatch):
    irouter = importlib.import_module("invoices.router")
    _, _, iid = _setup(db, monkeypatch)
    sent = []
    monkeypatch.setattr(irouter, "send_invoice_email", lambda **kw: sent.append(kw))
    assert client.post(f"/invoices/{iid}/send", headers=_hdr()).status_code == 200
    emailed = ptext(sent[0]["pdf_bytes"])
    assert "Facturaci" not in emailed and "Pago inicial" in emailed     # OFF: the old document, defects included


# ============================================================ Item B: NCF on the detail response

@needs_db
def test_detail_returns_the_ncf_and_its_expiry(db, monkeypatch):
    _, _, iid = _setup(db, monkeypatch)
    detail = client.get(f"/invoices/{iid}", headers=_hdr()).json()["data"]
    seq = _rows(db, "SELECT expiry_date FROM fiscal_sequences ORDER BY id LIMIT 1")[0]["expiry_date"]
    assert detail["ncf"].startswith("B01") and detail["ncf_valid_until"] == seq.isoformat()
    # shown regardless of the FISCAL switch: it is a stored fact of the document
    monkeypatch.delenv("FISCAL_NCF_ENABLED", raising=False)
    again = client.get(f"/invoices/{iid}", headers=_hdr()).json()["data"]
    assert again["ncf"] == detail["ncf"] and again["ncf_valid_until"] == detail["ncf_valid_until"]


@needs_db
def test_detail_without_ncf_has_nulls_and_lists_keep_their_shape(db, monkeypatch):
    from test_ncf_phases import _quote
    qid = _quote(db, 1000.0)
    inv = _convert(qid)
    assert inv.status_code == 200
    data = inv.json()["data"]
    iid = data.get("id") or data.get("invoice_id")
    detail = client.get(f"/invoices/{iid}", headers=_hdr()).json()["data"]
    assert detail["ncf"] is None and detail["ncf_valid_until"] is None
    listing = client.get("/invoices/", headers=_hdr()).json()["data"]
    assert listing and all("ncf" not in row and "ncf_valid_until" not in row for row in listing)


# ============================================================ download path: payments summary and headers

def _summary(t):
    import re
    w = re.findall(r"\((.*?)\) ?Tj", t)
    i = w.index("Resumen de Pagos")
    return dict(zip(("billed", "paid", "pending"), [w[w.index(k, i) + 1] for k in ("Total Facturado:", "Total Pagado:", "Pendiente:")]))


@needs_db
def test_download_path_payments_summary_follows_the_phase_invoice_and_prints_fiscal_and_client_blocks(db, monkeypatch):
    qid, _, iid = _setup(db, monkeypatch, kind="fixed", value=5000, label="Anticipo")
    assert client.post(f"/invoices/{iid}/payments", json={"amount": 1000.0, "method": "cash", "payment_date": "2026-11-26"},
                       headers=_hdr()).status_code in (200, 201)
    monkeypatch.setenv(SWITCH, "true")
    on = ptext(_download(iid))
    # The summary is the invoice's own: billed = this phase's total, paid = its payments, pending = the difference.
    assert _summary(on) == {"billed": "$5,000.00", "paid": "$1,000.00", "pending": "$4,000.00"}
    assert "TOTAL DE ESTA FACTURA:" in on and "$5,000.00" in on
    for needle in ("Factura de Crédito Fiscal", "NCF: B01", "Vencimiento NCF", "Cliente M2", "DESCRIPCIÓN", "PRECIO UNIT."):
        assert needle in on, needle
    # Same summary with the switch OFF: the layout switch never touches the payments block.
    monkeypatch.setenv(SWITCH, "false")
    assert _summary(ptext(_download(iid))) == {"billed": "$5,000.00", "paid": "$1,000.00", "pending": "$4,000.00"}
