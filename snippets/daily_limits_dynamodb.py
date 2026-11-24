"""Límites de uso diarios: por persona (IP) y global. Protegen el gasto en Claude.

Los contadores viven en DynamoDB (no en memoria): cada contenedor de Lambda tiene su
propia memoria y se borra al apagarse, así que un contador en memoria no serviría.

Cada pregunta hace, en una sola operación atómica: "suma 1 al contador SOLO si está
bajo el límite". Si ya llegó, DynamoDB no suma y la API responde 429 sin llamar a Claude.

En el PC (sin LIMITES_TABLA definida) no se aplica ningún límite.
"""
import hashlib
from datetime import UTC, datetime

from fastapi import HTTPException, Request

from app.config import settings

DOS_DIAS = 2 * 24 * 3600
_tabla = None   # se crea la primera vez que se usa y se reutiliza


def _tabla_dynamo():
    global _tabla
    if _tabla is None:
        import boto3

        _tabla = boto3.resource("dynamodb", region_name=settings.aws_region).Table(settings.limites_tabla)
    return _tabla


def _sumar_si_hay_cupo(clave: str, limite: int, expira: int) -> bool:
    """Suma 1 al contador `clave` si está bajo `limite`. Devuelve False si ya llegó."""
    from botocore.exceptions import ClientError

    if limite <= 0:   # límite 0 = nadie pasa (sirve para apagar la demo sin tocar el código)
        return False
    try:
        _tabla_dynamo().update_item(
            Key={"id": clave},
            # ADD crea el contador en 0 si no existe y le suma 1. expira = cuándo lo borra el TTL.
            UpdateExpression="ADD cuenta :uno SET expira = :expira",
            ConditionExpression="attribute_not_exists(cuenta) OR cuenta < :limite",
            ExpressionAttributeValues={":uno": 1, ":limite": limite, ":expira": expira},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def huella_ip(ip: str) -> str:
    """Código de 16 caracteres que identifica a la IP sin guardarla (hash con sal secreta)."""
    return hashlib.sha256(f"{settings.limites_sal}:{ip}".encode()).hexdigest()[:16]


def verificar_limite(request: Request, tipo: str = "pregunta") -> None:
    """Llamar al inicio de cada endpoint que gaste en IA. Lanza 429 si se superó un límite.

    tipo="pregunta": /preguntar y /agente (usan Claude: cupo chico).
    tipo="busqueda": /buscar (solo Voyage, ~100 tokens: cupo grande y contadores aparte,
    así buscar no gasta el cupo de preguntas).
    """
    if not settings.limites_tabla:
        return
    if tipo == "busqueda":
        prefijo, limite_ip, limite_global, palabra = "busqueda#", settings.limite_busqueda_ip_dia, \
            settings.limite_busqueda_global_dia, "búsquedas"
    else:
        prefijo, limite_ip, limite_global, palabra = "", settings.limite_ip_dia, settings.limite_global_dia, "preguntas"
    ahora = datetime.now(UTC)
    hoy = ahora.strftime("%Y-%m-%d")          # día en UTC: se reinicia a las 21:00 de Chile (verano)
    expira = int(ahora.timestamp()) + DOS_DIAS
    ip = request.client.host if request.client else "desconocida"

    # Primero la persona: si ya llegó a su límite, no consume cupo del contador global.
    if not _sumar_si_hay_cupo(f"{prefijo}ip#{huella_ip(ip)}#{hoy}", limite_ip, expira):
        raise HTTPException(status_code=429, detail=f"Llegaste al máximo de {palabra} por hoy. Vuelve mañana.")
    if not _sumar_si_hay_cupo(f"{prefijo}global#{hoy}", limite_global, expira):
        raise HTTPException(status_code=429, detail=f"La demo llegó a su máximo de {palabra} por hoy. Vuelve mañana.")
