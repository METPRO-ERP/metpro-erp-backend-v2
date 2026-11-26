"""Conduce OFF golden (conduce ledger plan, step 1, pinned BEFORE any code change on 2026-11-26).

The conduce builder with CONDUCE_LEDGER_ENABLED off (or absent) must keep producing exactly these bytes. Four fixtures:
short and long list, description wrap off and on. Like the other goldens, the hashes depend on fpdf2 2.8.3 and
fonttools==4.62.0 (requirements.txt); changing either means re-baselining all goldens together.
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_pdf_medidas_m2 import normalized  # noqa: E402
from pdf import builder_conduce  # noqa: E402

CLIENT = {"company_name": "Cliente Golden", "contact_name": "Ana", "address": "Av. Central 1", "email": "", "phone": ""}
SHORT = [{"product_name": "Viga IPE 200 x 6 m", "quantity": 10, "category": "ESTRUCTURA"},
         {"product_name": "Columna HEB 160 x 6 m", "quantity": 5, "category": "ESTRUCTURA"},
         {"product_name": "Pintura anticorrosiva", "quantity": 100.5, "category": "ACABADOS"}]
LONG = [{"product_name": f"Item {i} " + "descripcion larga " * (i % 4), "quantity": i + 1,
         "category": "A" if i < 30 else "B"} for i in range(70)]

PINS = {  # (fixture, wrap) -> sha256 of the normalized PDF bytes
    ("short", "off"): "e9702bfbe40bf6054dac1e9cea53e5291a7aa7ed0ba1704789fcfb49089dc487",
    ("short", "on"): "e02766a750461921a918cc7c25c8aee6985468d9ddc5aeeeba451f248466a93e",
    ("long", "off"): "36c1be4ba3a7bebe28bb6ba6a317c5792164f0f88216a34e8e96a52d79a3d290",
    ("long", "on"): "2beff1e7aa90a61fd84e633a578c8a66729c2b628dcaaa552ee5de3572b22a8d",
}


def _build(items, wrap, monkeypatch):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true" if wrap else "false")
    out = builder_conduce.create_conduce_pdf("C-10002", "2026-11-03", dict(CLIENT), "Torre Norte", "", [dict(i) for i in items],
                                             signer_name="Fabian")
    return hashlib.sha256(normalized(out.getvalue())).hexdigest()


@pytest.mark.parametrize("name,items,wrap", [("short", SHORT, False), ("short", SHORT, True), ("long", LONG, False), ("long", LONG, True)])
def test_conduce_off_golden(name, items, wrap, monkeypatch):
    got = _build(items, wrap, monkeypatch)
    assert got == PINS[(name, "on" if wrap else "off")], got
