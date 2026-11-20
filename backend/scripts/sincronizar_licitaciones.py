"""Worker: copia licitaciones de la API de Mercado Público a Postgres (tabla `licitaciones`).

Ejecutar desde la carpeta backend, con el venv activo y Docker corriendo:
    python -m scripts.sincronizar_licitaciones                    ← 50 licitaciones de hoy (desarrollo)
    python -m scripts.sincronizar_licitaciones --limite 0         ← todas las de hoy (~1.400; correr de noche)
    python -m scripts.sincronizar_licitaciones --fecha 18112026   ← otra fecha (ddmmaaaa)
    python -m scripts.sincronizar_licitaciones --existentes --limite 0  ← re-consultar las ya guardadas

Reglas de la API (chilecompra.cl/api): 10.000 consultas al día por ticket; excederlo
o eludirlo = suspensión. ChileCompra pide descargas masivas entre 22:00 y 07:00.
Por eso: una consulta a la vez, pausa entre consultas, reintentos con espera creciente
y un tope por corrida.

Datos personales: LISTA BLANCA. Solo se copian los campos elegidos en `extraer()`;
nombres, cargos, emails y teléfonos de funcionarios nunca se guardan.
Los textos libres pasan por scripts/filtro_datos_personales.limpiar(), y el proveedor
adjudicado por proveedor_publico() (empresa = público; persona natural = protegido).
"""
import argparse
import json
import time
from datetime import date, datetime

import httpx
import psycopg

from app.config import settings
from scripts.filtro_datos_personales import limpiar, proveedor_publico

BASE = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
PAUSA = 2.0             # segundos entre consultas
REINTENTOS = 3          # por consulta; espera 4 s, 8 s, 16 s entre intentos
TOPE_LLAMADAS = 5_000   # por corrida (la mitad del límite diario): freno de seguridad

CREAR_TABLA = """
CREATE TABLE IF NOT EXISTS licitaciones (
    codigo             TEXT PRIMARY KEY,
    nombre             TEXT,
    descripcion        TEXT,
    estado             TEXT,
    tipo               TEXT,
    organismo          TEXT,
    unidad             TEXT,
    comuna             TEXT,
    region             TEXT,
    monto_estimado     NUMERIC,
    moneda             TEXT,
    fecha_publicacion  TIMESTAMP,
    fecha_cierre       TIMESTAMP,
    fecha_adjudicacion TIMESTAMP,
    items              JSONB,
    sincronizado_en    TIMESTAMP NOT NULL DEFAULT now()
)
"""

COLUMNAS = ["codigo", "nombre", "descripcion", "estado", "tipo", "organismo", "unidad", "comuna",
            "region", "monto_estimado", "moneda", "fecha_publicacion", "fecha_cierre",
            "fecha_adjudicacion", "items"]

# "Upsert": inserta; si el código ya existe, actualiza (las licitaciones cambian de estado).
# Correrlo dos veces deja el mismo resultado → idempotente.
GUARDAR = f"""
INSERT INTO licitaciones ({", ".join(COLUMNAS)})
VALUES ({", ".join(f"%({c})s" for c in COLUMNAS)})
ON CONFLICT (codigo) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNAS[1:])},
    sincronizado_en = now()
"""


class ApiMercadoPublico:
    """Hace las consultas respetando el límite: de a una, con pausa, reintentos y tope."""

    def __init__(self):
        self.http = httpx.Client(timeout=60)
        self.llamadas = 0

    def consultar(self, **params) -> dict | None:
        """Devuelve el JSON de la API, o None si falló después de los reintentos."""
        for intento in range(1, REINTENTOS + 1):
            if self.llamadas >= TOPE_LLAMADAS:
                raise RuntimeError(f"Tope de {TOPE_LLAMADAS} consultas alcanzado: me detengo.")
            time.sleep(PAUSA)
            self.llamadas += 1
            try:
                r = self.http.get(BASE, params={**params, "ticket": settings.mercadopublico_ticket})
                datos = r.json()
                if r.status_code == 200 and "Listado" in datos:
                    return datos
                motivo = f"HTTP {r.status_code}: {str(datos.get('Mensaje', ''))[:100]}"
            except (httpx.HTTPError, ValueError) as error:
                # Solo el TIPO de error: el mensaje completo puede incluir la URL con el ticket.
                motivo = type(error).__name__
            espera = PAUSA * 2 ** intento
            print(f"   intento {intento} falló ({motivo}); espero {espera:.0f} s")
            time.sleep(espera)
        return None


