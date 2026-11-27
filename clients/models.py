from pydantic import BaseModel, Field, field_validator
from typing import Optional

class ClientBase(BaseModel):
    company_name: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    tax_id: Optional[str] = None
    notes: Optional[str] = None

class Client(ClientBase):
    id: int
    
    class Config:
        from_attributes = True

class ClientOut(Client):
    """Response used only while PAYMENT_DUE_ENABLED is on (R4). The OFF path keeps `Client`, byte-identical."""
    default_credit_days: Optional[int] = None
    default_payment_terms: Optional[str] = None


class ClientCreate(ClientBase):
    pass

class ClientUpdate(ClientBase):
    pass


class ClientInput(ClientBase):
    """POST/PUT body (debug B-12). Responses keep ClientBase so stored rows always serialize.

    - company_name: trimmed, required.
    - optional text fields: trimmed; blank becomes None (B-21: no more '' in tax_id or email).
    - default_credit_days / default_payment_terms (R4): honoured only while PAYMENT_DUE_ENABLED is on.
      Not sent = untouched on PUT; sent null = cleared.
    """
    default_credit_days: Optional[int] = Field(default=None, ge=0, le=365)
    default_payment_terms: Optional[str] = None

    @field_validator("default_payment_terms", mode="before")
    @classmethod
    def _terms_blank_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("company_name", mode="before")
    @classmethod
    def _company_required(cls, v):
        v = (v or "").strip() if isinstance(v, str) or v is None else v
        if not v:
            raise ValueError("El nombre de la empresa es obligatorio")
        return v

    @field_validator("contact_name", "email", "phone", "address", "tax_id", "notes", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v
