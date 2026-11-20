"""Filtro de datos personales para TEXTOS LIBRES (descripciones de licitaciones, ítems).

Complementa la lista blanca del worker: los campos estructurados con datos personales
ni siquiera se copian; pero en los textos que escribe un funcionario puede aparecer
cualquier cosa ("consultas al Sr. Juan Pérez, fono 9 1234 5678").

limpiar(texto) reemplaza cada dato por una etiqueta, para que el texto siga teniendo sentido:
    "Sr. Juan Pérez, fono +56 9 1234 5678" → "Sr. [NOMBRE], fono [TELÉFONO]"

Límite conocido: un nombre SIN título ("contactar a Juan Pérez") no se detecta; una regex
no distingue "Juan Pérez" de "Puerto Montt". Para eso haría falta NER (Presidio, spaCy).
"""
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# RUT chileno: 1-2 dígitos, (punto) 3 dígitos, (punto) 3 dígitos, guion, dígito verificador.
# (?![\w-]): no puede seguir un guion → "1234567-8-PC26" es un código de proyecto, no un RUT
# (falso positivo encontrado en datos reales, licitación 1057489-268-LE26).
RUT = re.compile(r"(?<![\w.-])\d{1,2}\.?\d{3}\.?\d{3}-[\dkK](?![\w-])")

# Teléfono chileno = 9 dígitos que empiezan con 2-9 (opcionalmente con +56 adelante),
# agrupados de cualquier forma: "9 1234 5678", "22 345 6789", "(32) 234 5678", "912345678".
#   (?:[ ()-]{0,2}\d){8}  → 8 dígitos más, cada uno con hasta 2 separadores antes
# Separadores: espacio, guion, paréntesis; NUNCA punto (los montos usan puntos: "$912.345.678").
# Bordes: (?<![\w$.-]) no puede empezar pegado a letras, dígitos, $, punto o guion;
#         (?![\w-])    no puede terminar pegado a letras, dígitos o guion
#         (así "1057390-21-LR26", un código de licitación, no se confunde con teléfono).
TELEFONO = re.compile(r"(?<![\w$.-])(?:\+?56[ -]?)?\(?[2-9](?:[ ()-]{0,2}\d){8}(?![\w-])")

# Nombre después de un título de trato: "Sr. Juan Pérez", "Doña María José Soto".
# Se conserva el título y se tapan 1 a 3 palabras que empiezan con mayúscula.
NOMBRE = re.compile(
    r"\b(Sr\.?|Sra\.?|Srta\.?|Don|Doña)\s+"
    r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,2}"
)

# El orden importa: el email primero (podría contener números que parezcan teléfono)
# y el RUT antes que el teléfono (un RUT sin puntos tiene 8-9 dígitos).
REGLAS = [
    (EMAIL, "[EMAIL]"),
    (RUT, "[RUT]"),
    (TELEFONO, "[TELÉFONO]"),
    (NOMBRE, r"\1 [NOMBRE]"),     # \1 = el título que se encontró (Sr., Doña...)
]


def limpiar(texto: str | None) -> str | None:
    """Reemplaza emails, RUTs, teléfonos y nombres con título por etiquetas."""
    if not texto:
        return texto
    for patron, etiqueta in REGLAS:
        texto = patron.sub(etiqueta, texto)
    return texto


# ---------- Proveedor adjudicado: empresa (público) o persona natural (protegido) ----------

# Regla práctica chilena: el RUT de las personas se asigna por orden de nacimiento
# (hoy ~20-30 millones); el de las empresas (personas jurídicas) va sobre 50 millones.
# Caso borde aceptado: una EIRL tiene RUT de empresa y su nombre suele ser el de una
# persona ("Juan Pérez EIRL"), pero ese nombre ya es público como razón social.
RUT_MINIMO_EMPRESA = 50_000_000
PERSONA_NATURAL = "Persona natural (dato protegido)"


def proveedor_publico(rut: str | None, nombre: str | None) -> dict:
    """Devuelve {"proveedor", "rut_proveedor"}: completos si es empresa, protegidos si es persona."""
    numero = re.sub(r"\D", "", (rut or "").split("-")[0])   # "76.123.456-7" → "76123456"
    if not numero:
        return {"proveedor": None, "rut_proveedor": None}     # ítem aún no adjudicado
    if int(numero) >= RUT_MINIMO_EMPRESA:
        return {"proveedor": nombre, "rut_proveedor": rut}
    return {"proveedor": PERSONA_NATURAL, "rut_proveedor": None}


def contar(texto: str | None) -> dict[str, int]:
    """Cuántos datos de cada tipo hay en un texto (para verificar sin imprimirlos)."""
    texto = texto or ""
    return {etiqueta: len(patron.findall(texto)) for patron, etiqueta in REGLAS}
