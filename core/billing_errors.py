"""Error codes added by the NCF and phased-invoicing release (plan v2.1).

Kept in one place so the Spanish messages and codes are stable and testable
(tests/test_ncf_phases_errors.py checks every entry).
"""
from core.errors import api_error

ERRORS = {
    "INVOICE_HAS_NCF": (400, "Esta factura tiene NCF. Use Anular en lugar de Eliminar"),
    "BILLING_MODE_INVALID": (400, "Modo de facturación no válido"),
    "FEATURE_DISABLED": (404, "No disponible"),
    "BILLING_MODE_LOCKED": (400, "El modo de facturación no puede cambiar después de la primera fase"),
    "BILLING_MODE_NOT_PHASED": (400, "La cotización no está en modo de facturación por fases"),
    "PHASE_NOT_FOUND": (404, "Fase no encontrada"),
    "PHASE_ALREADY_INVOICED": (400, "Esta fase ya tiene factura"),
    "QUOTE_TOTAL_LOCKED_PHASED": (400, "No se puede cambiar el total: ya existe una factura por fases"),
}


def billing_error(code: str):
    status, message = ERRORS[code]
    return api_error(status, message, code)
