from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class ConduceLineIn(BaseModel):
    quote_item_id: int
    quantity: Decimal


class ConduceCreate(BaseModel):
    invoice_id: int
    delivery_date: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=500)
    lines: List[ConduceLineIn]


class ConduceVoid(BaseModel):
    reason: str = Field(max_length=300)
