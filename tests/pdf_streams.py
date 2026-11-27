"""Shared PDF stream reader for the tests (test code only; nothing here is imported by production).

Why this exists: the old readers framed streams with  stream\\r?\\n(.*?)\\r?\\nendstream  and swallowed every
decompress error. fpdf2 writes  stream\\n<data>\\nendstream. A zlib stream whose last byte (the low byte of
its checksum) is 0x0D lost that byte to the optional \\r?, decompress raised, and the page's text vanished
without a trace. Whether a page ends in 0x0D depends on its content (document number, preparer name, date),
so the failure looked random. See claude/pdf-test-flake-diagnosis-2026-11-27.md.

Rules here:
  * frame on the exact  \\nendstream  (fpdf2 never writes CRLF);
  * decode only streams that carry a valid zlib header (the CID map and other binary streams are skipped);
  * a stream with a valid zlib header that fails to decompress raises. No silent skip on this path.
"""
import re
import zlib

_STREAM = re.compile(rb"stream\r?\n(.*?)\nendstream", re.S)


def is_zlib(data: bytes) -> bool:
    return len(data) > 2 and (data[0] & 0x0F) == 8 and ((data[0] << 8) + data[1]) % 31 == 0


def streams(pdf_bytes: bytes) -> list:
    """Decoded (latin-1) text of every zlib stream, in file order."""
    out = []
    for m in _STREAM.finditer(pdf_bytes):
        data = m.group(1)
        if is_zlib(data):
            out.append(zlib.decompress(data).decode("latin-1"))
    return out


def text(pdf_bytes: bytes) -> str:
    return "\n".join(streams(pdf_bytes))
