"""Tests de la API (app/main.py). Ejecutar desde backend:  python -m pytest -v

No llaman a Claude, Voyage ni Postgres: las funciones que los usan se reemplazan por
falsas (mocks). Costo: US$0, y corren igual sin internet.

TestClient = un "navegador falso": hace peticiones HTTP a la app directo en memoria,
sin levantar uvicorn ni abrir el puerto 8000 (parecido a supertest en Node).
"""
from unittest.mock import patch

import anthropic
import httpx
import psycopg
import pytest
import voyageai.error
from fastapi.testclient import TestClient
from langgraph.errors import GraphRecursionError

from app.main import app

ENDPOINTS = ["/preguntar", "/agente"]

# Dónde se parcha cada función (ver diario 8.5: se parcha donde se BUSCA el nombre).
#   main.py hace `from scripts.preguntar import responder` → main tiene su propia copia del nombre.
#   main.py hace `agente_langgraph.preguntar(...)`        → se busca en el módulo en cada llamada.
FUNCION_DE = {
    "/preguntar": "app.main.responder",
    "/agente": "scripts.agente_langgraph.preguntar",
}


# @pytest.fixture = algo que se prepara antes de cada test que lo pida por nombre
# (como beforeEach en Jest, pero el test recibe el resultado como argumento).
@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Entradas malas → 422, SIN llamar a la IA
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("cuerpo", [
    {},                         # falta el campo "pregunta"
    {"pregunta": ""},           # vacía
    {"pregunta": "     "},      # solo espacios (str_strip_whitespace la deja en "")
    {"pregunta": "ab"},         # menos de 3 caracteres
    {"pregunta": "a" * 501},    # más de 500 caracteres
    {"pregunta": 123},          # no es texto
])
def test_entrada_invalida_da_422_sin_llamar_a_la_ia(client, endpoint, cuerpo):
    # `with patch(...) as falsa`: mientras dure el bloque, la función real queda
    # reemplazada por un objeto falso que registra si alguien lo llamó.
    with patch(FUNCION_DE[endpoint]) as falsa:
        r = client.post(endpoint, json=cuerpo)

    assert r.status_code == 422
    falsa.assert_not_called()   # lo importante: con una entrada mala no se gasta ni un token


# ---------------------------------------------------------------------------
# 2. Dependencias caídas → 502 / 503 / 500
# ---------------------------------------------------------------------------

PREGUNTA_OK = {"pregunta": "¿Cuándo puedo comprar sin licitar?"}

# Errores reales de cada librería, como los lanzaría el servicio caído.
# El texto interno ("secreto-interno") NO debe llegar al cliente: se revisa abajo.
CLAUDE_CAIDO = anthropic.APIConnectionError(
    message="secreto-interno", request=httpx.Request("POST", "https://api.anthropic.com"))
VOYAGE_CAIDO = voyageai.error.APIConnectionError("secreto-interno")
POSTGRES_CAIDO = psycopg.OperationalError("secreto-interno: connection refused")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("error, codigo", [
    (CLAUDE_CAIDO, 502),     # 502 = un servicio del que dependo respondió mal
    (VOYAGE_CAIDO, 502),
    (POSTGRES_CAIDO, 503),   # 503 = no puedo atender ahora (la base no está)
])
def test_dependencia_caida_da_codigo_correcto(client, endpoint, error, codigo):
    # side_effect=error → cuando alguien llame a la función falsa, en vez de devolver
    # algo, LANZA ese error (como mockImplementation(() => { throw error }) en Jest).
    with patch(FUNCION_DE[endpoint], side_effect=error) as falsa:
        r = client.post(endpoint, json=PREGUNTA_OK)

    assert r.status_code == codigo
    falsa.assert_called_once_with(PREGUNTA_OK["pregunta"])   # sí se intentó, una vez, con la pregunta
    assert "secreto-interno" not in r.text                   # el detalle interno no se filtra al cliente
    assert set(r.json()) == {"detail"}                       # forma ErrorDetalle: {"detail": "..."}


def test_agente_sin_terminar_da_500(client):
    # Solo /agente tiene este caso: LangGraph lanza GraphRecursionError al pasar MAX_VUELTAS.
    with patch(FUNCION_DE["/agente"], side_effect=GraphRecursionError("secreto-interno")):
        r = client.post("/agente", json=PREGUNTA_OK)

    assert r.status_code == 500
    assert "secreto-interno" not in r.text
    assert "más acotada" in r.json()["detail"]   # mensaje útil para el usuario


