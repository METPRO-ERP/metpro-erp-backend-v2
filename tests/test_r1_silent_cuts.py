"""Release R1: client header fields and the payments-table note wrap instead of being cut.

Short values keep the exact bytes main produced (the existing goldens in test_payment_due.py and
test_ncf_phases.py stay untouched). New long-value goldens are pinned here after a visual read of each render.
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import build, normalized, _no_db_pages  # noqa: F401,E402
from test_payment_due import ptext  # noqa: E402

LONGC = {
    "company_name": "Corporacion Industrial Metalmecanica del Caribe y Servicios Generales de Construccion SRL",
    "contact_name": "Ing. Maria del Carmen Fernandez de la Rosa Santana, Gerente de Proyectos",
    "email": "administracion.proyectos.compras@corporacionindustrialcaribe.com.do",
    "phone": "+1 (809) 555-0123 / (829) 555-0199 / (849) 555-0177 ext. 4455",
    "address": "Autopista Duarte Km 12 1/2, Parque Industrial Zona Franca, Nave 7, Santiago de los Caballeros, Republica Dominicana",
    "tax_id": "1-31-71683-2 / 131-71683-2 (RNC alterno 101-00000-0)",
}
SHORT = {"company_name": "Cliente SRL", "contact_name": "Juan Perez", "email": "j@x.com",
         "phone": "809-555-0100", "address": "Calle 1, Santiago", "tax_id": "131-00000-1"}
PAY = [{"payment_date": "2026-11-20", "method": "Transferencia",
        "amount": 1500000, "notes": "Transferencia ref. 884412990033 correspondiente al primer avance de obra segun acuerdo del 15 de noviembre con la gerencia"}]
PROD = ("PDF_DESC_WRAP_ENABLED", "PDF_PROJECT_DESC_ENABLED", "PORTADA_NOTE_ENABLED", "PAYMENT_DUE_ENABLED")
GOLDEN = {
    "all_long_invoice": "62cc714f3865648fbb6ff3baaec6cc6a5e02dc361e3237c2113f359641be7a99",
    "all_long_quote": "6a2991bff2655d6a8f759bb75a87c56f0818d7e8cda3451259632b0522e26560",
    "long_payment_note": "b82d5735e1577b24fcecc5d38c13d1fe0ff5d90d535fe5b30ef8c4a24444a418",
}


@pytest.fixture(autouse=True)
def _prod_flags(monkeypatch):
    for n in PROD:
        monkeypatch.setenv(n, "true")


CASES = {
    "all_long_invoice": dict(doc_type="FACTURA", doc_id="F-9999", client=LONGC, payment_terms="Credito 30 dias"),
    "all_long_quote": dict(doc_type="COTIZACION", doc_id="Q-9999", client=LONGC, payment_terms="Credito 30 dias"),
    "long_payment_note": dict(doc_type="FACTURA", doc_id="F-9999", client=SHORT, payment_terms="Credito 30 dias",
                              payments=PAY, amount_paid=1500000.0, amount_due=500000.0),
}


@pytest.mark.parametrize("name", list(CASES))
def test_long_value_goldens_are_pinned(name):
    assert hashlib.sha256(normalized(build(**CASES[name]))).hexdigest() == GOLDEN[name]


@pytest.mark.parametrize("name", ["all_long_invoice", "all_long_quote"])
def test_every_client_value_prints_in_full(name):
    t = ptext(build(**CASES[name]))
    for tail in ("Servicios Generales de Construccion SRL", "Gerente de Proyectos", "caribe.com.do",
                 "4455", "Republica Dominicana", "101-00000-0)"):
        assert tail in t, tail


def test_long_payment_note_prints_in_full():
    t = ptext(build(**CASES["long_payment_note"]))
    assert "noviembre con la gerencia" in t


def test_header_grows_so_items_do_not_overlap():
    long_ = build(**CASES["all_long_invoice"])
    # the items heading must come after the RNC line in the content stream
    t = ptext(long_)
    assert t.index("101-00000-0)") < t.index("Detalle de Items")
