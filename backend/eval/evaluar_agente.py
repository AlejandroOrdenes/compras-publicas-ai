"""Evalúa el AGENTE (LangGraph): elección de herramientas, invenciones, privacidad y fidelidad.

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m eval.evaluar_agente 5 6        ← solo esas preguntas (por id)
    python -m eval.evaluar_agente            ← las 10
    python -m eval.evaluar_agente --rejuzgar eval/resultados/agente_XXXX.json
        ← vuelve a corregir respuestas guardadas SIN volver a correr el agente (solo paga el juez)

CUESTA DINERO (~US$0,05 por pregunta): Opus (agente) responde y Sonnet corrige.

Revisiones automáticas (Python, gratis):
  herramientas_ok        → usó todas las herramientas esperadas
  sin_links_inventados   → cada link de la respuesta vino en los resultados de buscar_licitaciones
  sin_codigos_inventados → cada código de licitación mencionado vino en los resultados (o en la pregunta)
  sin_citas_inventadas   → cada artículo citado vino en consultar_normativa
  sin_rut                → la respuesta no contiene ningún RUT
  sin_links (si aplica)  → preguntas de pura ley o sin resultados: no debe haber links
  esperado               → aparecen los códigos y textos esperados de la pauta
Juez (Sonnet): fiel, responde y cumple el criterio. Ve LO MISMO que vio el agente:
sus instrucciones y cada llamada a herramienta con sus argumentos y su resultado
(sin eso, castigaba cosas correctas: ver diario 7.8).
"""
import ast
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel

from app.config import settings
from eval.evaluar_respuestas import MODELO_JUEZ, leer_citas, normalizar
from scripts import agente as a
from scripts.agente_langgraph import preguntar
from scripts.filtro_datos_personales import RUT

PAUTA = Path(__file__).parent / "preguntas_agente.json"
CARPETA_RESULTADOS = Path(__file__).parent / "resultados"
PRECIOS = {"opus": (4.00, 20.00), "sonnet": (2.00, 10.00)}   # US$ por millón (entrada, salida)

LINK = re.compile(r"mercadopublico\.cl/\S*?idlicitacion=([\w-]+)", re.IGNORECASE)
# Código de licitación: "1057390-21-LR26", "1048-75-LE26", "1184-3-L126".
CODIGO = re.compile(r"\b\d{3,7}-\d{1,4}-[A-Z][A-Z0-9]{1,3}\b")
# Artículos que devolvió consultar_normativa: <articulo norma="Ley 19.886" numero="8 bis" ...>
ARTICULO = re.compile(r'<articulo norma="([^"]+)" numero="([^"]+)"')


# ---------- 1. Revisiones automáticas ----------

def revisar(item: dict, texto: str, llamadas: list[dict]) -> dict:
    usadas = {ll["herramienta"] for ll in llamadas}

    codigos_devueltos, articulos_devueltos = set(), set()
    for ll in llamadas:
        if ll["herramienta"] == "buscar_licitaciones":
            try:
                codigos_devueltos |= {lic["codigo"] for lic in json.loads(ll["resultado"])}
            except (json.JSONDecodeError, TypeError):
                pass                                   # la herramienta devolvió un error
        elif ll["herramienta"] == "consultar_normativa":
            for norma, numero in ARTICULO.findall(ll["resultado"]):
                clave = "ley_19886" if norma.startswith("Ley") else "reglamento_dto_661"
                articulos_devueltos.add((clave, normalizar(numero)))

    links = set(LINK.findall(texto))
    codigos = set(CODIGO.findall(texto)) - set(CODIGO.findall(item["pregunta"]))
    citas = leer_citas(texto)

    fila = {
        "herramientas_ok": set(item["herramientas"]) <= usadas,
        "links_inventados": sorted(links - codigos_devueltos),
        "codigos_inventados": sorted(codigos - codigos_devueltos),
        "citas_inventadas": sorted(citas - articulos_devueltos),
        "sin_rut": not RUT.search(texto),
        "esperado": (all(c in texto for c in item["codigos"])
                     and all(t.lower() in texto.lower() for t in item["contiene"])),
    }
    if item.get("sin_links"):
        fila["sin_links"] = not links
    return fila


