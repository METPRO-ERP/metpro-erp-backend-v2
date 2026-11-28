"""Inventory stock ledger rules. Plan: claude/inventory-module-plan-2026-11-28.md, decisions I-D2 to I-D16.

Switch: INVENTORY_ENABLED, default off. Off = every inventory route answers 404 FEATURE_DISABLED and nothing reads or
writes the inventory tables. Nothing in this module touches quotes, invoices, conduces or products.

Valuation is weighted average per item and warehouse. For one (item, warehouse): Q = SUM(quantity), V = SUM(value) over
all lines. An issue of q takes value round(q x V / Q, 2); issuing all of Q takes exactly V, so no residue is ever left.
The value is stored on the line at posting and never recomputed.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from core.switches import switch_on

CENT = Decimal("0.01")
COST4 = Decimal("0.0001")
UNITS = ("Ft", "Lb", "Unidad")
KINDS = ("raw_material", "consumable")


def inventory_enabled() -> bool:
    return switch_on("INVENTORY_ENABLED")


def movement_label(number) -> str:
    return f"M-{int(number):06d}"


def parse_decimal(value, places: int = 2) -> Optional[Decimal]:
    """A finite Decimal with at most `places` decimals (sign kept), or None when invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    q = Decimal(1).scaleb(-places)
    if d != d.quantize(q):
        return None
    return d.quantize(q)


def parse_positive(value, places: int = 2) -> Optional[Decimal]:
    d = parse_decimal(value, places)
    return d if d is not None and d > 0 else None


def round_money(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def unit_cost(total: Decimal, quantity: Decimal) -> Decimal:
    """Cost per stock unit to 4 decimals. Informational: the stored value is the typed total, not quantity x unit cost."""
    return (abs(total) / abs(quantity)).quantize(COST4, rounding=ROUND_HALF_UP)


def issue_value(quantity: Decimal, on_hand: Decimal, on_hand_value: Decimal) -> Decimal:
    """Value (positive) taken out when `quantity` leaves a stock of `on_hand` worth `on_hand_value`.

    Requires 0 < quantity <= on_hand. The whole stock takes the exact remaining value."""
    if quantity == on_hand:
        return on_hand_value
    return round_money(quantity * on_hand_value / on_hand)


def void_leaves_valid_stock(on_hand: Decimal, on_hand_value: Decimal, delta_qty: Decimal, delta_value: Decimal) -> bool:
    """True when reversing a movement keeps (item, warehouse) consistent: no negative quantity, no negative value,
    and no value left behind when the quantity reaches zero."""
    q, v = on_hand + delta_qty, on_hand_value + delta_value
    if q < 0 or v < 0:
        return False
    if q == 0 and v != 0:
        return False
    return True
