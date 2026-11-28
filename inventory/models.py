"""Inventory request models. Every model forbids extra fields: a client can never send a value, unit cost, unit or sign
the backend computes. Quantities and costs are validated in the service (decimals, positivity)."""
from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

Kind = Literal["raw_material", "consumable"]
Unit = Literal["Ft", "Lb", "Unidad"]


def _blank_to_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class ItemIn(_Base):
    sku: str
    name: str
    kind: Kind
    stock_unit: Unit
    tracks_lots: bool = False
    standard_length_ft: Optional[float] = None
    product_id: Optional[int] = None
    is_dummy: bool = False
    notes: Optional[str] = None

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v):
        return _blank_to_none(v)


class ItemPatch(_Base):
    """Identity (sku, kind, unit, lot tracking, dummy flag) is not patchable: it is frozen once an item has movements."""
    name: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None
    standard_length_ft: Optional[float] = None
    product_id: Optional[int] = None


class ReceiptLine(_Base):
    item_id: int
    quantity: float
    total_cost: float
    lot_ref: Optional[str] = None
    weight_lb: Optional[float] = None

    @field_validator("lot_ref", mode="before")
    @classmethod
    def _lot(cls, v):
        return _blank_to_none(v)


class ReceiptIn(_Base):
    warehouse_id: Optional[int] = None
    occurred_on: Optional[date] = None
    supplier_id: Optional[int] = None
    supplier_ref: Optional[str] = None
    note: Optional[str] = None
    lines: List[ReceiptLine]

    @field_validator("supplier_ref", "note", mode="before")
    @classmethod
    def _b(cls, v):
        return _blank_to_none(v)


class IssueLine(_Base):
    item_id: int
    quantity: float


class IssueIn(_Base):
    warehouse_id: Optional[int] = None
    occurred_on: Optional[date] = None
    quote_id: str
    project_id: Optional[int] = None
    note: Optional[str] = None
    lines: List[IssueLine]

    @field_validator("note", mode="before")
    @classmethod
    def _b(cls, v):
        return _blank_to_none(v)


class AdjustLine(_Base):
    item_id: int
    quantity: float          # signed: positive = gain (needs total_cost), negative = loss (valued at the average)
    total_cost: Optional[float] = None


class AdjustIn(_Base):
    warehouse_id: Optional[int] = None
    occurred_on: Optional[date] = None
    reason: str
    note: Optional[str] = None
    lines: List[AdjustLine]

    @field_validator("note", mode="before")
    @classmethod
    def _b(cls, v):
        return _blank_to_none(v)


class OpeningRow(_Base):
    sku: str
    name: str
    kind: Kind
    stock_unit: Unit
    tracks_lots: bool = False
    standard_length_ft: Optional[float] = None
    quantity: float
    total_cost: float
    lot_ref: Optional[str] = None
    weight_lb: Optional[float] = None

    @field_validator("lot_ref", mode="before")
    @classmethod
    def _lot(cls, v):
        return _blank_to_none(v)


class OpeningIn(_Base):
    warehouse_id: Optional[int] = None
    occurred_on: Optional[date] = None
    note: Optional[str] = None
    rows: List[OpeningRow]

    @field_validator("note", mode="before")
    @classmethod
    def _b(cls, v):
        return _blank_to_none(v)


class VoidIn(_Base):
    reason: str
