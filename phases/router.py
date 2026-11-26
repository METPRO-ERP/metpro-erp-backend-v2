from fastapi import APIRouter, Depends

from auth.service import require_auth, require_role
from core import phased_invoicing
from core.role_policy import enforce_admin
from . import service
from .models import PhaseIn

router = APIRouter(tags=["phases"])
admin = Depends(require_role("admin"))  # admin and superadmin


def _uid(user: dict):
    return user.get("user_id") or user.get("sub")


@router.get("/phases/options")
def phase_options(current_user: dict = Depends(require_auth)):
    """Always available, so screens know whether to show the mode choice and the phase panel."""
    return {"phased_enabled": phased_invoicing.phased_enabled()}


@router.get("/quotes/{quote_id}/phases")
def list_phases(quote_id: str, user: dict = admin):
    service.require_enabled()
    enforce_admin(user)
    return service.list_phases(quote_id)


@router.post("/quotes/{quote_id}/phases")
def create_phase(quote_id: str, body: PhaseIn, user: dict = admin):
    service.require_enabled()
    enforce_admin(user)
    return service.create_phase(quote_id, body, _uid(user))


@router.post("/phases/{phase_id}/invoice")
def invoice_phase(phase_id: int, user: dict = admin):
    service.require_enabled()
    enforce_admin(user)
    invoice = service.generate_invoice(phase_id, _uid(user))
    out = {
        "invoice_id": invoice["id"], "invoice_number": invoice["invoice_number"], "ncf": invoice.get("ncf"),
        "quote_id": invoice["quote_id"], "client_id": invoice["client_id"], "total_amount": invoice["total_amount"],
        "status": invoice["status"], "invoice_date": invoice["invoice_date"],
        "quote_fully_invoiced": invoice.get("quote_fully_invoiced", False),
        "message": f"Factura {invoice['invoice_number']} creada para la fase",
    }
    if invoice.get("ncf_warning"):
        out["ncf_warning"] = invoice["ncf_warning"]
    return out
