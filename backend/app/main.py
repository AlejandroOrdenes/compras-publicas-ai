"""Punto de entrada de la API.

Ejecutar (desde la carpeta backend, con el venv activo):
    uvicorn app.main:app --reload
Luego abrir http://localhost:8000/docs  (documentación automática de FastAPI)
"""
import logging
from collections.abc import Callable

import anthropic
import psycopg
import voyageai.error
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from langgraph.errors import GraphRecursionError

from app.config import settings
from app.schemas import (ErrorDetalle, Fuente, PasoAgente, PreguntaEntrada,
                         RespuestaAgente, RespuestaRAG)
from scripts import agente_langgraph
from scripts.preguntar import NOMBRES, responder

logger = logging.getLogger(__name__)

# Errores que FastAPI no puede adivinar (salen de un `raise`), declarados para que aparezcan en /docs.
# Se reusa en todos los endpoints que dependen de la IA y de la base.
ERRORES_DEPENDENCIAS = {
    502: {"model": ErrorDetalle, "description": "Falló un servicio de IA (Claude o Voyage)"},
    503: {"model": ErrorDetalle, "description": "La base de datos no está disponible"},
}

app = FastAPI(title="Compras Públicas AI")


@app.get("/")
def root():
    return {"app": "compras-publicas-ai", "status": "ok"}


@app.get("/health")
def health():
    """Verifica que la API puede hablar con Postgres y que pgvector está activo."""
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
    except psycopg.Error as e:
        logger.error("Health check: la base no responde: %s", e)
        return JSONResponse(status_code=503, content={"db": "error"})

    if row is None:
        return JSONResponse(status_code=503, content={"db": "ok", "pgvector": "no instalado"})

    return {"db": "ok", "pgvector": row[0]}


def _ejecutar(funcion: Callable[[str], dict], pregunta: str) -> dict:
    """Llama a `funcion(pregunta)` y traduce los errores de las dependencias a códigos HTTP.

    Callable[[str], dict] = "una función que recibe un str y devuelve un dict"
    (en TypeScript: (pregunta: string) => object). Pasar una función como argumento
    funciona igual que en JS: sin paréntesis, solo el nombre.
    """
    try:
        return funcion(pregunta)
    except (anthropic.APIError, voyageai.error.VoyageError) as e:
        # Falla un servicio externo: 502 = "el servidor del que dependo respondió mal".
        logger.error("Servicio externo falló: %s", e)
        raise HTTPException(status_code=502, detail="Servicio de IA no disponible, intenta en un rato.")
    except psycopg.Error as e:
        logger.error("La base no responde: %s", e)
        raise HTTPException(status_code=503, detail="Base de datos no disponible.")


# `def` normal (no `async def`) a propósito: responder() es bloqueante (espera a Voyage,
# Postgres y Claude). Con `def`, FastAPI lo corre en un hilo aparte y el servidor sigue
# atendiendo otras peticiones mientras tanto. Con `async def` congelaría todo el servidor.
@app.post("/preguntar", response_model=RespuestaRAG, responses=ERRORES_DEPENDENCIAS)
def preguntar(entrada: PreguntaEntrada):
    """Responde una pregunta sobre la normativa de compras públicas (Ley 19.886 y su reglamento)."""
    r = _ejecutar(responder, entrada.pregunta)

    # Los tokens quedan en el log del servidor (y en LangSmith), no en la respuesta pública.
    logger.info("Pregunta respondida: %s tokens entrada, %s salida", r["tokens_entrada"], r["tokens_salida"])

    return RespuestaRAG(
        respuesta=r["texto"],
        rechazada=r["rechazo"],
        fuentes=[
            Fuente(norma=NOMBRES.get(norma, norma), articulo=str(articulo), transitorio=transitorio)
            for norma, articulo, transitorio, _texto, _similitud in r["articulos"]
        ],
    )


@app.post("/agente", response_model=RespuestaAgente, responses={
    **ERRORES_DEPENDENCIAS,     # ** = "spread" de un diccionario (como {...obj} en JS)
    500: {"model": ErrorDetalle, "description": "El agente superó el máximo de pasos sin terminar"},
})
def agente(entrada: PreguntaEntrada):
    """Agente que combina licitaciones de Mercado Público (muestra guardada) con la normativa.

    Decide solo qué herramientas usar. Los errores DENTRO de una herramienta (ej. Voyage caído)
    no llegan aquí: ToolNode se los entrega a Claude como resultado, y el agente responde con eso.
    """
    try:
        r = _ejecutar(agente_langgraph.preguntar, entrada.pregunta)
    except GraphRecursionError:
        # Pasó MAX_VUELTAS sin dar una respuesta final. Es un problema nuestro, no del usuario → 500.
        logger.error("Agente superó el límite de pasos para: %r", entrada.pregunta[:100])
        raise HTTPException(status_code=500, detail="El agente no logró terminar. Intenta con una pregunta más acotada.")

    logger.info("Agente: %s llamadas a Claude, %s tokens entrada, %s salida",
                r["llamadas_a_claude"], r["tokens_entrada"], r["tokens_salida"])

    return RespuestaAgente(
        respuesta=r["texto"],
        pasos=[PasoAgente(herramienta=ll["herramienta"], argumentos=ll["argumentos"]) for ll in r["llamadas"]],
    )
