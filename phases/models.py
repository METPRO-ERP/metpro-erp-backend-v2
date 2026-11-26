from typing import Literal, Optional

from pydantic import BaseModel, Field


class PhaseIn(BaseModel):
    """One phase at a time. 'final' takes exactly the remaining balance (the amount value is ignored)."""
    label: str = Field(..., min_length=1, max_length=120)
    amount_input_type: Literal["percent", "fixed", "final"]
    amount_input_value: Optional[float] = None
    note: Optional[str] = Field(None, max_length=500)