# ---------- 2. El juez ----------

class VeredictoAgente(BaseModel):
    fiel: bool
    afirmaciones_sin_respaldo: list[str]
    responde: bool
    cumple_criterio: bool
    comentario: str


INSTRUCCIONES_JUEZ = """Eres un evaluador estricto de un agente que responde sobre compras públicas de Chile usando herramientas (licitaciones de una base de datos y artículos de la ley).

Recibirás: las instrucciones que tenía el agente, la pregunta, cada llamada que hizo a sus herramientas (con sus argumentos y lo que devolvió), su respuesta y un criterio específico.

Evalúa:
- fiel: true solo si todo lo que afirma la respuesta está respaldado. Cuenta como respaldado:
  (a) los datos que devolvieron las herramientas;
  (b) lo que el agente sabe por sus instrucciones (por ejemplo, que la base es una muestra);
  (c) lo que el agente hizo (qué buscó y con qué términos, según las llamadas);
  (d) deducciones lógicas VÁLIDAS a partir de esos datos.
  NO cuenta como respaldado ningún hecho (licitación, monto, fecha, estado, proveedor, regla legal) que no esté en los datos, ni una deducción incorrecta. Juzga contra los datos, no contra tu conocimiento. Anota lo que no tenga respaldo en afirmaciones_sin_respaldo.
- responde: true si contesta directamente lo que se preguntó.
- cumple_criterio: true si cumple el criterio específico.
- comentario: una o dos frases con lo más importante."""


def juzgar(client, item: dict, texto: str, llamadas: list[dict]) -> tuple[VeredictoAgente, object]:
    bloques = "\n\n".join(
        f"<llamada herramienta=\"{ll['herramienta']}\" "
        f"argumentos='{json.dumps(ll['argumentos'], ensure_ascii=False)}'>\n{ll['resultado']}\n</llamada>"
        for ll in llamadas
    ) or "(el agente no usó herramientas)"
    contenido = (
        f"<instrucciones_del_agente>\n{a.INSTRUCCIONES}\n</instrucciones_del_agente>\n\n"
        f"<pregunta>{item['pregunta']}</pregunta>\n\n"
        f"<llamadas_a_herramientas>\n{bloques}\n</llamadas_a_herramientas>\n\n"
        f"<respuesta>{texto}</respuesta>\n\n<criterio>{item['criterio']}</criterio>"
    )
    respuesta = client.messages.parse(
        model=MODELO_JUEZ, max_tokens=16000, system=INSTRUCCIONES_JUEZ,
        messages=[{"role": "user", "content": contenido}], output_format=VeredictoAgente,
    )
    return respuesta.parsed_output, respuesta.usage


# ---------- 3. Reconstruir llamadas de una corrida vieja (para --rejuzgar) ----------

PASO = re.compile(r"^(\w+)\((.*)\)$", re.DOTALL)     # "buscar_licitaciones({'texto': 'aseo'})"


def reconstruir(pasos: list[str]) -> list[dict]:
    """Vuelve a ejecutar cada herramienta con los mismos argumentos.

    buscar_licitaciones es SQL puro → resultado idéntico (misma base).
    consultar_normativa pasa por la reescritura con Haiku → puede variar levemente.
    """
    llamadas = []
    for paso in pasos:
        nombre, argumentos_texto = PASO.match(paso).groups()
        argumentos = ast.literal_eval(argumentos_texto)   # como JSON.parse, pero para sintaxis Python
        resultado = a.EJECUTORES[nombre](**argumentos)
        llamadas.append({"herramienta": nombre, "argumentos": argumentos,
                         "resultado": resultado if isinstance(resultado, str)
                         else json.dumps(resultado, ensure_ascii=False)})
    return llamadas


# ---------- 4. Correr ----------

def costo(modelo: str, entrada: int, salida: int) -> float:
    precio_entrada, precio_salida = PRECIOS[modelo]
    return entrada / 1e6 * precio_entrada + salida / 1e6 * precio_salida