# ---------------------------------------------------------------------------
# 3. Caso feliz: la función falsa DEVUELVE algo y revisamos cómo lo transforma la API
# ---------------------------------------------------------------------------

def test_preguntar_ok_transforma_fuentes(client):
    # Misma forma que devuelve responder() de verdad (scripts/preguntar.py).
    # Cada artículo es una tupla: (norma, artículo, transitorio, texto, similitud).
    falso = {
        "texto": "Puedes usar trato directo cuando...",
        "rechazo": False,
        "articulos": [
            ("ley_19886", 8, False, "texto largo del artículo...", 0.81),
            ("reglamento_dto_661", "10 bis", True, "otro texto...", 0.77),
        ],
        "tokens_entrada": 2500,
        "tokens_salida": 300,
    }
    # return_value=falso → la función falsa devuelve eso (Jest: mockReturnValue(falso)).
    with patch(FUNCION_DE["/preguntar"], return_value=falso):
        r = client.post("/preguntar", json=PREGUNTA_OK)

    assert r.status_code == 200
    assert r.json() == {
        "respuesta": "Puedes usar trato directo cuando...",
        "rechazada": False,
        "fuentes": [
            # el código interno "ley_19886" se muestra con su nombre legible; 8 (int) pasa a "8"
            {"norma": "Ley 19.886", "articulo": "8", "transitorio": False},
            {"norma": "Reglamento (Decreto 661)", "articulo": "10 bis", "transitorio": True},
        ],
    }
    # Comparar el JSON completo con == también prueba lo que NO debe salir:
    # ni tokens, ni el texto de los artículos, ni la similitud.


def test_agente_ok_devuelve_pasos_sin_resultados_crudos(client):
    falso = {
        "texto": "Encontré 2 licitaciones de computadores...",
        "llamadas": [
            {"herramienta": "buscar_licitaciones", "argumentos": {"texto": "computadores"},
             "resultado": "[resultado crudo muy largo]"},
            {"herramienta": "consultar_normativa", "argumentos": {"pregunta": "¿plazo mínimo?"},
             "resultado": "[otro resultado crudo]"},
        ],
        "llamadas_a_claude": 3,
        "tokens_entrada": 9000,
        "tokens_salida": 700,
    }
    with patch(FUNCION_DE["/agente"], return_value=falso):
        r = client.post("/agente", json=PREGUNTA_OK)

    assert r.status_code == 200
    assert r.json() == {
        "respuesta": "Encontré 2 licitaciones de computadores...",
        "pasos": [
            {"herramienta": "buscar_licitaciones", "argumentos": {"texto": "computadores"}},
            {"herramienta": "consultar_normativa", "argumentos": {"pregunta": "¿plazo mínimo?"}},
        ],
    }
    assert "resultado crudo" not in r.text


# ---------------------------------------------------------------------------
# 4. /health y /
# ---------------------------------------------------------------------------

def _conexion_falsa(conectar, fila):
    """Arma la cadena falsa `with psycopg.connect(...) as conn: conn.execute(...).fetchone()`.

    patch entrega un MagicMock: un objeto que acepta cualquier atributo o llamada.
    Solo hay que decirle qué devolver al final de la cadena.
    `__enter__` es lo que devuelve el `with ... as conn` (Python lo llama por dentro).
    """
    conn = conectar.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = fila


def test_health_ok(client):
    with patch("app.main.psycopg.connect") as conectar:
        _conexion_falsa(conectar, ("0.8.7",))
        r = client.get("/health")

    assert r.status_code == 200
    assert r.json() == {"db": "ok", "pgvector": "0.8.7"}


def test_health_sin_pgvector_da_503(client):
    with patch("app.main.psycopg.connect") as conectar:
        _conexion_falsa(conectar, None)          # la extensión no está instalada
        r = client.get("/health")

    assert r.status_code == 503
    assert r.json()["pgvector"] == "no instalado"


def test_health_base_caida_da_503(client):
    with patch("app.main.psycopg.connect", side_effect=POSTGRES_CAIDO):
        r = client.get("/health")

    assert r.status_code == 503
    assert r.json() == {"db": "error"}
    assert "secreto-interno" not in r.text


def test_raiz_redirige_a_docs(client):
    # follow_redirects=False: queremos ver la redirección misma, no la página de destino.
    r = client.get("/", follow_redirects=False)

    assert r.status_code == 307
    assert r.headers["location"] == "/docs"
