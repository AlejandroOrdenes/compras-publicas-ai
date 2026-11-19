"""Mide qué tan bien encuentra artículos el buscador, con la pauta de preguntas.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m eval.evaluar_busqueda

Compara dos índices con las MISMAS preguntas:
    normativa_v1 → un artículo = una fila
    normativa    → v2, artículos largos divididos en trozos

Métricas (sobre los 5 primeros resultados de cada pregunta):
    acierto   → ¿llegó al menos UNO de los artículos esperados? (sí/no)
    cobertura → de los artículos esperados, ¿qué fracción llegó? (0 a 1)
Solo usa Voyage (para convertir las preguntas en vectores), no Claude.
"""
import json
from pathlib import Path

import psycopg
import voyageai

from app.config import settings
from scripts.buscar_normativa import CANDIDATOS, CANTIDAD, deduplicar
from scripts.indexar_normativa import MODELO

PAUTA = Path(__file__).parent / "preguntas.json"

# Cada variante: nombre → (tabla, ¿deduplicar?)
INDICES = {
    "v1": ("normativa_v1", False),
    "v2": ("normativa", False),
    "v2 dedup": ("normativa", True),
}

# El nombre de la tabla no puede ir como parámetro %s (los parámetros son
# solo para VALORES), por eso se arma con .format() desde INDICES, que es una
# lista fija escrita por nosotros (nunca texto que venga de un usuario).
BUSCAR = """
SELECT norma, articulo, transitorio
FROM {tabla}
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def vectorizar(preguntas: list[str]) -> list[str]:
    """Convierte todas las preguntas en vectores (una sola llamada a Voyage)."""
    vo = voyageai.Client(api_key=settings.voyage_api_key)
    vectores = vo.embed(preguntas, model=MODELO, input_type="query").embeddings
    return [str(v) for v in vectores]


def evaluar(conn, tabla: str, dedup: bool, pauta: list[dict], vectores: list[str]) -> list[dict]:
    """Hace cada pregunta al índice y anota acierto y cobertura."""
    resultados = []
    for item, vector in zip(pauta, vectores):
        pedir = CANDIDATOS if dedup else CANTIDAD
        filas = conn.execute(BUSCAR.format(tabla=tabla), (vector, pedir)).fetchall()
        if dedup:
            filas = deduplicar(filas, CANTIDAD)
        # Los esperados son todos artículos permanentes: un transitorio con el
        # mismo número (p. ej. "1") no debe contar como acierto.
        encontrados = {(norma, articulo) for norma, articulo, transitorio in filas
                       if not transitorio}
        esperados = {tuple(e) for e in item["esperados"]}
        llegaron = esperados & encontrados   # intersección: los que están en ambos
        resultados.append({
            "id": item["id"],
            "estilo": item["estilo"],
            "acierto": len(llegaron) > 0,
            "cobertura": len(llegaron) / len(esperados),
            "distintos": len(encontrados),   # artículos distintos en el top 5
        })
    return resultados


def resumen(resultados: list[dict]) -> str:
    n = len(resultados)
    aciertos = sum(r["acierto"] for r in resultados)
    cobertura = sum(r["cobertura"] for r in resultados) / n
    distintos = sum(r["distintos"] for r in resultados) / n
    return (f"acierto {aciertos:>2}/{n} ({aciertos / n:.0%})  "
            f"cobertura {cobertura:.0%}  artículos distintos {distintos:.1f}/{CANTIDAD}")


def main():
    pauta = json.loads(PAUTA.read_text(encoding="utf-8"))
    vectores = vectorizar([item["pregunta"] for item in pauta])

    todos = {}
    with psycopg.connect(settings.database_url) as conn:
        for nombre, (tabla, dedup) in INDICES.items():
            todos[nombre] = evaluar(conn, tabla, dedup, pauta, vectores)

    # Detalle: una fila por pregunta, ✔/✘ para cada índice.
    print(f"{'#':>3}  {'estilo':<10}" + "".join(f"{n:>10}" for n in INDICES) + "  pregunta")
    for i, item in enumerate(pauta):
        marcas = "".join(
            f"{('✔' if todos[n][i]['acierto'] else '✘') + ' ' + format(todos[n][i]['cobertura'], '.0%'):>10}"
            for n in INDICES
        )
        print(f"{item['id']:>3}  {item['estilo']:<10}{marcas}  {item['pregunta'][:60]}")

    # Resumen: total y por estilo de pregunta.
    for nombre in INDICES:
        print(f"\n{nombre}")
        print(f"  total      {resumen(todos[nombre])}")
        for estilo in ("legal", "cotidiano"):
            grupo = [r for r in todos[nombre] if r["estilo"] == estilo]
            print(f"  {estilo:<10} {resumen(grupo)}")


if __name__ == "__main__":
    main()
