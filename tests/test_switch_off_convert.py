"""Switch-OFF conversion, end to end through the real route (finding 1 of the 2026-11-25 production checks).

Both switches are absent. "Convertir en factura" must create the invoice and mark the quote Invoiced exactly as on ea07edf,
whatever headers the browser sends (it posts JSON headers with an empty body), and for a quote that was invoiced before and
whose invoice was cancelled (production Q-10002), which comes back as Approved and is invoiced again as F-<n>-2.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_ncf_phases import db, _base_db, _quote, _hdr, _rows, _status, client, needs_db  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _switches_absent(monkeypatch):
    monkeypatch.delenv("FISCAL_NCF_ENABLED", raising=False)
    monkeypatch.delenv("PHASED_INVOICING_ENABLED", raising=False)


@needs_db
@pytest.mark.parametrize("content_type", [None, "application/json", "text/plain"])
def test_switch_off_full_conversion_with_browser_headers_and_empty_body(db, content_type):
    qid = _quote(db, 2555.0)
    headers = _hdr()
    if content_type:
        headers["Content-Type"] = content_type   # the frontend sends application/json with no body
    r = client.post(f"/quotes/{qid}/convert-to-invoice", headers=headers, content=b"")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data) == {"invoice_id", "invoice_number", "quote_id", "client_id", "total_amount", "status", "invoice_date", "message"}
    inv = _rows(db, "SELECT status, ncf, fiscal_sequence_id, phase_id FROM invoices WHERE id = %s", (data["invoice_id"],))[0]
    assert inv["status"] == "Pending" and inv["ncf"] is None and inv["fiscal_sequence_id"] is None and inv["phase_id"] is None
    assert _status(db, qid) == "Invoiced"


@needs_db
def test_switch_off_reconversion_after_cancelled_invoice_like_q_10002(db):
    qid = _quote(db, 3015.51)
    first = client.post(f"/quotes/{qid}/convert-to-invoice", headers=_hdr()).json()["data"]
    assert client.patch(f"/invoices/{first['invoice_id']}/status", json={"status": "Cancelled"}, headers=_hdr()).status_code == 200
    assert _status(db, qid) == "Approved"             # cancel reopens the quote (B-06)
    r = client.post(f"/quotes/{qid}/convert-to-invoice", headers={**_hdr(), "Content-Type": "application/json"}, content=b"")
    assert r.status_code == 200, r.text
    second = r.json()["data"]
    assert second["invoice_id"] != first["invoice_id"] and second["invoice_number"].endswith("-2")
    assert _status(db, qid) == "Invoiced"
    rows = _rows(db, "SELECT status, ncf FROM invoices WHERE quote_id = %s ORDER BY id", (qid,))
    assert [x["status"] for x in rows] == ["Cancelled", "Pending"] and all(x["ncf"] is None for x in rows)
