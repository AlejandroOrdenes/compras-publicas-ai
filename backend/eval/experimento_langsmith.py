"""La misma evaluación de respuestas, pero registrada en LangSmith como "Experiment".

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m eval.experimento_langsmith 12 17 33     ← solo esas preguntas (por id)
    python -m eval.experimento_langsmith              ← las 36

CUESTA DINERO (~US$0,06 por pregunta): Opus responde y Sonnet corrige.

Diferencia con evaluar_respuestas.py:
  - evaluar_respuestas.py → sin framework; guarda el detalle en un JSON en tu disco (permanente).
  - este archivo          → los resultados quedan en LangSmith: cada respuesta con su traza
                            y sus notas, y se pueden comparar experimentos lado a lado
                            (ej. prompt v1 vs v2). El plan gratis borra las trazas a los 14 días.
Las revisiones son LAS MISMAS: se reutilizan leer_citas, normalizar y juzgar.

Conceptos de LangSmith:
  - Dataset    = las preguntas con su pauta, guardadas en LangSmith (se suben una sola vez).
  - Experiment = una corrida del asistente sobre el dataset, con notas para cada respuesta.
  - Evaluator  = una función que recibe la pregunta, la respuesta y la pauta, y devuelve notas.
"""
import json
import sys

import anthropic
from langsmith import Client

from app.config import settings
from eval.evaluar_respuestas import PAUTA, juzgar, leer_citas, normalizar
from scripts.preguntar import MODELO as MODELO_RESPUESTA
from scripts.preguntar import responder

DATASET = "compras-publicas-36"
VERSION_PROMPT = "v2"   # cambiar a mano al probar otras instrucciones, para reconocer el experimento

ls = Client()
claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)


# ---------- 1. Dataset: subir preguntas.json una sola vez ----------

def asegurar_dataset() -> None:
    if ls.has_dataset(dataset_name=DATASET):
        return
    pauta = json.loads(PAUTA.read_text(encoding="utf-8"))
    ls.create_dataset(DATASET, description="36 preguntas sobre Ley 19.886 y Reglamento, con pauta")
    ls.create_examples(dataset_name=DATASET, examples=[
        {
            "inputs": {"pregunta": p["pregunta"]},                  # lo que recibe el asistente
            "outputs": {"esperados": p["esperados"],                # la pauta (respuesta de referencia)
                        "puntos_clave": p["puntos_clave"]},
            "metadata": {"id": p["id"], "estilo": p["estilo"]},     # para filtrar en el panel
        }
        for p in pauta if "puntos_clave" in p
    ])
    print(f"Dataset '{DATASET}' creado en LangSmith.")


# ---------- 2. Lo que se evalúa: el asistente ----------

def asistente(inputs: dict) -> dict:
    """LangSmith llama a esta función con cada pregunta del dataset."""
    r = responder(inputs["pregunta"])
    # Las filas son tuplas; se pasan a listas porque LangSmith guarda JSON.
    return {"texto": r["texto"], "articulos": [list(fila) for fila in r["articulos"]]}


# ---------- 3. Evaluators: las notas ----------
# LangSmith mira los NOMBRES de los parámetros (inputs, outputs, reference_outputs)
# y le pasa a cada función lo que pide. Devolver {"results": [...]} = varias notas a la vez.

def revisiones_automaticas(outputs: dict, reference_outputs: dict) -> dict:
    citas = leer_citas(outputs["texto"])
    recibidos = {(f[0], normalizar(f[1])) for f in outputs["articulos"]}
    esperados = {(n, normalizar(a)) for n, a in reference_outputs["esperados"]}
    inventadas = citas - recibidos
    return {"results": [
        {"key": "cita", "score": int(bool(citas))},
        {"key": "sin_inventar", "score": int(not inventadas),
         "comment": f"inventadas: {sorted(inventadas)}" if inventadas else None},
        {"key": "cita_esperado", "score": int(bool(citas & esperados))},
    ]}


def juez(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    articulos = [tuple(fila) for fila in outputs["articulos"]]
    puntos = reference_outputs["puntos_clave"]
    veredicto, _uso = juzgar(claude, inputs["pregunta"], articulos, outputs["texto"], puntos)
    cubiertos = sum(p.cubierto for p in veredicto.puntos_clave)
    faltan = [p.punto for p in veredicto.puntos_clave if not p.cubierto]
    return {"results": [
        {"key": "fiel", "score": int(veredicto.fiel),
         "comment": "; ".join(veredicto.afirmaciones_sin_respaldo) or veredicto.comentario},
        {"key": "responde", "score": int(veredicto.responde)},
        {"key": "puntos_clave", "score": cubiertos / len(puntos),     # 0 a 1 (ej. 2 de 3 = 0,67)
         "comment": ("Faltan: " + "; ".join(faltan)) if faltan else None},
    ]}


# ---------- 4. Correr el experimento ----------

def main():
    asegurar_dataset()
    ids = {int(x) for x in sys.argv[1:]}
    ejemplos = [e for e in ls.list_examples(dataset_name=DATASET)
                if not ids or e.metadata["id"] in ids]
    print(f"Corriendo experimento con {len(ejemplos)} preguntas...")

    resultados = ls.evaluate(
        asistente,                                   # qué correr
        data=ejemplos,                               # sobre qué preguntas
        evaluators=[revisiones_automaticas, juez],   # con qué notas
        experiment_prefix=f"prompt-{VERSION_PROMPT}",
        metadata={"prompt": VERSION_PROMPT, "modelo": MODELO_RESPUESTA},
        max_concurrency=0,                           # de a una pregunta (sin paralelo)
    )
    print(f"Listo. Experimento: {resultados.experiment_name}")


if __name__ == "__main__":
    main()
