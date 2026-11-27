"""PDF correctness release (2026-11-26): conduce page numbers and conduce project-name wrap.

TWIN GOLDENS. The three existing goldens (test_pdf_medidas_m2, test_ncf_phases) build the quote/invoice PDF with a
64-character project name. Twins use a project name that fits its column ("Torre Norte"). They were hashed on the
UNMODIFIED base (main 86b8a47) before any code change and must pass unchanged after it: quote and invoice bytes
are not touched by this release. Hashes hold only in the pinned environment (fpdf2==2.8.3, fonttools==4.62.0).
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import pdf_streams  # noqa: E402  (shared stream reader)
from test_pdf_medidas_m2 import build, normalized, _no_db_pages  # noqa: E402,F401

SHORT = "Torre Norte"
TWIN_OLD_PDF = "567d59054e6a48e9f5781f839139c096a57efab1375919de0b42371e1a106e26"
TWIN_PAYMENTS_BUILD = "de42a51a3174ee926865e176bf6935d4c4959772a4908393cbb5deeefc4bcd74"
TWIN_PLAIN_BUILD = "bdc4f038b8fc0c48a639d6037bd44c62e37298a80aa5447623b931c782c932a8"
GOLDEN_ITEMS = [{"product_name": "Viga W10x12 largo 6 m", "quantity": 10, "unit_price": 105.0, "unit_of_measure": "und"},
                {"product_name": "Tornillo autoperforante", "quantity": 500, "unit_price": 0.25}]


def _sha(b):
    return hashlib.sha256(normalized(b)).hexdigest()


def _twin_invoice(**kw):
    return _sha(build(doc_type="FACTURA", doc_id="F-10002", items=[dict(i) for i in GOLDEN_ITEMS], project_name=SHORT, **kw))


def test_twin_quote_unchanged():
    assert _sha(build(project_name=SHORT)) == TWIN_OLD_PDF


def test_twin_invoice_with_payment_unchanged():
    got = _twin_invoice(payments=[{"payment_date": "2026-10-24", "amount": 500.0, "method": "Transferencia"}],
                        amount_paid=500.0, amount_due=739.0, valid_until="", payment_terms="Contado")
    assert got == TWIN_PAYMENTS_BUILD


def test_twin_invoice_plain_unchanged():
    assert _twin_invoice() == TWIN_PLAIN_BUILD


# ============================================================ conduce: page numbers and project-name wrap
import re  # noqa: E402
import ast  # noqa: E402
import pathlib  # noqa: E402

import pytest  # noqa: E402
from fpdf import FPDF  # noqa: E402

from pdf import builder_conduce  # noqa: E402

PT = 72 / 25.4
CLIENT = {"company_name": "Grupo Ramos Constructora SA", "contact_name": "Ana Perez",
          "address": "Av. Winston Churchill 1099, Santo Domingo"}


_streams = pdf_streams.streams  # shared reader (tests/pdf_streams.py)


def _page_count(pdf_bytes):
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf_bytes))


def footers(pdf_bytes):
    """[(page, total)] per page. fpdf2 emits the alias footer as two strings: (Página k de ) Tj (N) Tj."""
    found = []
    for s in _streams(pdf_bytes):
        found += [(int(k), int(n)) for k, n in re.findall(r"\(P\xe1gina (\d+) de \) Tj \((\d+)\) Tj", s)]
    return found


def _cells(pdf_bytes):
    """[(x_pt, y_pt, text)] for every single-string text cell, in drawing order."""
    cells = []
    for s in _streams(pdf_bytes):
        cells += [(float(x), float(y), t) for x, y, t in re.findall(r"q BT ([\d.]+) ([\d.]+) Td [\d. ]+ rg \((.*?)\) Tj ET Q", s)]
    return cells


def conduce(n_items=3, project="Torre Norte", **kw):
    items = [{"product_name": f"Item {i}", "quantity": 1} for i in range(n_items)]
    return builder_conduce.create_conduce_pdf("C-1", "2026-11-03", dict(CLIENT), project, "", items, signer_name="Fabian").getvalue()


# ---- Defect 1

@pytest.mark.parametrize("wrap", ["true", "false"])
@pytest.mark.parametrize("n", [0, 1, 30, 44, 45, 50, 52, 55, 60, 120, 250])
def test_conduce_every_page_has_its_number_and_the_true_total(monkeypatch, n, wrap):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", wrap)
    out = conduce(n)
    pages = _page_count(out)
    assert footers(out) == [(k, pages) for k in range(1, pages + 1)]


def test_single_page_conduce_is_one_page_and_says_1_of_1():
    out = conduce(3)
    assert _page_count(out) == 1  # base 86b8a47 produced a second, footer-only page here
    assert footers(out) == [(1, 1)]


def test_multi_page_conduce_numbers_all_pages():
    out = conduce(120)
    pages = _page_count(out)
    assert pages >= 3
    assert footers(out) == [(k, pages) for k in range(1, pages + 1)]


def test_conduce_footer_style_unchanged():
    # D1: italic 8, grey 180 (0.7059), centred as before.
    s = "\n".join(_streams(conduce(3)))
    assert re.search(r"/F\d+ 8\.00 Tf ET\s+q BT [\d.]+ [\d.]+ Td 0\.7059 0\.7059 0\.7059 rg \(P\xe1gina 1 de \) Tj \(1\) Tj", s)


def _doc_footers(builder):
    from pdf import builder_rfq, builder_po
    doc = {"rfq_number": 1, "po_number": 1, "vendor_name": "V", "title": "T", "rfq_date": "2026-11-01", "po_date": "2026-11-01",
           "status": "Draft", "subtotal": 1, "total": 1, "vendor_tax_id": "", "notes": "", "bill_to": "", "payment_terms": "",
           "vendor_contact": "", "vendor_email": "", "vendor_phone": "", "vendor_address": ""}
    items = [{"item": f"Item {i}", "description": "d", "quantity": 1, "unit": "u", "unit_price": 1, "amount": 1, "position": i} for i in range(120)]
    fn = builder_rfq.create_rfq_pdf if builder == "rfq" else builder_po.create_po_pdf
    out = fn(dict(doc), items)
    return out.getvalue() if hasattr(out, "getvalue") else bytes(out)


@pytest.mark.parametrize("which", ["rfq", "po"])
def test_rfq_and_po_numbering_stays_correct(which):
    out = _doc_footers(which)
    pages = _page_count(out)
    assert pages >= 3 and footers(out) == [(k, pages) for k in range(1, pages + 1)]


def test_quote_and_invoice_print_no_page_claim():
    # D2: numbering on quote/invoice is out of scope. Policy pinned: no "Página" text, so no false claim.
    items = [{"product_name": f"Viga {i}", "quantity": 1, "unit_price": 100.0} for i in range(120)]
    for doc_type in ("COTIZACION", "FACTURA"):
        out = build(doc_type=doc_type, items=items, project_name=SHORT)
        assert _page_count(out) >= 3
        assert all("gina" not in s for s in _streams(out))


# Every module that creates a PDF object, and its footer policy. A new builder fails here until it is classified.
PDF_BUILDERS = {
    "pdf/builder_conduce.py": "numbered (_ConducePdf)",
    "pdf/builder_rfq.py": "numbered (_RfqPdf)",
    "pdf/builder_po.py": "numbered (uses _RfqPdf)",
    "pdf/builder_receipt.py": "numbered (_ReceiptPdf, R3)",
    "pdf/builder_cover.py": "none",
    "pdf/builder_terms.py": "none",
    "pdf/utils/layout_utils.py": "none (quote and invoice)",
}


def test_closed_set_of_pdf_builders():
    root = pathlib.Path(__file__).resolve().parent.parent
    users = set()
    for path in (root / "pdf").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(getattr(b, "id", "") == "FPDF" for b in node.bases):
                users.add(str(path.relative_to(root)))
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("FPDF", "_RfqPdf", "_ConducePdf"):
                users.add(str(path.relative_to(root)))
    assert users == set(PDF_BUILDERS)


# ---- Defect 2 (conduce)

LONG_NAMES = {
    "64": "Nave industrial en Santiago, proyecto de prueba con nombre largo",
    "90": "Construccion de nave industrial y estructura metalica Fase 2 - Zona Franca Santiago",
    "120": " ".join(f"palabra{i}" for i in range(14)),
    "400": " ".join(f"palabra{i}" for i in range(50)),
    "token80": "X" * 80,
}


def _project_lines(out):
    cells = _cells(out)
    i = next(k for k, c in enumerate(cells) if c[2] == "Proyecto:")
    j = next(k for k, c in enumerate(cells) if c[2] == "Cliente:")
    return cells[i + 1:j]


@pytest.mark.parametrize("key", sorted(LONG_NAMES))
def test_conduce_prints_the_whole_project_name(key):
    name = LONG_NAMES[key]
    out = conduce(3, project=name)
    printed = "".join(t for _, _, t in _project_lines(out))
    assert printed.replace(" ", "") == name.replace(" ", "")  # every character, nothing cut


@pytest.mark.parametrize("key", sorted(LONG_NAMES))
def test_conduce_project_name_stays_inside_its_column(key):
    out = conduce(3, project=LONG_NAMES[key])
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "", 7)
    for x, _, t in _project_lines(out):
        assert x / PT + pdf.get_string_width(t) <= 110  # the client column starts at 110 mm


@pytest.mark.parametrize("key", sorted(LONG_NAMES))
def test_conduce_items_start_below_the_wrapped_project_name(key):
    out = conduce(3, project=LONG_NAMES[key])
    cells = _cells(out)
    last_project_y = min(y for _, y, _ in _project_lines(out))  # PDF y grows upward
    items_y = next(y for _, y, t in cells if t == "Items Entregados")
    assert items_y < last_project_y


def test_conduce_name_that_fits_keeps_one_line():
    assert len(_project_lines(conduce(3, project="Torre Norte"))) == 1


def test_conduce_wrap_and_page_numbers_together():
    out = conduce(120, project=LONG_NAMES["400"])
    pages = _page_count(out)
    assert footers(out) == [(k, pages) for k in range(1, pages + 1)]
