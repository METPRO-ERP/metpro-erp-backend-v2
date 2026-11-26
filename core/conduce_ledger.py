"""Conduce delivery ledger rules. Plan: claude/conduce-module-plan-2026-11-26.md.

Switch: CONDUCE_LEDGER_ENABLED, default off. Off = the conduce behaves exactly as before (one row per invoice created
by viewing it, lines from the invoice's stored items). On = explicit, numbered, dated deliveries against the quote.
"""
from decimal import Decimal, InvalidOperation
from typing import Optional

from core.switches import switch_on

# Physical lines only. Services (installation, labor) are not deliveries. A missing item_type counts as a product.
DELIVERABLE_TYPES = ("product", "custom")
CENT = Decimal("0.01")


def ledger_enabled() -> bool:
    return switch_on("CONDUCE_LEDGER_ENABLED")


def is_deliverable(item: dict) -> bool:
    """Deliverable = physical type and printable (a hidden line is never on a customer document)."""
    from core.visibility import is_printable
    kind = (item.get("item_type") or "product").strip().lower()
    return kind in DELIVERABLE_TYPES and is_printable(item)


def parse_quantity(value) -> Optional[Decimal]:
    """A positive quantity with at most 2 decimals, or None when invalid."""
    try:
        q = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not q.is_finite() or q <= 0 or q != q.quantize(CENT):
        return None
    return q.quantize(CENT)


def conduce_number(quote: dict, seq: int) -> str:
    from core.numbering import quote_label_for
    return f"C-{quote_label_for(quote)[2:]}-E{seq}"


def state_of(lines: list) -> str:
    """Sin entregas / Entrega parcial / Entregado from per-line dicts with quoted and delivered."""
    if not lines:
        return "Sin items entregables"
    delivered = [Decimal(str(l["delivered"])) for l in lines]
    if all(d == 0 for d in delivered):
        return "Sin entregas"
    if all(Decimal(str(l["delivered"])) >= Decimal(str(l["quoted"])) for l in lines):
        return "Entregado"
    return "Entrega parcial"
