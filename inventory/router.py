from typing import Optional

from fastapi import APIRouter, Depends

from auth.service import require_auth
from core import inventory as inv
from . import service
from .models import AdjustIn, IssueIn, ItemIn, ItemPatch, OpeningIn, ReceiptIn, VoidIn

# Every route except /inventory/options checks the switch FIRST (404 FEATURE_DISABLED, even for non-admins), then the
# role. Only issue_job (and reading items and on-hand quantities, without any cost) is open to any signed-in user (I-D8).
router = APIRouter(tags=["inventory"])


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "")


def _admin(user: dict) -> None:
    service.require_enabled()
    service.require_admin(user)


@router.get("/inventory/options")
def inventory_options(current_user: dict = Depends(require_auth)):
    """Always available, so screens know whether to show Inventario. Fail-closed on the client."""
    return service.options()


@router.get("/inventory/warehouses")
def list_warehouses(current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return service.list_warehouses()


@router.get("/inventory/items")
def list_items(include_inactive: bool = False, warehouse_id: Optional[int] = None, current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return service.list_items(current_user, include_inactive, warehouse_id)


@router.get("/inventory/items/{item_id}")
def get_item(item_id: int, current_user: dict = Depends(require_auth)):
    service.require_enabled()
    return service.get_item(current_user, item_id)


@router.post("/inventory/items")
def create_item(body: ItemIn, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.create_item(body, _actor(current_user))


@router.patch("/inventory/items/{item_id}")
def update_item(item_id: int, body: ItemPatch, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.update_item(item_id, body, set(body.model_fields_set))


@router.post("/inventory/receipts")
def post_receipt(body: ReceiptIn, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.post_receipt(body, _actor(current_user))


@router.post("/inventory/issues")
def post_issue(body: IssueIn, current_user: dict = Depends(require_auth)):
    service.require_enabled()  # any signed-in user may issue to a job; the response carries no cost unless the caller is an admin
    return service.post_issue(body, _actor(current_user), service.is_admin(current_user))


@router.post("/inventory/adjustments")
def post_adjustment(body: AdjustIn, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.post_adjustment(body, _actor(current_user))


@router.post("/inventory/opening/preview")
def opening_preview(body: OpeningIn, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.opening_preview(body)


@router.post("/inventory/opening")
def post_opening(body: OpeningIn, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.post_opening(body, _actor(current_user))


@router.get("/inventory/movements")
def list_movements(item_id: Optional[int] = None, kind: Optional[str] = None, quote_id: Optional[str] = None,
                   limit: int = 100, offset: int = 0, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.list_movements(item_id, kind, quote_id, limit, offset)


@router.get("/inventory/movements/{movement_id}")
def get_movement(movement_id: int, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.get_movement(movement_id)


@router.post("/inventory/movements/{movement_id}/void")
def void_movement(movement_id: int, body: VoidIn, current_user: dict = Depends(require_auth)):
    _admin(current_user)
    return service.void_movement(movement_id, body.reason, _actor(current_user))
