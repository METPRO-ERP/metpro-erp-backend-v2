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
    # Conduce delivery ledger (2026-11-26)
    "CONDUCE_NOT_FOUND": (404, "Conduce no encontrado. Cree un conduce primero"),
    "CONDUCE_NO_LINES": (400, "Indique al menos una línea con cantidad a entregar"),
    "CONDUCE_LINE_NOT_DELIVERABLE": (400, "Una de las líneas no es un item entregable de esta cotización"),
    "CONDUCE_DUPLICATE_LINE": (400, "Una línea está repetida"),
    "CONDUCE_QUANTITY_INVALID": (400, "La cantidad debe ser mayor que cero y tener máximo 2 decimales"),
    "CONDUCE_DATE_INVALID": (400, "La fecha de entrega no puede ser futura"),
    "CONDUCE_INVOICE_CANCELLED": (409, "La factura está anulada: no se puede crear un conduce"),
    "CONDUCE_NO_DELIVERABLE_LINES": (409, "La cotización no tiene items entregables"),
    "CONDUCE_OVER_DELIVERY": (409, "La cantidad excede lo pendiente de entregar"),
    "CONDUCE_ALREADY_VOID": (409, "El conduce ya está anulado"),
    "CONDUCE_VOID_REASON_REQUIRED": (400, "Indique el motivo de la anulación"),
    "INVOICE_HAS_DELIVERIES": (409, "La factura tiene conduces de entrega registrados y no se puede eliminar"),
    # Payment terms days and invoice due date (2026-11-26, PAYMENT_DUE_ENABLED)
    "DUE_DATE_NOTE_REQUIRED": (400, "Indique el motivo del cambio de la fecha de vencimiento"),
    "DUE_DATE_UNCHANGED": (400, "La fecha de vencimiento es la misma que ya tiene la factura"),
    "DUE_DATE_BEFORE_INVOICE": (400, "La fecha de vencimiento no puede ser anterior a la fecha de la factura"),
    "INVOICE_DUE_DATE_LOCKED": (409, "La fecha de vencimiento no se puede cambiar en una factura pagada o anulada"),
    "INVOICE_HAS_DUE_DATE_AUDIT": (409, "La factura tiene cambios de fecha de vencimiento registrados y no se puede eliminar"),
}


def billing_error(code: str):
    status, message = ERRORS[code]
    return api_error(status, message, code)
