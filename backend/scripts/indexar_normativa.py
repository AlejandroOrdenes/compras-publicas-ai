"""Genera los embeddings de cada artículo y los guarda en Postgres (pgvector).

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.indexar_normativa

Requiere haber corrido antes: python -m scripts.descargar_normativa
Se puede volver a ejecutar: borra y vuelve a crear la tabla.

v2: los artículos largos se dividen en trozos (scripts/dividir_articulos.py).
Cada trozo es una fila; la columna `parte` dice qué trozo es (1, 2, 3...).
La versión anterior (un artículo = una fila) quedó guardada en `normativa_v1`.
"""
import time

import psycopg
import voyageai

from app.config import settings
from scripts.dividir_articulos import dividir
from scripts.parsear_normativa import CARPETA, parsear

MODELO = "voyage-4"
DIMENSIONES = 1024   # largo de cada embedding de voyage-4 (valor por defecto)
TAMANO_LOTE = 10     # fichas por llamada a Voyage
MAX_INTENTOS = 5

CREAR_TABLA = f"""
CREATE TABLE normativa (
    id          SERIAL PRIMARY KEY,
    norma       TEXT NOT NULL,
    articulo    TEXT NOT NULL,
    parte       INTEGER NOT NULL,
    transitorio BOOLEAN NOT NULL,
    texto       TEXT NOT NULL,
    embedding   VECTOR({DIMENSIONES}) NOT NULL
)
"""


def embeber_lote(vo: voyageai.Client, textos: list[str]) -> list[list[float]]:
    """Pide a Voyage los embeddings de un lote. Si Voyage pide esperar, espera y reintenta."""
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            return vo.embed(textos, model=MODELO, input_type="document").embeddings
        except voyageai.error.RateLimitError:
            if intento == MAX_INTENTOS:
                raise
            print(f"  Voyage pidió esperar (límite por minuto). Reintento {intento} en 60 s...")
            time.sleep(60)


def main():
    articulos = []
    for ruta in sorted(CARPETA.glob("*.xml")):
        articulos += parsear(ruta)

    # Cada artículo se convierte en 1 o más fichas (una por trozo).
    fichas = []
    for art in articulos:
        for numero, trozo in enumerate(dividir(art["texto"]), start=1):
            fichas.append({**art, "texto": trozo, "parte": numero})
    print(f"{len(articulos)} artículos → {len(fichas)} fichas para indexar")

    # 1) Calcular TODOS los embeddings antes de tocar la base: si Voyage falla a
    #    mitad de camino, la tabla queda como estaba (no a medio llenar).
    vo = voyageai.Client(api_key=settings.voyage_api_key)
    embeddings = []
    for inicio in range(0, len(fichas), TAMANO_LOTE):
        lote = fichas[inicio:inicio + TAMANO_LOTE]
        embeddings += embeber_lote(vo, [f["texto"] for f in lote])
        print(f"  embeddings: {len(embeddings)}/{len(fichas)}")

    # 2) Guardar todo en una sola transacción: o entra completo, o no entra nada.
    #    Se borra la tabla entera (no solo las filas) porque cambió su forma.
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("DROP TABLE IF EXISTS normativa")
        conn.execute(CREAR_TABLA)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO normativa (norma, articulo, parte, transitorio, texto, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s::vector)",
                [
                    (f["norma"], f["articulo"], f["parte"], f["transitorio"], f["texto"], str(e))
                    for f, e in zip(fichas, embeddings)
                ],
            )
        total = conn.execute("SELECT count(*) FROM normativa").fetchone()[0]

    print(f"OK  {total} fichas guardadas en la tabla normativa")


if __name__ == "__main__":
    main()
