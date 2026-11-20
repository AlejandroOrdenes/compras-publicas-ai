"""Busca los artículos más parecidos (en significado) a una pregunta.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.buscar_normativa "¿cuándo puedo comprar sin licitar?"

Requiere haber corrido antes: python -m scripts.indexar_normativa

Pasos de buscar():
    1. reescribir  → Haiku traduce la pregunta al vocabulario legal (opcional)
    2. vectorizar  → Voyage convierte original + reescrita en vectores (1 llamada)
    3. candidatos  → pgvector trae los 15 trozos más parecidos a cada vector
    4. fusionar    → mezcla los rankings con RRF
    5. deduplicar  → deja el mejor trozo de cada artículo, hasta 5 artículos
    6. completar   → cambia cada trozo por su artículo entero (para Claude)
Medido con eval/evaluar_busqueda.py (cobertura): 85% sin reescribir → 87% con.
"""
import sys

import psycopg
import voyageai
from langsmith import traceable

from app.config import settings
from scripts.indexar_normativa import MODELO
from scripts.reescribir_pregunta import reescribir as reescribir_con_haiku

CANTIDAD = 5       # artículos distintos que se devuelven
CANDIDATOS = 15    # trozos que se piden a la base por cada búsqueda
RRF_K = 60         # constante estándar de RRF (suaviza la diferencia entre puestos)

# <=> es el operador de pgvector para la "distancia coseno":
# 0 = mismo significado, 2 = opuesto. Similitud = 1 - distancia.
# El nombre de la tabla va con .format() porque %s solo sirve para VALORES;
# solo se usa con nombres fijos escritos en el código, nunca con texto del usuario.
BUSCAR = """
SELECT norma, articulo, transitorio, texto, 1 - (embedding <=> %s::vector) AS similitud
FROM {tabla}
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


# Para LangSmith: una fila de la base → algo legible (sin el texto completo del artículo).
def _filas_legibles(filas: list[tuple]) -> dict:
    return {"articulos": [f"{f[0]} art. {f[1]}{' (transitorio)' if f[2] else ''} | similitud {f[4]:.3f}"
                          for f in filas]}


# process_outputs: en vez de guardar 1.024 números por vector (ilegible), guarda solo cuántos hubo.
@traceable(name="vectorizar", run_type="embedding",
           process_outputs=lambda vectores: {"vectores": len(vectores)})
def vectorizar(textos: list[str]) -> list[str]:
    """Convierte textos en vectores con UNA sola llamada a Voyage."""
    vo = voyageai.Client(api_key=settings.voyage_api_key)
    return [str(v) for v in vo.embed(textos, model=MODELO, input_type="query").embeddings]


def candidatos(conn, vector: str, cantidad: int = CANDIDATOS, tabla: str = "normativa") -> list[tuple]:
    """Los `cantidad` trozos más parecidos a un vector, del más al menos parecido."""
    return conn.execute(BUSCAR.format(tabla=tabla), (vector, vector, cantidad)).fetchall()


def fusionar(rankings: list[list[tuple]]) -> list[tuple]:
    """Mezcla varios rankings con RRF (Reciprocal Rank Fusion).

    Cada trozo suma 1 / (RRF_K + puesto) por cada ranking en que aparece:
    estar arriba suma más, y aparecer en los dos rankings suma doble.
    Con un solo ranking, devuelve el mismo orden.
    """
    puntaje = {}
    fila_de = {}
    for ranking in rankings:
        for puesto, fila in enumerate(ranking, start=1):
            clave = fila[:4]                  # (norma, articulo, transitorio, texto) = un trozo
            puntaje[clave] = puntaje.get(clave, 0) + 1 / (RRF_K + puesto)
            fila_de.setdefault(clave, fila)   # guarda la primera fila vista de ese trozo
    ordenadas = sorted(puntaje, key=puntaje.get, reverse=True)
    return [fila_de[clave] for clave in ordenadas]


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


# Rearma el artículo completo uniendo sus trozos en orden. Desde el 2º trozo,
# cada uno empieza con "encabezado (...) ": se corta hasta después de " (...) "
# (7 caracteres) para no repetir el encabezado. Verificado: los 325 artículos
# quedan idénticos al texto original del XML.
ARTICULO_COMPLETO = """
SELECT string_agg(
         CASE WHEN parte = 1 THEN texto
              ELSE substr(texto, strpos(texto, ' (...) ') + 7) END,
         ' ' ORDER BY parte)
FROM normativa
WHERE norma = %s AND articulo = %s AND transitorio = %s
"""


def completar(conn, filas: list[tuple]) -> list[tuple]:
    """Reemplaza el texto de cada trozo por el de su artículo completo ("small-to-big").

    Se BUSCA con trozos chicos (más precisos), pero a Claude se le ENTREGA el
    artículo entero: así no recibe listas cortadas por la mitad.
    """
    completas = []
    for norma, articulo, transitorio, _trozo, similitud in filas:
        texto = conn.execute(ARTICULO_COMPLETO, (norma, articulo, transitorio)).fetchone()[0]
        completas.append((norma, articulo, transitorio, texto, similitud))
    return completas


@traceable(name="buscar", run_type="retriever", process_outputs=_filas_legibles)
def buscar(pregunta: str, cantidad: int = CANTIDAD, reescribir: bool = True,
           completo: bool = True) -> list[tuple]:
    """Devuelve los `cantidad` artículos distintos más relevantes para la pregunta.

    completo=True → cada artículo viene entero; False → solo su mejor trozo.
    """
    textos = [pregunta]
    if reescribir:
        textos.append(reescribir_con_haiku(pregunta))
    vectores = vectorizar(textos)

    pedir = max(CANDIDATOS, cantidad * 3)
    with psycopg.connect(settings.database_url) as conn:
        rankings = [candidatos(conn, v, pedir) for v in vectores]
        elegidos = deduplicar(fusionar(rankings), cantidad)
        return completar(conn, elegidos) if completo else elegidos


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
