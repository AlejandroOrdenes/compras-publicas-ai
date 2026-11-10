"""Punto de entrada de la API.

Ejecutar (desde la carpeta backend, con el venv activo):
    uvicorn app.main:app --reload
Luego abrir http://localhost:8000/docs  (documentación automática de FastAPI)
"""
import logging

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

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
