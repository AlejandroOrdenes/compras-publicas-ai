"""Responde una pregunta sobre compras públicas usando solo la normativa (RAG).

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.preguntar "¿cuándo puedo comprar sin licitar?"

Flujo: buscar los artículos más parecidos (pgvector) → pasárselos a Claude →
Claude responde citando los artículos.
"""
import sys

import anthropic

from app.config import settings
from scripts.buscar_normativa import buscar

MODELO = "claude-opus-5-5"
CANTIDAD_ARTICULOS = 5

NOMBRES = {"ley_19886": "Ley 19.886", "reglamento_dto_661": "Reglamento (Decreto 661)"}

INSTRUCCIONES = """Eres un asistente que responde preguntas sobre la normativa chilena de compras públicas.

Reglas:
- Responde SOLO con la información de los artículos entregados entre etiquetas <articulo>. No uses conocimiento propio.
- Cita cada afirmación con la norma y el número de artículo, por ejemplo: (Reglamento (Decreto 661), art. 71).
- Si los artículos no alcanzan para responder, dilo claramente en vez de inventar.
- Los artículos marcados transitorio="sí" son reglas temporales de cuando la ley entró en vigencia; adviértelo si los usas.
- Escribe en español de Chile, claro y sin jerga legal innecesaria. Respuesta breve: un párrafo corto o una lista."""


def armar_contexto(articulos: list[tuple]) -> str:
    bloques = []
    for norma, articulo, transitorio, texto, _similitud in articulos:
        bloques.append(
            f'<articulo norma="{NOMBRES.get(norma, norma)}" numero="{articulo}" '
            f'transitorio="{"sí" if transitorio else "no"}">\n{texto}\n</articulo>'
        )
    return "\n\n".join(bloques)


def responder(pregunta: str) -> dict:
    """Busca los artículos, se los pasa a Claude y devuelve todo lo que pasó.

    Devuelve un diccionario (lo usan preguntar() y eval/evaluar_respuestas.py):
        texto         → la respuesta de Claude
        articulos     → las filas que recibió Claude (norma, articulo, transitorio, texto, similitud)
        rechazo       → True si un filtro de seguridad no dejó responder
        tokens_entrada, tokens_salida
    """
    articulos = buscar(pregunta, CANTIDAD_ARTICULOS)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    respuesta = client.beta.messages.create(
        model=MODELO,
        max_tokens=16000,
        system=INSTRUCCIONES,
        messages=[{
            "role": "user",
            "content": f"{armar_contexto(articulos)}\n\nPregunta: {pregunta}",
        }],
        # Si un filtro de seguridad rechaza la pregunta, la API reintenta sola con otro modelo.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    rechazo = respuesta.stop_reason == "refusal"
    return {
        "texto": "" if rechazo else "".join(b.text for b in respuesta.content if b.type == "text"),
        "articulos": articulos,
        "rechazo": rechazo,
        "tokens_entrada": respuesta.usage.input_tokens,
        "tokens_salida": respuesta.usage.output_tokens,
    }


def preguntar(pregunta: str) -> str:
    """Versión para personas: la respuesta como texto, con los tokens al final."""
    r = responder(pregunta)
    if r["rechazo"]:
        return "Claude no pudo responder esta pregunta."
    return f"{r['texto']}\n\n[tokens: {r['tokens_entrada']} de entrada, {r['tokens_salida']} de salida]"


def main():
    if len(sys.argv) < 2:
        sys.exit('Uso: python -m scripts.preguntar "tu pregunta"')
    print(preguntar(sys.argv[1]))


if __name__ == "__main__":
    main()
