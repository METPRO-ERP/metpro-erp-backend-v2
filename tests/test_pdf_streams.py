"""Regression for the PDF test reader (tests/pdf_streams.py). No database.

A zlib stream whose last byte is 0x0D must extract in full. The legacy reader (kept below only as the reference
that proves the trigger is real) lost exactly that stream. Diagnosis: claude/pdf-test-flake-diagnosis-2026-11-27.md.
"""
import os
import re
import sys
import zlib

import pytest
from fpdf import FPDF

sys.path.insert(0, os.path.dirname(__file__))
import pdf_streams  # noqa: E402


def _legacy_text(pdf_bytes):
    """The pre-fix reader, verbatim in behaviour. Reference only; never used by a real test."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S):
        try:
            out.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except Exception:
            pass
    return "\n".join(out)


def _wrap(*bodies):
    parts = [b"%PDF-1.4\n"]
    for n, body in enumerate(bodies, 1):
        parts.append(b"%d 0 obj\n<< /Length %d >>\nstream\n" % (n, len(body)) + body + b"\nendstream\nendobj\n")
    parts.append(b"%%EOF\n")
    return b"".join(parts)


def _stream_ending_in_cr(prefix="BT (Torre Norte) Tj ET "):
    """A synthetic content stream whose compressed bytes end in 0x0D. Deterministic search."""
    for n in range(1, 600):  # 'a' is odd, so the byte sum (and the checksum's low byte) walks every residue
        payload = (prefix + "a" * n).encode()
        data = zlib.compress(payload)
        if data[-1] == 0x0D:
            return payload, data
    raise AssertionError("no CR-ended stream found")  # pragma: no cover


def test_synthetic_stream_ending_in_0d_extracts_in_full_and_the_legacy_reader_loses_it():
    payload, data = _stream_ending_in_cr()
    pdf = _wrap(data)
    assert pdf_streams.text(pdf) == payload.decode("latin-1")
    assert payload.decode("latin-1") not in _legacy_text(pdf)  # proves the trigger: red on the old reader


def test_real_fpdf2_page_ending_in_0d_extracts_in_full():
    """Same trigger through the real writer: find a document number whose page stream ends in 0x0D."""
    hit = None
    for n in range(10000, 12000):
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 10, f"Q-{n} Torre Norte Resumen Financiero")
        raw = bytes(pdf.output())
        if f"Q-{n} Torre Norte" not in _legacy_text(raw):
            hit = (n, raw)
            break
    assert hit, "no real fpdf2 page ended in 0x0D within the search range"
    n, raw = hit
    assert f"Q-{n} Torre Norte Resumen Financiero" in pdf_streams.text(raw)


def test_binary_non_zlib_streams_are_skipped():
    pdf = _wrap(bytes(range(256)), zlib.compress(b"BT (visible) Tj ET"))
    assert pdf_streams.text(pdf) == "BT (visible) Tj ET"


def test_streams_keep_file_order():
    pdf = _wrap(zlib.compress(b"first"), zlib.compress(b"second"))
    assert pdf_streams.streams(pdf) == ["first", "second"]


def test_a_corrupt_zlib_stream_fails_loudly():
    good = zlib.compress(b"BT (page text) Tj ET " * 20)
    pdf = _wrap(good[:-6])  # valid header, truncated body
    with pytest.raises(zlib.error):
        pdf_streams.text(pdf)
