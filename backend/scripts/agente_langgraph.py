"""El mismo agente de scripts/agente.py, pero con LangGraph.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.agente_langgraph "¿Qué licitaciones de aseo hay publicadas y qué plazo mínimo exige la ley?"
    python -m scripts.agente_langgraph --grafo        ← solo imprime el grafo (gratis)

CUESTA DINERO (~US$0,10-0,20 por pregunta), igual que el agente a mano.

LangGraph en 3 conceptos:
  - Estado:  los datos que viajan entre nodos. Aquí, la lista de mensajes (MessagesState).
             Lo que devuelve cada nodo se AGREGA a la lista (como un reducer de Redux).
  - Nodos:   funciones que reciben el estado y devuelven un cambio ("modelo", "herramientas").
  - Aristas: qué nodo sigue. Una es condicional: si Claude pidió herramientas → "herramientas";
             si no → END.

Equivalencias con el agente a mano:
  mensajes = [...]                       → MessagesState
  client.messages.create(...)            → nodo "modelo"
  ejecutar() + EJECUTORES                → ToolNode
  if stop_reason != "tool_use": return   → tools_condition
  for vuelta in range(MAX_VUELTAS)       → recursion_limit
Las herramientas son LAS MISMAS funciones de agente.py (solo se envuelven con @tool).
"""
import sys
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.config import settings
from scripts import agente as a      # herramientas e instrucciones del agente a mano

MAX_VUELTAS = 6
# Cada vuelta = 2 pasos del grafo (modelo + herramientas); +1 por la respuesta final.
LIMITE_PASOS = MAX_VUELTAS * 2 + 1


# ---------- Herramientas: las mismas funciones, envueltas con @tool ----------
# @tool lee el nombre, los parámetros (con sus tipos) y la docstring, y arma la
# descripción que recibe Claude (lo que en agente.py escribimos a mano en HERRAMIENTAS).

# parse_docstring=True: la sección "Args:" se convierte en la descripción de cada parámetro.
# Literal[...] = solo esos valores (como el "enum" del esquema en agente.py; en TypeScript:
# 'Publicada' | 'Cerrada' | ...).
@tool(parse_docstring=True)
def buscar_licitaciones(texto: str | None = None, region: str | None = None,
                        estado: Literal["Publicada", "Cerrada", "Adjudicada", "Desierta", "Revocada"] | None = None,
                        organismo: str | None = None, monto_min: float | None = None,
                        monto_max: float | None = None, limite: int = 10, codigo: str | None = None) -> list[dict]:
    """Busca licitaciones de Mercado Público guardadas en la base de datos (una muestra, no todas las de Chile).

    Úsala cuando pregunten por licitaciones, compras concretas, organismos que compran algo, montos,
    estados o proveedores adjudicados. Todos los filtros son opcionales y se combinan.
    Devuelve hasta 'limite' licitaciones (1 a 20), cada una con su link oficial en el campo 'url'.

    Args:
        texto: palabras a buscar en el nombre, la descripción y los ítems. Ej: 'aseo', 'computadores'.
        region: parte del nombre de la región. Ej: 'Valparaíso', 'Metropolitana'.
        estado: estado de la licitación.
        organismo: parte del nombre del organismo comprador. Ej: 'Municipalidad'.
        monto_min: monto estimado mínimo, en pesos.
        monto_max: monto estimado máximo, en pesos.
        limite: cuántas devolver (1 a 20).
        codigo: código exacto de una licitación. Ej: '1048-75-LE26'.
    """
    return a.buscar_licitaciones(texto, region, estado, organismo, monto_min, monto_max, limite, codigo)


@tool(parse_docstring=True)
def consultar_normativa(consulta: str) -> str:
    """Busca los artículos más relevantes de la Ley 19.886 y su Reglamento (Decreto 661).

    Úsala para cualquier pregunta sobre reglas, plazos, montos, requisitos o procedimientos de
    compras públicas. Devuelve el texto completo de hasta 5 artículos.

    Args:
        consulta: la pregunta legal, en una frase. Ej: 'plazo mínimo de publicación de una licitación'.
    """
    return a.consultar_normativa(consulta)


