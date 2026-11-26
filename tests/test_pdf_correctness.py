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
