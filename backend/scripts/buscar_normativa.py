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

CANTIDAD = 5

# <=> es el operador de pgvector para la "distancia coseno":
# 0 = mismo significado, 2 = opuesto. Similitud = 1 - distancia.
BUSCAR = """
SELECT norma, articulo, transitorio, texto, 1 - (embedding <=> %s::vector) AS similitud
FROM normativa
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def buscar(pregunta: str, cantidad: int = CANTIDAD) -> list[tuple]:
    vo = voyageai.Client(api_key=settings.voyage_api_key)
    vector = str(vo.embed([pregunta], model=MODELO, input_type="query").embeddings[0])
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute(BUSCAR, (vector, vector, cantidad)).fetchall()


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