HERRAMIENTAS = [buscar_licitaciones, consultar_normativa]


# ---------- El grafo ----------

def construir_grafo():
    modelo = ChatAnthropic(
        model=a.MODELO,
        api_key=settings.anthropic_api_key,
        max_tokens=16000,
        thinking={"type": "adaptive"},
    ).bind_tools(HERRAMIENTAS)          # le "muestra" las herramientas a Claude en cada llamada

    def nodo_modelo(estado: MessagesState) -> dict:
        """Llama a Claude con las instrucciones + toda la conversación hasta ahora."""
        respuesta = modelo.invoke([SystemMessage(a.INSTRUCCIONES)] + estado["messages"])
        return {"messages": [respuesta]}            # se AGREGA a la lista de mensajes

    grafo = StateGraph(MessagesState)
    grafo.add_node("modelo", nodo_modelo)
    grafo.add_node("herramientas", ToolNode(HERRAMIENTAS))   # ejecuta los tool_use; errores → a Claude
    grafo.add_edge(START, "modelo")
    # tools_condition mira el último mensaje: si trae pedidos de herramientas → "tools"; si no → END.
    grafo.add_conditional_edges("modelo", tools_condition, {"tools": "herramientas", END: END})
    grafo.add_edge("herramientas", "modelo")                 # después de ejecutar, vuelve a Claude
    return grafo.compile()


def _llamadas(mensajes) -> list[dict]:
    resultados = {m.tool_call_id: m.content for m in mensajes if isinstance(m, ToolMessage)}
    return [{"herramienta": llamada["name"], "argumentos": llamada["args"],
             "resultado": resultados.get(llamada["id"], "")}
            for m in mensajes if getattr(m, "tool_calls", None) for llamada in m.tool_calls]


def preguntar(pregunta: str) -> dict:
    grafo = construir_grafo()
    estado = grafo.invoke(
        {"messages": [("user", pregunta)]},
        config={"recursion_limit": LIMITE_PASOS, "run_name": "agente_langgraph"},
    )
    mensajes = estado["messages"]
    pasos = [f"{llamada['name']}({llamada['args']})"
             for m in mensajes if getattr(m, "tool_calls", None) for llamada in m.tool_calls]
    uso = [m.usage_metadata for m in mensajes if getattr(m, "usage_metadata", None)]
    return {
        "texto": mensajes[-1].text,                  # .text = solo el texto (sin bloques de razonamiento)
        "pasos": pasos,
        # Cada llamada a herramienta: nombre, argumentos y lo que devolvió (lo usa la evaluación).
        # El pedido (tool_call, en el mensaje de Claude) y su resultado (ToolMessage) se unen por id.
        "llamadas": _llamadas(mensajes),
        "llamadas_a_claude": len(uso),
        "tokens_entrada": sum(u["input_tokens"] for u in uso),
        "tokens_salida": sum(u["output_tokens"] for u in uso),
    }


def main():
    if "--grafo" in sys.argv:
        print(construir_grafo().get_graph().draw_mermaid())   # diagrama en formato Mermaid
        return
    pregunta = " ".join(sys.argv[1:]) or "¿Qué licitaciones de aseo hay publicadas y qué plazo mínimo de publicación exige la ley?"
    r = preguntar(pregunta)
    print("Pasos del agente:")
    for paso in r["pasos"]:
        print("  ", paso)
    print(f"\n{r['texto']}\n")
    costo = r["tokens_entrada"] / 1e6 * 4 + r["tokens_salida"] / 1e6 * 20
    print(f"[{r['llamadas_a_claude']} llamadas a Claude | tokens: {r['tokens_entrada']} entrada, "
          f"{r['tokens_salida']} salida | ~US${costo:.3f}]")


if __name__ == "__main__":
    main()