def fecha(texto: str | None) -> datetime | None:
    return datetime.fromisoformat(texto) if texto else None


def extraer(lic: dict) -> dict:
    """LISTA BLANCA: arma la fila solo con los campos elegidos (sin datos personales)."""
    comprador = lic.get("Comprador") or {}
    fechas = lic.get("Fechas") or {}
    items = [
        {
            "categoria": item.get("Categoria"),
            "producto": limpiar(item.get("NombreProducto")),
            "descripcion": limpiar(item.get("Descripcion")),
            "cantidad": item.get("Cantidad"),
            "unidad": item.get("UnidadMedida"),
            "monto_unitario_adjudicado": (item.get("Adjudicacion") or {}).get("MontoUnitario"),
            # Proveedor: nombre y RUT si es empresa; "Persona natural (dato protegido)" si no.
            **proveedor_publico((item.get("Adjudicacion") or {}).get("RutProveedor"),
                                (item.get("Adjudicacion") or {}).get("NombreProveedor")),
        }
        for item in (lic.get("Items") or {}).get("Listado") or []
    ]
    return {
        "codigo": lic["CodigoExterno"],
        # Textos libres: pasan por el filtro de datos personales (emails, RUT, teléfonos, nombres con título).
        "nombre": limpiar(lic.get("Nombre")),
        "descripcion": limpiar(lic.get("Descripcion")),
        "estado": lic.get("Estado"),
        "tipo": lic.get("Tipo"),
        "organismo": comprador.get("NombreOrganismo"),
        "unidad": comprador.get("NombreUnidad"),
        "comuna": comprador.get("ComunaUnidad"),
        "region": (comprador.get("RegionUnidad") or "").strip() or None,
        "monto_estimado": lic.get("MontoEstimado"),
        "moneda": lic.get("Moneda"),
        "fecha_publicacion": fecha(fechas.get("FechaPublicacion")),
        "fecha_cierre": fecha(fechas.get("FechaCierre")),
        "fecha_adjudicacion": fecha(fechas.get("FechaAdjudicacion")),
        "items": json.dumps(items, ensure_ascii=False),
    }


def main():
    parser = argparse.ArgumentParser(description="Sincroniza licitaciones de Mercado Público a Postgres")
    parser.add_argument("--fecha", default=date.today().strftime("%d%m%Y"), help="ddmmaaaa (por defecto: hoy)")
    parser.add_argument("--limite", type=int, default=50, help="cuántas licitaciones (0 = todas)")
    parser.add_argument("--existentes", action="store_true",
                        help="volver a consultar las licitaciones que ya están guardadas (ignora --fecha)")
    args = parser.parse_args()

    inicio = time.time()
    api = ApiMercadoPublico()
    if args.existentes:
        # Ponerse al día: cambios de estado (Publicada → Adjudicada) o campos nuevos.
        with psycopg.connect(settings.database_url) as conn:
            codigos = [fila[0] for fila in conn.execute("SELECT codigo FROM licitaciones ORDER BY codigo")]
        print(f"Re-sincronizando {len(codigos)} licitaciones ya guardadas.")
    else:
        lista = api.consultar(fecha=args.fecha)
        if lista is None:
            print("No se pudo obtener la lista de licitaciones.")
            return
        codigos = [lic["CodigoExterno"] for lic in lista["Listado"]]
        print(f"{lista['Cantidad']} licitaciones el {args.fecha}.")
    if args.limite:
        codigos = codigos[:args.limite]
    print(f"Sincronizando {len(codigos)}.")

    guardadas, fallidas = 0, []
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(CREAR_TABLA)
        for numero, codigo in enumerate(codigos, start=1):
            detalle = api.consultar(codigo=codigo)
            if not detalle or not detalle["Listado"]:
                fallidas.append(codigo)
                continue
            fila = extraer(detalle["Listado"][0])
            conn.execute(GUARDAR, fila)
            conn.commit()        # una por una: si se corta a la mitad, lo guardado queda guardado
            guardadas += 1
            print(f"[{numero}/{len(codigos)}] {codigo} | {fila['estado']} | {(fila['nombre'] or '')[:55]}")

    minutos = (time.time() - inicio) / 60
    print(f"\nGuardadas: {guardadas} | fallidas: {len(fallidas)} {fallidas or ''}")
    print(f"Consultas usadas: {api.llamadas} de 10.000 diarias | tiempo: {minutos:.1f} min")


if __name__ == "__main__":
    main()
