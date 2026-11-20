"""Agente "a mano": Claude + 2 herramientas + un loop, SIN framework.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.agente "¿Qué licitaciones de aseo hay publicadas y qué plazo mínimo exige la ley?"

CUESTA DINERO (~US$0,10-0,20 por pregunta): cada vuelta del loop reenvía toda la conversación a Opus.

Cómo funciona (tool use):
  1. Le mandamos a Claude la pregunta + la descripción de las herramientas.
  2. Si Claude necesita datos, responde con stop_reason="tool_use" y bloques "tool_use"
     ("usa buscar_licitaciones con estos datos"). Claude NO ejecuta nada: solo pide.
  3. Nuestro código ejecuta la herramienta y le devuelve el resultado ("tool_result").
  4. Se repite hasta que Claude responde con stop_reason="end_turn" (respuesta final).
La API no tiene memoria: en cada vuelta se reenvía la conversación completa.
"""
import json
import sys

import anthropic
import psycopg
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic

from app.config import settings
from scripts.buscar_normativa import buscar
from scripts.preguntar import armar_contexto

MODELO = "claude-opus-5-5"
MAX_VUELTAS = 6          # freno: si Claude encadena herramientas sin terminar, se corta
URL_FICHA = "https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={codigo}"

INSTRUCCIONES = """Eres un asistente sobre compras públicas de Chile. Tienes dos herramientas:
- buscar_licitaciones: licitaciones reales de Mercado Público (una muestra sincronizada en la base).
- consultar_normativa: artículos de la Ley 19.886 y su Reglamento (Decreto 661).

Reglas:
- Usa las herramientas para responder; no respondas de memoria sobre licitaciones ni sobre la ley.
- Sobre la ley: responde SOLO con los artículos que devuelva consultar_normativa y cita cada afirmación, por ejemplo: (Ley 19.886, art. 8 bis). No agregues ejemplos, nombres ni datos que no estén en los artículos.
- Sobre licitaciones: usa SOLO los datos que devuelva buscar_licitaciones. Para cada licitación que menciones, incluye su link copiándolo EXACTAMENTE desde el campo "url". Nunca escribas ni modifiques un link.
- Si el monto es "no informado", dilo así; no lo supongas.
- Si un proveedor aparece como "Persona natural (dato protegido)", no intentes identificarlo.
- Si las herramientas no traen lo necesario, dilo claramente.
- Escribe en español de Chile, claro y breve."""

HERRAMIENTAS = [
    {
        "name": "buscar_licitaciones",
        "description": (
            "Busca licitaciones de Mercado Público guardadas en la base de datos (una muestra, no todas las de Chile). "
            "Úsala cuando pregunten por licitaciones, compras concretas, organismos que compran algo, montos, "
            "estados o proveedores adjudicados. Todos los filtros son opcionales y se combinan. "
            "Devuelve hasta 'limite' licitaciones, cada una con su link oficial en el campo 'url'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string",
                          "description": "Palabras a buscar en el nombre, la descripción y los ítems. Ej: 'aseo', 'computadores'."},
                "region": {"type": "string", "description": "Parte del nombre de la región. Ej: 'Valparaíso', 'Metropolitana'."},
                "estado": {"type": "string", "enum": ["Publicada", "Cerrada", "Adjudicada", "Desierta"]},
                "organismo": {"type": "string", "description": "Parte del nombre del organismo comprador. Ej: 'Municipalidad'."},
                "monto_min": {"type": "number", "description": "Monto estimado mínimo, en pesos."},
                "monto_max": {"type": "number", "description": "Monto estimado máximo, en pesos."},
                "limite": {"type": "integer", "description": "Cuántas devolver (1 a 20; por defecto 10)."},
                "codigo": {"type": "string", "description": "Código exacto de una licitación. Ej: '1048-75-LE26'."},
            },
        },
    },
    {
        "name": "consultar_normativa",
        "description": (
            "Busca los artículos más relevantes de la Ley 19.886 y su Reglamento (Decreto 661) para una consulta. "
            "Úsala para cualquier pregunta sobre reglas, plazos, montos, requisitos o procedimientos de compras públicas. "
            "Devuelve el texto completo de hasta 5 artículos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {"type": "string", "description": "La pregunta legal, en una frase. Ej: 'plazo mínimo de publicación de una licitación'."},
            },
            "required": ["consulta"],
        },
    },
]


# ---------- Herramientas: código nuestro que Claude puede pedir ----------

# Claude NO escribe SQL: solo elige valores. El SQL es fijo y los valores van como
# parámetros (%s), así una pregunta maliciosa no puede inyectar SQL.
# unaccent(): "Valparaiso" encuentra "Valparaíso". Búsqueda de texto: Postgres FTS en español.
BUSCAR_LICITACIONES = """
SELECT codigo, nombre, estado, organismo, comuna, region, monto_estimado, moneda,
       fecha_publicacion, fecha_cierre, items
FROM licitaciones
WHERE (%(texto)s::text IS NULL OR
       to_tsvector('spanish', unaccent(coalesce(nombre,'') || ' ' || coalesce(descripcion,'') || ' ' || items::text))
       @@ plainto_tsquery('spanish', unaccent(%(texto)s)))
  AND (%(region)s::text IS NULL OR unaccent(region) ILIKE unaccent('%%' || %(region)s || '%%'))
  AND (%(estado)s::text IS NULL OR estado ILIKE %(estado)s || '%%')
  AND (%(organismo)s::text IS NULL OR unaccent(organismo) ILIKE unaccent('%%' || %(organismo)s || '%%'))
  AND (%(monto_min)s::numeric IS NULL OR monto_estimado >= %(monto_min)s)
  AND (%(monto_max)s::numeric IS NULL OR monto_estimado <= %(monto_max)s)
  AND (%(codigo)s::text IS NULL OR upper(codigo) = upper(trim(%(codigo)s)))
ORDER BY fecha_publicacion DESC NULLS LAST
LIMIT %(limite)s
"""


