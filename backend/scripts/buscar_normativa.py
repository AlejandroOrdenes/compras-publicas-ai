"""Busca los artículos más parecidos (en significado) a una pregunta.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.buscar_normativa "¿cuándo puedo comprar sin licitar?"

Requiere haber corrido antes: python -m scripts.indexar_normativa
"""
import sys

import psycopg
import voyageai

from app.config import settings
from scripts.indexar_normativa import MODELO

CANTIDAD = 5       # artículos distintos que se devuelven
CANDIDATOS = 15    # trozos que se piden a la base antes de deduplicar

# <=> es el operador de pgvector para la "distancia coseno":
# 0 = mismo significado, 2 = opuesto. Similitud = 1 - distancia.
BUSCAR = """
SELECT norma, articulo, transitorio, texto, 1 - (embedding <=> %s::vector) AS similitud
FROM normativa
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def deduplicar(filas: list[tuple], cantidad: int) -> list[tuple]:
    """Recorre los trozos en orden y se queda con el primero (el mejor) de cada artículo.

    Cada fila empieza con (norma, articulo, transitorio, ...). El artículo se
    identifica con esos 3 datos: la ley tiene transitorios con el mismo número
    que artículos permanentes (dos "artículo 1"), y no son el mismo artículo.
    """
    vistos = set()
    elegidos = []
    for fila in filas:
        clave = fila[:3]              # (norma, articulo, transitorio)
        if clave in vistos:
            continue                  # este artículo ya está: saltar
        vistos.add(clave)
        elegidos.append(fila)
        if len(elegidos) == cantidad:
            break                     # ya tenemos los suficientes: dejar de recorrer
    return elegidos


def buscar(pregunta: str, cantidad: int = CANTIDAD) -> list[tuple]:
    """Devuelve los `cantidad` artículos distintos más parecidos a la pregunta.

    Como los artículos largos están divididos en trozos, se piden más trozos
    (CANDIDATOS) y se deja solo el mejor trozo de cada artículo. Medido con
    eval/evaluar_busqueda.py: cobertura 84% → 85% y 5 artículos distintos (antes 4,4).
    """
    vo = voyageai.Client(api_key=settings.voyage_api_key)
    vector = str(vo.embed([pregunta], model=MODELO, input_type="query").embeddings[0])
    with psycopg.connect(settings.database_url) as conn:
        filas = conn.execute(BUSCAR, (vector, vector, max(CANDIDATOS, cantidad * 3))).fetchall()
    return deduplicar(filas, cantidad)


def main():
    if len(sys.argv) < 2:
        sys.exit('Uso: python -m scripts.buscar_normativa "tu pregunta"')
    pregunta = sys.argv[1]

    print(f"Pregunta: {pregunta}\n")
    for norma, articulo, transitorio, texto, similitud in buscar(pregunta):
        marca = " (transitorio)" if transitorio else ""
        print(f"{similitud:.3f}  {norma} art. {articulo}{marca}")
        print(f"       {texto[:150]}...\n")


if __name__ == "__main__":
    main()