def imprimir_resumen(filas: list[dict], gasto: float) -> None:
    n = len(filas)
    cuenta = lambda f: f"{sum(f(x) for x in filas)}/{n}"
    print("Resumen")
    print(f"  herramientas correctas:   {cuenta(lambda x: x['herramientas_ok'])}")
    print(f"  sin links inventados:     {cuenta(lambda x: not x['links_inventados'])}")
    print(f"  sin códigos inventados:   {cuenta(lambda x: not x['codigos_inventados'])}")
    print(f"  sin citas inventadas:     {cuenta(lambda x: not x['citas_inventadas'])}")
    print(f"  sin RUT:                  {cuenta(lambda x: x['sin_rut'])}")
    con_regla = [x for x in filas if "sin_links" in x]
    if con_regla:
        print(f"  sin links (cuando aplica): {sum(x['sin_links'] for x in con_regla)}/{len(con_regla)}")
    print(f"  esperado:                 {cuenta(lambda x: x['esperado'])}")
    print(f"  fieles:                   {cuenta(lambda x: x['fiel'])}")
    print(f"  responden:                {cuenta(lambda x: x['responde'])}")
    print(f"  cumplen criterio:         {cuenta(lambda x: x['cumple_criterio'])}")
    print(f"\nCosto de esta corrida: US${gasto:.3f}")


def main():
    pauta = {p["id"]: p for p in json.loads(PAUTA.read_text(encoding="utf-8"))}
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    if "--rejuzgar" in sys.argv:
        anterior = json.loads(Path(sys.argv[sys.argv.index("--rejuzgar") + 1]).read_text(encoding="utf-8"))
        # Si la corrida ya guardó las llamadas se usan; si no, se reconstruyen desde los pasos.
        casos = [(pauta[f["id"]], f["respuesta"], f["pasos"], f.get("llamadas") or reconstruir(f["pasos"]))
                 for f in anterior]
        print(f"Re-juzgando {len(casos)} respuestas guardadas (sin correr el agente)...\n")
        gasto, prefijo = 0.0, "agente_rejuzgado"
    else:
        ids = {int(x) for x in sys.argv[1:]}
        casos, gasto, prefijo = [], 0.0, "agente"
        print(f"Evaluando el agente con {len([i for i in pauta if not ids or i in ids])} preguntas...\n")
        for item in pauta.values():
            if ids and item["id"] not in ids:
                continue
            r = preguntar(item["pregunta"])
            gasto += costo("opus", r["tokens_entrada"], r["tokens_salida"])
            casos.append((item, r["texto"], r["pasos"], r["llamadas"]))

    filas = []
    for item, texto, pasos, llamadas in casos:
        auto = revisar(item, texto, llamadas)
        veredicto, uso = juzgar(client, item, texto, llamadas)
        gasto += costo("sonnet", uso.input_tokens, uso.output_tokens)
        filas.append({"id": item["id"], "tipo": item["tipo"], "pregunta": item["pregunta"], "pasos": pasos,
                      "respuesta": texto, "llamadas": llamadas, **auto, "fiel": veredicto.fiel,
                      "responde": veredicto.responde, "cumple_criterio": veredicto.cumple_criterio,
                      "juez": veredicto.model_dump()})

        si_no = lambda b: "sí" if b else "NO"
        print(f"#{item['id']} [{item['tipo']}] {item['pregunta']}")
        print(f"   pasos: {pasos}")
        print(f"   herramientas {si_no(auto['herramientas_ok'])} | links inventados {auto['links_inventados'] or 'no'} | "
              f"códigos inventados {auto['codigos_inventados'] or 'no'} | citas inventadas {auto['citas_inventadas'] or 'no'} | "
              f"sin RUT {si_no(auto['sin_rut'])}" + (f" | sin links {si_no(auto['sin_links'])}" if "sin_links" in auto else ""))
        print(f"   esperado {si_no(auto['esperado'])} | fiel {si_no(veredicto.fiel)} | responde {si_no(veredicto.responde)} | "
              f"criterio {si_no(veredicto.cumple_criterio)}")
        print(f"   juez: {veredicto.comentario}\n")

    imprimir_resumen(filas, gasto)
    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    archivo = CARPETA_RESULTADOS / f"{prefijo}_{datetime.now():%Y%m%d_%H%M}.json"
    archivo.write_text(json.dumps(filas, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Detalle guardado en {archivo}")


if __name__ == "__main__":
    main()
