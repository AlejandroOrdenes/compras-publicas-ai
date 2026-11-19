"""Evalúa las RESPUESTAS completas del asistente (búsqueda + Claude).

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m eval.evaluar_respuestas 2 11 14 17 32     ← solo esas preguntas (por id)
    python -m eval.evaluar_respuestas                   ← todas las que tienen puntos_clave

CUESTA DINERO (~US$0,05 por pregunta): Opus responde y Sonnet corrige.
Al final imprime el costo real de la corrida.

Dos tipos de revisión:
  1. Automáticas (Python, gratis), leyendo las citas "(Ley 19.886, art. 8 bis)":
     - cita    → ¿citó al menos un artículo?
     - inventa → ¿citó algún artículo que NO estaba entre los que recibió? (lo grave)
     - esperado→ ¿citó alguno de los artículos esperados de la pauta?
  2. Juez (Claude Sonnet lee y corrige con una pauta = "LLM-as-judge"):
     - fiel     → ¿todo lo que afirma está en los artículos entregados?
     - responde → ¿contesta lo que se preguntó?
     - puntos   → ¿cuántos puntos clave de la pauta incluye?
"""
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel

from app.config import settings
from scripts.preguntar import MODELO as MODELO_RESPUESTA
from scripts.preguntar import armar_contexto, responder

PAUTA = Path(__file__).parent / "preguntas.json"
CARPETA_RESULTADOS = Path(__file__).parent / "resultados"
MODELO_JUEZ = "claude-sonnet-5-5"

# US$ por millón de tokens (entrada, salida). Haiku (reescritura) se omite: ~US$0,00006 por pregunta.
PRECIOS = {MODELO_RESPUESTA: (4.00, 20.00), MODELO_JUEZ: (2.00, 10.00)}

# ---------- 1. Revisiones automáticas: leer las citas ----------

# Cómo cita Claude: "(Ley 19.886, art. 8 bis)", "(Reglamento (Decreto 661), arts. 71 y 72)".
# Grupo 1 = la norma; grupo 2 = lo que viene después de "art." hasta ")" o ";".
CITA = re.compile(r"(Ley 19\.886|Reglamento(?: \(Decreto 661\))?),?\s*arts?\.\s*([^);]+)", re.IGNORECASE)
# Un número de artículo: "71", "8 bis", "35 quáter", "4°".
NUMERO = re.compile(r"\d+\s*°?(?:\s*(?:bis|ter|quáter|quater|quinquies|sexies|septies|octies|nonies|decies)\b)?",
                    re.IGNORECASE)


def normalizar(articulo: str) -> str:
    """'8 bis' / '8 BIS' / '4°' / '35 quáter' → '8 BIS' / '4' / '35 QUATER' (para comparar)."""
    sin_tildes = unicodedata.normalize("NFD", articulo).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.replace("°", " ").replace("º", " ").upper().split())


# Desde aquí en adelante, los números son partes del artículo, no artículos:
# "art. 154 N° 7, 8 y 9" → el 8 y el 9 son numerales del 154 (falso "inventa" visto en la pregunta 16).
SUBDIVISION = re.compile(r"\b(?:N[°º]|n[°º]|numerales?|letras?|literal(?:es)?|incisos?)", re.IGNORECASE)


def leer_citas(texto: str) -> set[tuple[str, str]]:
    """Devuelve las citas de la respuesta como {(norma, articulo_normalizado)}."""
    citas = set()
    for norma_texto, lista in CITA.findall(texto):
        norma = "ley_19886" if norma_texto.lower().startswith("ley") else "reglamento_dto_661"
        lista = SUBDIVISION.split(lista)[0]               # "154 N° 7, 8 y 9" → "154 "
        for pedazo in re.split(r",\s*|\s+y\s+", lista):   # "71 y 72" → ["71", "72"]
            numero = NUMERO.match(pedazo.strip())
            if numero:                                    # ignora "inciso 2", "letra b)", etc.
                citas.add((norma, normalizar(numero.group())))
    return citas


# ---------- 2. El juez ----------

class PuntoClave(BaseModel):
    punto: str
    cubierto: bool


class Veredicto(BaseModel):
    """Formato fijo que debe devolver el juez (structured outputs)."""
    fiel: bool
    afirmaciones_sin_respaldo: list[str]
    responde: bool
    puntos_clave: list[PuntoClave]
    comentario: str


