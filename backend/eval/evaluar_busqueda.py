"""Mide qué tan bien encuentra artículos el buscador, con la pauta de preguntas.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m eval.evaluar_busqueda

Compara variantes del buscador con las MISMAS preguntas:
    v1                → un artículo = una fila (tabla normativa_v1)
    v2                → artículos largos divididos en trozos
    v2 dedup          → + mejor trozo por artículo
    v2 dedup + reesc. → + búsqueda con la pregunta reescrita por Haiku (lo que usa el asistente)

Métricas (sobre los 5 artículos que recibiría Claude):
    acierto   → ¿llegó al menos UNO de los artículos esperados? (sí/no)
    cobertura → de los artículos esperados, ¿qué fracción llegó? (0 a 1)

Costo por corrida: 2 llamadas a Voyage (~1.000 tokens, capa gratis) y
36 llamadas a Haiku (~US$0,002). Usa las mismas funciones que buscar_normativa.py.
"""
import json
from pathlib import Path

import psycopg

from app.config import settings
from scripts.buscar_normativa import (
    CANDIDATOS,
    CANTIDAD,
    candidatos,
    deduplicar,
    fusionar,
    vectorizar,
)
from scripts.reescribir_pregunta import reescribir

PAUTA = Path(__file__).parent / "preguntas.json"

# Cada variante: nombre → (tabla, ¿deduplicar?, ¿reescribir?)
VARIANTES = {
    "v1": ("normativa_v1", False, False),
    "v2": ("normativa", False, False),
    "v2 dedup": ("normativa", True, False),
    "+ reesc.": ("normativa", True, True),
}


def evaluar(conn, tabla, dedup, vectores_originales, vectores_reescritos, pauta) -> list[dict]:
    """Hace cada pregunta al índice y anota acierto y cobertura."""
    resultados = []
    for i, item in enumerate(pauta):
        pedir = CANDIDATOS if dedup else CANTIDAD
        rankings = [candidatos(conn, vectores_originales[i], pedir, tabla)]
        if vectores_reescritos:
            rankings.append(candidatos(conn, vectores_reescritos[i], pedir, tabla))
        filas = fusionar(rankings)
        filas = deduplicar(filas, CANTIDAD) if dedup else filas[:CANTIDAD]

        # Los esperados son todos artículos permanentes: un transitorio con el
        # mismo número (p. ej. "1") no debe contar como acierto.
        encontrados = {(f[0], f[1]) for f in filas if not f[2]}
        esperados = {tuple(e) for e in item["esperados"]}
        llegaron = esperados & encontrados   # intersección: los que están en ambos
        resultados.append({
            "id": item["id"],
            "estilo": item["estilo"],
            "acierto": len(llegaron) > 0,
            "cobertura": len(llegaron) / len(esperados),
            "distintos": len({f[:3] for f in filas}),   # artículos distintos entre los 5
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
    preguntas = [item["pregunta"] for item in pauta]

    print("Reescribiendo las preguntas con Haiku...")
    reescritas = [reescribir(p) for p in preguntas]

    # 2 llamadas a Voyage en total (una por lista): cuida el límite por minuto.
    vectores_originales = vectorizar(preguntas)
    vectores_reescritos = vectorizar(reescritas)

    todos = {}
    with psycopg.connect(settings.database_url) as conn:
        for nombre, (tabla, dedup, con_reescritura) in VARIANTES.items():
            todos[nombre] = evaluar(conn, tabla, dedup, vectores_originales,
                                    vectores_reescritos if con_reescritura else None, pauta)

    # Detalle: una fila por pregunta, ✔/✘ y cobertura para cada variante.
    print(f"\n{'#':>3}  {'estilo':<10}" + "".join(f"{n:>10}" for n in VARIANTES) + "  pregunta")
    for i, item in enumerate(pauta):
        marcas = "".join(
            f"{('✔' if todos[n][i]['acierto'] else '✘') + ' ' + format(todos[n][i]['cobertura'], '.0%'):>10}"
            for n in VARIANTES
        )
        print(f"{item['id']:>3}  {item['estilo']:<10}{marcas}  {item['pregunta'][:60]}")

    # Resumen: total y por estilo de pregunta.
    for nombre in VARIANTES:
        print(f"\n{nombre}")
        print(f"  total      {resumen(todos[nombre])}")
        for estilo in ("legal", "cotidiano"):
            grupo = [r for r in todos[nombre] if r["estilo"] == estilo]
            print(f"  {estilo:<10} {resumen(grupo)}")


if __name__ == "__main__":
    main()
