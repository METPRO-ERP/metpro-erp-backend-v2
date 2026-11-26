from fastapi import APIRouter, Depends

from auth.service import require_role
from core.role_policy import enforce_admin
from . import service
from .models import SequenceIn

router = APIRouter(prefix="/fiscal", tags=["fiscal"])
admin = Depends(require_role("admin"))  # admin and superadmin


def _uid(user: dict):
    return user.get("user_id") or user.get("sub")


@router.get("/sequence")
def sequence_status(user: dict = admin):
    """Admin status panel: batch, remaining numbers, days to expiry, warnings."""
    enforce_admin(user)
    return service.get_status()


@router.post("/sequence/activate")
def activate_sequence(body: SequenceIn, user: dict = admin):
    """confirm=false returns the batch for the confirmation screen; confirm=true activates it. Does not turn the switch on."""
    enforce_admin(user)
    return service.activate(body, _uid(user))


@router.post("/sequence/correct")
def correct_sequence(body: SequenceIn, user: dict = admin):
    """Allowed only while zero NCFs have been issued from the active batch."""
    enforce_admin(user)
    return service.correct(body, _uid(user))
