"""PDF item descriptions wrap instead of being cut (PDF_DESC_WRAP_ENABLED). Off = old output, byte for byte."""
import hashlib
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_pdf_medidas_m2 as m2  # noqa: E402  (shared builder, fixtures and golden hash)
from test_pdf_medidas_m2 import _no_db_pages  # noqa: F401,E402
import pdf.builder_conduce as conduce  # noqa: E402

LONG = ("Suministro e instalacion de techo de aluzinc calibre 26 con aislante termico de 2 pulgadas, "
        "incluye tornilleria autoperforante, cumbrera, botaguas y mano de obra especializada FIN")


def visible(pdf_bytes):
    """Text drawn with Tj, joined, so a wrapped description reads as one string."""
    parts = re.findall(r"\((.*?)\)\s*Tj", m2.text(pdf_bytes))
    return " ".join(p.strip() for p in parts)


def stable(b):
    b = re.sub(rb"/CreationDate \(.*?\)", b"", b)
    return hashlib.sha256(re.sub(rb"/ID \[.*?\]", b"", b)).hexdigest()


def long_items(n=1):
    return [{"product_name": LONG, "quantity": 1, "unit_price": 10.0} for _ in range(n)]


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch):
    monkeypatch.delenv("PDF_DESC_WRAP_ENABLED", raising=False)


def test_off_is_byte_identical_to_old_golden():
    assert stable(m2.build()) == m2.GOLDEN_OLD_PDF


def test_off_still_cuts_long_description():
    assert "FIN" not in visible(m2.build(items=long_items()))


def test_on_prints_full_description(monkeypatch):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true")
    out = visible(m2.build(items=long_items()))
    assert "FIN" in out and "aluzinc" in out


def test_on_with_unidad_column(monkeypatch):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true")
    monkeypatch.setenv("PDF_MEDIDAS_ENABLED", "true")
    items = long_items()
    items[0]["unit_of_measure"] = "m2"
    out = visible(m2.build(items=items))
    assert "FIN" in out and "m2" in out
    for h in ("UND.", "CANT.", "P.U.", "TOTAL"):
        assert h in out
    assert "PRECIO UNIT." not in out and "CANTIDAD" not in out


def test_on_compact_columns_fit_large_amounts(monkeypatch):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true")
    items = [{"product_name": "Nave", "quantity": 12500, "unit_price": 1234567.89}]
    out = visible(m2.build(items=items))
    assert "$1,234,567.89" in out and "$15,432,098,625.00" in out


def test_on_short_rows_keep_one_line(monkeypatch):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true")
    out = visible(m2.build())
    assert "Viga W10x12 largo" in out and "Tornillo" in out


def test_on_many_long_rows_page_break(monkeypatch):
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true")
    out = m2.build(items=long_items(40))
    assert visible(out).count("FIN") == 40
    assert int(re.search(rb"/Count (\d+)", out).group(1)) >= 2


def _conduce(items):
    return conduce.create_conduce_pdf("C-1", "2026-11-03", {"company_name": "Cliente"}, "Proyecto", "", items).getvalue()


def test_conduce_off_cuts_on_wraps(monkeypatch):
    assert "FIN" not in visible(_conduce(long_items()))
    monkeypatch.setenv("PDF_DESC_WRAP_ENABLED", "true")
    assert "FIN" in visible(_conduce(long_items()))
