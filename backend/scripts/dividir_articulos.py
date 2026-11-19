"""Divide los artículos muy largos en trozos más chicos, sin cortar oraciones.

Ejecutar desde la carpeta backend, con el venv activo:
    python -m scripts.dividir_articulos

Por qué: un artículo de 11.000 caracteres genera UN solo embedding que
"promedia" muchos temas y ya no se parece a ninguna pregunta concreta.
Con trozos de ~2.000 caracteres, cada uno tiene un tema más definido.
"""
import re

from scripts.parsear_normativa import CARPETA, parsear

MAXIMO = 2000

# Dónde se puede cortar: en el espacio que viene después de un "." o ":"
# SOLO si lo que sigue parece inicio de oración:
#   - una mayúscula             → "...publicación. El organismo..."
#   - una letra con paréntesis  → "...publicación. b) Si no hubiere..."
#   - un número con punto       → "...siguientes: 1. Las bases..."
# Así NO se corta en "art. 24" (después del punto viene un número sin punto).
CORTE = re.compile(r"(?<=[.:]) (?=[A-ZÁÉÍÓÚÑ]|[a-zñ]\) |\d+\. )")


def dividir(texto: str, maximo: int = MAXIMO) -> list[str]:
    """Devuelve el texto en trozos de hasta `maximo` caracteres (aprox.).

    - Si el texto ya es corto, lo devuelve tal cual (una lista de 1 elemento).
    - Cada trozo, desde el segundo, empieza con el encabezado del artículo
      (su primera oración) + " (...) ", para no perder de qué se trata.
    - Una oración nunca se parte; si una sola oración supera el máximo,
      queda sola en un trozo más largo.
    """
    if len(texto) <= maximo:
        return [texto]

    oraciones = CORTE.split(texto)
    encabezado = oraciones[0]

    trozos = []
    actual = ""
    for oracion in oraciones:
        if actual and len(actual) + 1 + len(oracion) > maximo:
            trozos.append(actual)
            actual = encabezado + " (...) " + oracion
        else:
            actual = (actual + " " + oracion).strip()
    trozos.append(actual)
    return trozos


def main():
    articulos = parsear(CARPETA / "ley_19886.xml")
    art_8bis = next(a for a in articulos if a["articulo"] == "8 BIS")
    trozos = dividir(art_8bis["texto"])

    print(f"Art. 8 bis: {len(art_8bis['texto'])} caracteres → {len(trozos)} trozos\n")
    for i, trozo in enumerate(trozos, start=1):
        print(f"--- Trozo {i} ({len(trozo)} caracteres) ---")
        print(trozo[:250] + " ...\n")


if __name__ == "__main__":
    main()