INSTRUCCIONES_JUEZ = """Eres un evaluador estricto de un asistente que responde preguntas sobre la normativa chilena de compras públicas usando SOLO los artículos que se le entregan.

Recibirás: la pregunta, los artículos que recibió el asistente, su respuesta y una lista de puntos clave.

Evalúa:
- fiel: true solo si TODO lo que afirma la respuesta está respaldado por los artículos entregados. Juzga contra los artículos, no contra tu propio conocimiento. Si algo no está en los artículos, anótalo en afirmaciones_sin_respaldo.
- responde: true si contesta directamente lo que se preguntó (no basta con hablar del tema).
- puntos_clave: para cada punto de la lista, cubierto=true si la respuesta incluye esa información (con otras palabras está bien). Copia el texto del punto en "punto".
- comentario: una o dos frases sobre lo más importante que falla o que está bien."""


def juzgar(client, pregunta, articulos, respuesta, puntos) -> tuple[Veredicto, object]:
    contenido = (
        f"<pregunta>{pregunta}</pregunta>\n\n"
        f"<articulos_entregados>\n{armar_contexto(articulos)}\n</articulos_entregados>\n\n"
        f"<respuesta>{respuesta}</respuesta>\n\n"
        "<puntos_clave>\n" + "\n".join(f"- {p}" for p in puntos) + "\n</puntos_clave>"
    )
    r = client.messages.parse(
        model=MODELO_JUEZ,
        max_tokens=16000,
        system=INSTRUCCIONES_JUEZ,
        messages=[{"role": "user", "content": contenido}],
        output_format=Veredicto,     # el SDK valida que la respuesta calce con Veredicto
    )
    return r.parsed_output, r.usage


# ---------- 3. Correr la evaluación ----------

def costo(modelo: str, entrada: int, salida: int) -> float:
    precio_entrada, precio_salida = PRECIOS[modelo]
    return entrada / 1e6 * precio_entrada + salida / 1e6 * precio_salida


def main():
    pauta = json.loads(PAUTA.read_text(encoding="utf-8"))
    ids = {int(x) for x in sys.argv[1:]}
    items = [p for p in pauta if "puntos_clave" in p and (not ids or p["id"] in ids)]
    print(f"Evaluando {len(items)} preguntas (Opus responde, Sonnet corrige)...\n")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resultados = []
    gasto = 0.0
    for item in items:
        r = responder(item["pregunta"])
        gasto += costo(MODELO_RESPUESTA, r["tokens_entrada"], r["tokens_salida"])

        recibidos = {(f[0], normalizar(f[1])) for f in r["articulos"]}
        esperados = {(n, normalizar(a)) for n, a in item["esperados"]}
        citas = leer_citas(r["texto"])

        veredicto, uso = juzgar(client, item["pregunta"], r["articulos"], r["texto"], item["puntos_clave"])
        gasto += costo(MODELO_JUEZ, uso.input_tokens, uso.output_tokens)

        cubiertos = sum(p.cubierto for p in veredicto.puntos_clave)
        fila = {
            "id": item["id"],
            "pregunta": item["pregunta"],
            "respuesta": r["texto"],
            "recibidos": sorted(recibidos),
            "citas": sorted(citas),
            "cita": len(citas) > 0,
            "inventa": sorted(citas - recibidos),
            "cita_esperado": len(citas & esperados) > 0,
            "fiel": veredicto.fiel,
            "responde": veredicto.responde,
            "puntos": f"{cubiertos}/{len(item['puntos_clave'])}",
            "juez": veredicto.model_dump(),
        }
        resultados.append(fila)

        si_no = lambda b: "sí" if b else "NO"
        print(f"#{item['id']} {item['pregunta']}")
        print(f"   cita {si_no(fila['cita'])} | inventa {fila['inventa'] or 'nada'} | "
              f"cita esperado {si_no(fila['cita_esperado'])} | fiel {si_no(fila['fiel'])} | "
              f"responde {si_no(fila['responde'])} | puntos clave {fila['puntos']}")
        print(f"   juez: {veredicto.comentario}\n")

    n = len(resultados)
    print("Resumen")
    print(f"  citan algo:            {sum(f['cita'] for f in resultados)}/{n}")
    print(f"  sin citas inventadas:  {sum(not f['inventa'] for f in resultados)}/{n}")
    print(f"  citan un esperado:     {sum(f['cita_esperado'] for f in resultados)}/{n}")
    print(f"  fieles:                {sum(f['fiel'] for f in resultados)}/{n}")
    print(f"  responden:             {sum(f['responde'] for f in resultados)}/{n}")
    total = sum(len(i["puntos_clave"]) for i in items)
    hechos = sum(int(f["puntos"].split("/")[0]) for f in resultados)
    print(f"  puntos clave:          {hechos}/{total} ({hechos / total:.0%})")
    print(f"\nCosto de esta corrida: US${gasto:.3f}")

    # Se guarda todo (respuestas completas incluidas) para poder leerlo con calma.
    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    archivo = CARPETA_RESULTADOS / f"respuestas_{datetime.now():%Y%m%d_%H%M}.json"
    archivo.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Detalle guardado en {archivo}")


if __name__ == "__main__":
    main()
