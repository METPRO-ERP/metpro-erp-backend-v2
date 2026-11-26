"""Phased invoicing rules. Plan v2.1 section 3. Pure helpers plus the switch.

Switch: PHASED_INVOICING_ENABLED, default off. It only counts when FISCAL_NCF_ENABLED is also on
(every phase invoice carries an NCF), so phased_enabled() checks both.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from core.fiscal_ncf import fiscal_ncf_enabled
from core.switches import switch_on

CENT = Decimal("0.01")
PRESET_LABELS = ("Pago inicial / depósito", "Segundo pago", "Pago final")


def phased_enabled() -> bool:
    return switch_on("PHASED_INVOICING_ENABLED") and fiscal_ncf_enabled()


def d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def money(value) -> Decimal:
    return d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def amount_from_percent(percent, quote_total) -> Decimal:
    """A % entry becomes a tax-included amount at entry, rounded to cents, of the quote grand total."""
    return money(d(percent) / Decimal(100) * d(quote_total))


def phase_itbis(amount, quote_total, quote_itbis, prior_itbis: Iterable, is_final: bool) -> Decimal:
    """F1 pro-rata, specification of section 3.4.

    Non-final: share of the quote ITBIS by the phase's share of the quote total, rounded to cents,
    never above the ITBIS still unassigned. Final: the exact remainder, so the phase ITBIS values
    sum to the quote ITBIS.
    """
    unassigned = money(d(quote_itbis) - sum((d(p) for p in prior_itbis), Decimal(0)))
    if is_final:
        return unassigned
    share = d(amount) / d(quote_total) if d(quote_total) else Decimal(0)
    return min(money(share * d(quote_itbis)), unassigned)
