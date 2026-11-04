"""Built-in cover page text. The wording is the approved Spanish template, used as provided."""

DOC_TYPES = ("quote", "invoice")

# Placeholders the backend fills in. {Logo} is part of the fixed layout, not the text.
COMMON_FIELDS = ("Contact_Name", "User_Name", "Client", "Date")
DOC_FIELD = {"quote": "No_Cotizacion", "invoice": "No_Factura"}
# 2026-11-03: the quote's Notas inside the portada letter. Quotes only, switch PORTADA_NOTE_ENABLED (default off).
NOTE_FIELD = "Note"

MAX_BODY_CHARS = 2500

DEFAULT_BODY = {
    "quote": (
        "Estimado {Client}:\n"
        "\n"
        "Gracias por confiar en Metpro. Nuestro compromiso es brindar resultados confiables, eficientes y "
        "duraderos, respaldados por un equipo profesional y una atención personalizada. Su apoyo nos ha "
        "ayudado a crecer y mejorar.\n"
        "\n"
        "Adjunta la cotización {No_Cotizacion}.\n"
        "\n"
        "Esta propuesta ha sido preparada exclusivamente para el cliente indicado, con el objetivo de "
        "presentar el alcance del trabajo, los costos estimados y las condiciones generales del servicio. "
        "Estamos disponibles para aclarar cualquier duda y ajustar la propuesta según sus necesidades.\n"
        "\n"
        "Un cordial saludo,"
    ),
    "invoice": (
        "Estimado Cliente:\n"
        "\n"
        "Adjunta la factura {No_Factura} por los productos y/o servicios proporcionados por Metpro SRL.\n"
        "Los detalles de la factura, incluido un desglose de los cargos, se incluyen para su referencia.\n"
        "\n"
        "Solicitamos amablemente que el pago se realice antes de la fecha de vencimiento, de acuerdo con "
        "los términos acordados. Si tiene alguna pregunta o necesita más aclaraciones sobre la factura, "
        "no dude en ponerse en contacto con nosotros.\n"
        "\n"
        "Gracias por seguir confiando en nosotros. Esperamos tener la oportunidad de colaborar nuevamente "
        "en el futuro.\n"
        "\n"
        "Un cordial saludo,"
    ),
}


# Text in [[ ... ]] prints only when every field inside it has a value (see cover_page.service.render_text).
QUOTE_BODY_WITH_NOTE = (
    "Estimado {Client}:\n"
    "\n"
    "Gracias por confiar en Metpro. Nuestro compromiso es brindar resultados confiables, eficientes y "
    "duraderos, respaldados por un equipo profesional y una atención personalizada. Su apoyo nos ha "
    "ayudado a crecer y mejorar.\n"
    "\n"
    "Adjunta la cotización {No_Cotizacion}[[ con las siguientes especificaciones: {Note}]]\n"
    "\n"
    "Esta propuesta ha sido preparada exclusivamente para el cliente indicado, con el objetivo de "
    "presentar el alcance del trabajo, los costos estimados y las condiciones generales del servicio. "
    "Estamos disponibles para aclarar cualquier duda y ajustar la propuesta según sus necesidades.\n"
    "\n"
    "Un cordial saludo,"
)


def portada_note_enabled() -> bool:
    from core.switches import switch_on
    return switch_on("PORTADA_NOTE_ENABLED")


def default_body(doc_type: str) -> str:
    """Built-in text. With PORTADA_NOTE_ENABLED the quote letter carries the note (2026-11-03)."""
    if doc_type == "quote" and portada_note_enabled():
        return QUOTE_BODY_WITH_NOTE
    return DEFAULT_BODY[doc_type]


def default_bodies() -> dict:
    return {t: default_body(t) for t in DOC_TYPES}


def allowed_fields(doc_type: str):
    fields = COMMON_FIELDS + (DOC_FIELD[doc_type],)
    if doc_type == "quote" and portada_note_enabled():
        fields += (NOTE_FIELD,)
    return fields
