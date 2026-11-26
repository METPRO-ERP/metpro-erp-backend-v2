from fastapi import APIRouter, Depends

from auth.service import require_auth, require_role
from core import conduce_ledger
from core.role_policy import enforce_admin
from . import pdf as conduce_pdf
from . import service
from .models import ConduceCreate, ConduceVoid

router = APIRouter(tags=["conduces"])
admin = Depends(require_role("admin"))


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "")


@router.get("/conduces/options")
def conduce_options(current_user: dict = Depends(require_auth)):
    """Always available, so screens know whether to show the delivery panel. Fail-closed on the client."""
    return {"enabled": conduce_ledger.ledger_enabled()}


@router.get("/conduces/invoice/{invoice_id}/state")
def invoice_state(invoice_id: int, current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return service.invoice_state(invoice_id)


@router.get("/conduces/quote/{quote_id}/state")
def quote_state(quote_id: str, current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return service.quote_state(quote_id)


@router.post("/conduces")
def create_conduce(body: ConduceCreate, current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return service.create_conduce(body, _actor(current_user))


@router.get("/conduces/{conduce_id}/pdf")
def get_conduce_pdf(conduce_id: str, print_seal: bool = False, current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return conduce_pdf.conduce_pdf(conduce_id, current_user, print_seal)


@router.post("/conduces/{conduce_id}/void")
def void_conduce(conduce_id: str, body: ConduceVoid, user: dict = admin):
    service.require_enabled()
    enforce_admin(user)
    return service.void_conduce(conduce_id, body.reason, _actor(user))
