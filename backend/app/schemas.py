"""Forma de los datos que entran y salen de la API (modelos Pydantic).

FastAPI usa estas clases para:
- validar el JSON que llega (si no cumple, responde 422 sin ejecutar nuestro código),
- convertir la respuesta a JSON,
- generar la documentación automática en /docs.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PreguntaEntrada(BaseModel):
    # Quita espacios al inicio y al final ANTES de validar: "     " queda en "" y falla min_length.
    model_config = ConfigDict(str_strip_whitespace=True)

    # min_length=3 evita preguntas vacías o "a".
    # max_length=500 limita el costo: una pregunta gigante = más tokens = más plata.
    pregunta: str = Field(min_length=3, max_length=500, examples=["¿Cuándo puedo comprar sin licitar?"])


class Fuente(BaseModel):
    norma: str          # "Ley 19.886" o "Reglamento (Decreto 661)"
    articulo: str       # str y no int: existen artículos como "10 bis"
    transitorio: bool


class ErrorDetalle(BaseModel):
    # Misma forma que produce HTTPException: {"detail": "mensaje"}
    detail: str


class RespuestaRAG(BaseModel):
    respuesta: str
    rechazada: bool     # True si un filtro de seguridad de Claude no dejó responder
    fuentes: list[Fuente]


class PasoAgente(BaseModel):
    herramienta: str            # "buscar_licitaciones" o "consultar_normativa"
    argumentos: dict[str, Any]  # dict[str, Any] = objeto con claves texto y valores de cualquier tipo
                                # (en TypeScript: Record<string, unknown>)


class RespuestaAgente(BaseModel):
    respuesta: str
    pasos: list[PasoAgente]     # qué herramientas usó, en orden (transparencia: se ve cómo llegó a la respuesta)
