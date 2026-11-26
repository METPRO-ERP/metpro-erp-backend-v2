from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class SequenceIn(BaseModel):
    """Batch values from the OFV authorization. next_number is typed twice and must match."""
    series: str = Field(..., min_length=1, max_length=1)
    next_number: int = Field(..., ge=1)
    next_number_confirm: int = Field(..., ge=1)
    range_start: int = Field(..., ge=1)
    range_end: int = Field(..., ge=1)
    expiry_date: date
    confirm: bool = False   # false: validate and return the batch for the confirmation screen, write nothing
