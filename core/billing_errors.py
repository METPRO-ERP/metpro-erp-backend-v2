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
    # Payment receipt (2026-11-27, RECEIPT_ENABLED)
    "RECEIPT_PAYMENT_NOT_FOUND": (404, "Pago no encontrado"),
    "RECEIPT_PAYMENT_VOIDED": (409, "El pago está anulado: no se emite recibo"),
    "RECEIPT_NO_SNAPSHOT": (409, "Este pago se registró antes del recibo de pago y no tiene saldos guardados: no se emite recibo"),
    # Inventory stock ledger (2026-11-28, INVENTORY_ENABLED)
    "INVENTORY_NO_LINES": (400, "Indique al menos una línea"),
    "INVENTORY_QUANTITY_INVALID": (400, "La cantidad debe ser mayor que cero y tener máximo 2 decimales"),
    "INVENTORY_ADJUST_QUANTITY_INVALID": (400, "La cantidad del ajuste no puede ser cero y debe tener máximo 2 decimales"),
    "INVENTORY_COST_INVALID": (400, "El costo total debe ser mayor que cero y tener máximo 2 decimales"),
    "INVENTORY_COST_REQUIRED": (400, "El costo total es obligatorio en esta línea"),
    "INVENTORY_COST_NOT_ALLOWED": (400, "Una salida o merma se valora al costo promedio: no indique costo"),
    "INVENTORY_DATE_INVALID": (400, "La fecha no puede ser futura"),
    "INVENTORY_REASON_REQUIRED": (400, "Indique el motivo"),
    "INVENTORY_LOT_REQUIRED": (400, "Este artículo se controla por lote: indique la referencia del lote y el peso en libras"),
    "INVENTORY_LOT_NOT_ALLOWED": (400, "Este artículo no se controla por lote: no indique lote ni peso"),
    "INVENTORY_LOT_EXISTS": (409, "Ya existe un lote con esa referencia para este artículo"),
    "INVENTORY_DUPLICATE_ROW": (400, "Un artículo está repetido en la carga inicial"),
    "INVENTORY_ITEM_NOT_FOUND": (404, "Artículo de inventario no encontrado"),
    "INVENTORY_ITEM_INACTIVE": (409, "El artículo está inactivo"),
    "INVENTORY_ITEM_HAS_STOCK": (409, "El artículo tiene existencia: no se puede desactivar"),
    "INVENTORY_SKU_EXISTS": (409, "Ya existe un artículo con ese código"),
    "INVENTORY_PRODUCT_NOT_FOUND": (404, "Producto no encontrado"),
    "INVENTORY_WAREHOUSE_NOT_FOUND": (404, "Almacén no encontrado o inactivo"),
    "INVENTORY_WAREHOUSE_REQUIRED": (400, "Indique el almacén"),
    "INVENTORY_QUOTE_NOT_FOUND": (404, "Cotización no encontrada"),
    "INVENTORY_PROJECT_NOT_FOUND": (404, "Proyecto no encontrado"),
    "INVENTORY_SUPPLIER_NOT_FOUND": (404, "Suplidor no encontrado"),
    "INVENTORY_INSUFFICIENT_STOCK": (409, "No hay existencia suficiente"),
    "INVENTORY_MOVEMENT_NOT_FOUND": (404, "Movimiento no encontrado"),
    "INVENTORY_ALREADY_VOID": (409, "El movimiento ya está anulado"),
    "INVENTORY_NOT_VOIDABLE": (409, "Este movimiento no se puede anular. Corrija con un ajuste"),
    "INVENTORY_VOID_BREAKS_STOCK": (409, "Anular este movimiento dejaría la existencia o su valor en un estado inválido. Corrija con un ajuste"),
    "INVENTORY_OPENING_EXISTS": (409, "La carga inicial de este almacén ya se registró y está cerrada. Corrija con ajustes"),
    "INVENTORY_OPENING_DUMMY": (400, "Un artículo de prueba no puede entrar en la carga inicial"),
    "INVENTORY_OPENING_MISMATCH": (409, "El código ya existe con otro nombre, tipo, unidad o control de lote"),
    "INVENTORY_LENGTH_INVALID": (400, "La longitud estándar debe ser mayor que cero y tener máximo 2 decimales"),
    "INVENTORY_ITEM_FIELDS_REQUIRED": (400, "El código y el nombre son obligatorios"),
    "INVENTORY_QUOTE_HAS_ISSUES": (409, "Esta cotización tiene material despachado desde inventario y no se puede eliminar"),
}


def billing_error(code: str):
    status, message = ERRORS[code]
    return api_error(status, message, code)
