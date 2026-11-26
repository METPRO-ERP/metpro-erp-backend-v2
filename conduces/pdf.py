"""Responses for the ledger conduce PDF. The old route and the new route share this one builder."""
from fastapi.responses import StreamingResponse

from company_settings.service import seal_for_pdf
from pdf.builder_conduce import create_conduce_ledger_pdf
from pdf.utils.signer import resolve_signer_name
from . import service


def _response(data: dict, current_user, print_seal: bool) -> StreamingResponse:
    stream = create_conduce_ledger_pdf(
        number=data["number"], seq=data["seq"], doc_date=data["date"], quote_label=data["quote_label"],
        invoice_label=data["invoice_label"], client=data["client"], project_name=data["project_name"], notes=data["notes"],
        lines=data["lines"], marker=data["marker"],
        signer_name=resolve_signer_name(current_user, data.get("created_by_name")), seal=seal_for_pdf(print_seal))
    return StreamingResponse(stream, media_type="application/pdf",
                             headers={"Content-Disposition": f"attachment; filename={data['number']}_conduce.pdf"})


def conduce_pdf(conduce_id: str, current_user: dict, print_seal: bool = False) -> StreamingResponse:
    return _response(service.conduce_pdf_data(conduce_id=conduce_id), current_user, print_seal)


def latest_conduce_pdf_for_invoice(invoice_id: int, current_user: dict, print_seal: bool = False) -> StreamingResponse:
    """The old GET route with the switch ON: serves the latest active conduce of the invoice's quote. Never creates."""
    quote_id = service.invoice_state(invoice_id)["quote_id"]
    return _response(service.conduce_pdf_data(latest_for_quote=quote_id), current_user, print_seal)