@traceable(name="buscar_licitaciones", run_type="tool")
def buscar_licitaciones(texto=None, region=None, estado=None, organismo=None,
                        monto_min=None, monto_max=None, limite=10, codigo=None) -> list[dict]:
    parametros = {"texto": texto, "region": region, "estado": estado, "organismo": organismo,
                  "monto_min": monto_min, "monto_max": monto_max, "codigo": codigo,
                  "limite": max(1, min(int(limite or 10), 20))}
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        filas = conn.execute(BUSCAR_LICITACIONES, parametros).fetchall()

    resultados = []
    for codigo, nombre, estado_, organismo_, comuna, region_, monto, moneda, publicada, cierre, items in filas:
        proveedores = sorted({i["proveedor"] for i in items if i.get("proveedor")})
        resultados.append({
            "codigo": codigo,
            "nombre": nombre,
            "estado": estado_,
            "organismo": organismo_,
            "comuna": comuna,
            "region": region_,
            "monto_estimado": f"{monto:,.0f} {moneda}".replace(",", ".") if monto else "no informado",
            "fecha_publicacion": publicada.date().isoformat() if publicada else None,
            "fecha_cierre": cierre.date().isoformat() if cierre else None,
            "proveedores_adjudicados": proveedores,
            "url": URL_FICHA.format(codigo=codigo),      # el link lo arma el código, no Claude
        })
    return resultados


@traceable(name="consultar_normativa", run_type="tool")
def consultar_normativa(consulta: str) -> str:
    return armar_contexto(buscar(consulta))


# Nombre que usa Claude → función de Python que la ejecuta.
EJECUTORES = {"buscar_licitaciones": buscar_licitaciones, "consultar_normativa": consultar_normativa}


def ejecutar(bloque) -> dict:
    """Ejecuta un pedido de Claude (bloque tool_use) y arma el tool_result para devolverle."""
    try:
        resultado = EJECUTORES[bloque.name](**bloque.input)
        contenido = resultado if isinstance(resultado, str) else json.dumps(resultado, ensure_ascii=False)
        return {"type": "tool_result", "tool_use_id": bloque.id, "content": contenido}
    except Exception as error:
        # El error se le devuelve a Claude (puede corregir o avisar); el programa no se cae.
        return {"type": "tool_result", "tool_use_id": bloque.id, "is_error": True,
                "content": f"Error al ejecutar {bloque.name}: {type(error).__name__}: {error}"}


# ---------- El loop del agente ----------

@traceable(name="agente")
def agente(pregunta: str) -> dict:
    client = wrap_anthropic(anthropic.Anthropic(api_key=settings.anthropic_api_key))
    mensajes = [{"role": "user", "content": pregunta}]
    pasos, tokens_entrada, tokens_salida = [], 0, 0

    for vuelta in range(1, MAX_VUELTAS + 1):
        respuesta = client.messages.create(
            model=MODELO,
            max_tokens=16000,
            system=INSTRUCCIONES,
            tools=HERRAMIENTAS,
            thinking={"type": "adaptive"},     # Claude decide cuánto pensar antes de elegir herramientas
            messages=mensajes,
        )
        tokens_entrada += respuesta.usage.input_tokens
        tokens_salida += respuesta.usage.output_tokens

        if respuesta.stop_reason != "tool_use":
            # end_turn = terminó. (max_tokens / refusal = se cortó o se negó: se informa igual.)
            texto = "".join(b.text for b in respuesta.content if b.type == "text")
            return {"texto": texto, "motivo_fin": respuesta.stop_reason, "vueltas": vuelta,
                    "pasos": pasos, "tokens_entrada": tokens_entrada, "tokens_salida": tokens_salida}

        # Claude pidió herramientas: guardar lo que dijo (incluye sus bloques tool_use y de
        # razonamiento, que hay que devolver tal cual) y responderle con los resultados.
        mensajes.append({"role": "assistant", "content": respuesta.content})
        pedidos = [b for b in respuesta.content if b.type == "tool_use"]
        resultados = []
        for bloque in pedidos:
            pasos.append(f"vuelta {vuelta}: {bloque.name}({json.dumps(bloque.input, ensure_ascii=False)})")
            resultados.append(ejecutar(bloque))
        mensajes.append({"role": "user", "content": resultados})

    return {"texto": f"[Se alcanzó el máximo de {MAX_VUELTAS} vueltas sin respuesta final.]",
            "motivo_fin": "max_vueltas", "vueltas": MAX_VUELTAS, "pasos": pasos,
            "tokens_entrada": tokens_entrada, "tokens_salida": tokens_salida}


def main():
    pregunta = " ".join(sys.argv[1:]) or "¿Qué licitaciones de aseo hay publicadas y qué plazo mínimo de publicación exige la ley?"
    r = agente(pregunta)
    print("Pasos del agente:")
    for paso in r["pasos"]:
        print("  ", paso)
    print(f"\n{r['texto']}\n")
    costo = r["tokens_entrada"] / 1e6 * 4 + r["tokens_salida"] / 1e6 * 20
    print(f"[{r['vueltas']} vueltas | fin: {r['motivo_fin']} | tokens: {r['tokens_entrada']} entrada, "
          f"{r['tokens_salida']} salida | ~US${costo:.3f}]")


if __name__ == "__main__":
    main()
