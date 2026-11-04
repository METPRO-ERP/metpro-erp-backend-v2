from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth.service import require_role
from cover_page import service
from cover_page.defaults import allowed_fields, default_bodies, default_body
from pdf.builder_cover import create_cover_pdf
from pdf.utils.signer import resolve_signer_name

router = APIRouter(prefix="/cover-page", tags=["cover-page"])


class CoverSave(BaseModel):
    body: str
    enabled: bool = True


class CoverPreview(BaseModel):
    body: Optional[str] = None


@router.get("/")
def list_templates(_admin: dict = Depends(require_role("admin"))):
    """Both cover templates plus the fields each one accepts and the built-in default text."""
    return {
        "templates": service.get_all_templates(),
        "fields": {t: list(allowed_fields(t)) for t in ("quote", "invoice")},
        "defaults": default_bodies(),
    }


@router.put("/{doc_type}")
def save_template(doc_type: str, payload: CoverSave, admin: dict = Depends(require_role("admin"))):
    return service.save_template(doc_type, payload.body, payload.enabled, admin.get("sub"))


@router.post("/{doc_type}/preview")
def preview(doc_type: str, payload: CoverPreview, admin: dict = Depends(require_role("admin"))):
    """One-page PDF of the cover with sample data. Uses the posted text, so unsaved edits can be previewed."""
    service.validate_body(doc_type, "x")  # 404 for an unknown type before default_body is read
    body = service.validate_body(doc_type, payload.body if payload.body is not None else default_body(doc_type))
    kind = doc_type
    stream = create_cover_pdf(
        kind=kind,
        body=body,
        client_name="Cliente de Ejemplo SRL",
        contact_name="Juan Pérez",
        user_name=resolve_signer_name(admin) or "Representante Metpro",
        date_text="2026-10-17",
        doc_label="Q-10001" if kind == "quote" else "F-10001",
        note="Nave industrial de 18 x 45 m con techo de aluzinc calibre 26." if kind == "quote" else "",
    )
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=portada_{kind}_vista_previa.pdf"},
    )
