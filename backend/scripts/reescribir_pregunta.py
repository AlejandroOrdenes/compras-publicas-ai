"""Traduce una pregunta cotidiana al vocabulario de la ley, para buscar mejor.

Ejecutar desde la carpeta backend, con el venv activo:
    python -m scripts.reescribir_pregunta "¿se puede comprar sin usar Mercado Público?"

Por qué: la gente pregunta "Mercado Público" y la ley dice "Sistema de
Información". Los embeddings entienden sinónimos, pero no siempre. Medido con
eval/evaluar_busqueda.py: cobertura 85% → 87% (preguntas cotidianas 78% → 81%).

Usa Claude Haiku (el modelo más barato): ~US$0,00006 por pregunta, ~1 s.
Si Haiku falla o se niega, se devuelve la pregunta original: la búsqueda
nunca se cae por culpa de este paso.
"""
import sys

import anthropic

from app.config import settings

MODELO = "claude-haiku-5-5"

# Estas instrucciones son el resultado del 2º intento (ver diario 5.7): con unas
# más generales, Haiku agregaba relleno ("conforme a la Ley 19.886...") y temas
# no preguntados, y la búsqueda empeoraba.
INSTRUCCIONES = """Traduce la pregunta del usuario al vocabulario de la normativa chilena de compras públicas, para buscarla en el texto legal.
Reglas:
- Cambia solo las palabras cotidianas por las del texto legal. Ejemplos: "Mercado Público" → "Sistema de Información"; "empresa" o "emprendedor" → "proveedor"; "municipio" o "servicio público" → "Entidad"; "sin licitar" → "trato directo".
- Si la pregunta ya usa términos legales, devuélvela casi igual.
- Mantén el mismo tema: no agregues temas, procedimientos ni montos que la pregunta no menciona.
- No nombres la ley ni el reglamento. No respondas la pregunta.
- Devuelve SOLO la pregunta traducida, en una línea y con no más de 25 palabras."""


def reescribir(pregunta: str) -> str:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        respuesta = client.messages.create(
            model=MODELO,
            max_tokens=1024,                    # incluye lo que Haiku "piensa" antes de responder
            output_config={"effort": "low"},    # tarea simple: pensar poco = más rápido y barato
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": pregunta}],
        )
    except anthropic.APIError as error:
        print(f"[reescribir] Haiku falló ({error.__class__.__name__}); se usa la pregunta original.")
        return pregunta

    if respuesta.stop_reason == "refusal":
        return pregunta
    texto = "".join(b.text for b in respuesta.content if b.type == "text").strip()
    return texto or pregunta


def main():
    if len(sys.argv) < 2:
        sys.exit('Uso: python -m scripts.reescribir_pregunta "tu pregunta"')
    print(reescribir(sys.argv[1]))


if __name__ == "__main__":
    main()
