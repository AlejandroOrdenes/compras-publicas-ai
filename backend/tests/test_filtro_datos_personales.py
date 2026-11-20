"""Tests del filtro de datos personales. Ejecutar desde backend:  python -m pytest -v

pytest busca funciones que empiezan con test_ y revisa cada `assert`
(como `expect(...).toBe(...)` en Jest). Los datos de ejemplo son inventados.
"""
import pytest

from scripts.filtro_datos_personales import limpiar

# @pytest.mark.parametrize = correr el mismo test con varios casos (como test.each en Jest).


@pytest.mark.parametrize("entrada, esperado", [
    # Emails
    ("Consultas a juan.perez@municipio.cl", "Consultas a [EMAIL]"),
    # RUTs: con puntos, sin puntos, con K
    ("RUT 12.345.678-9 del oferente", "RUT [RUT] del oferente"),
    ("rut 12345678-K", "rut [RUT]"),
    ("RUT 7.654.321-0", "RUT [RUT]"),
    # Teléfonos: celular, Santiago, regiones, con +56
    ("fono 9 1234 5678", "fono [TELÉFONO]"),
    ("cel +56 9 8765 4321", "cel [TELÉFONO]"),
    ("teléfono 22 345 6789", "teléfono [TELÉFONO]"),
    ("llamar al (32) 234 5678", "llamar al [TELÉFONO]"),
    ("fono 912345678.", "fono [TELÉFONO]."),
    # Nombres con título (se conserva el título)
    ("encargado Sr. Juan Pérez", "encargado Sr. [NOMBRE]"),
    ("contactar a Doña María José Soto.", "contactar a Doña [NOMBRE]."),
])
def test_tapa_datos_personales(entrada, esperado):
    assert limpiar(entrada) == esperado


@pytest.mark.parametrize("texto", [
    # Falsos positivos que NO deben taparse
    "Monto estimado $912.345.678",               # montos usan puntos
    "Presupuesto de 1.416.050.088 pesos",
    "Código de producto 72131502",               # 8 dígitos, no empieza como teléfono
    "Licitación 1048-75-LE26",                   # código de licitación
    "Ley N° 19.886, art. 8 bis",
    "Entrega en Puerto Montt y Don Bosco 123",   # "Don Bosco" es calle... (ver test de abajo)
    "ID 1057390-21-LR26 por 2.500 UTM",
    "vinculada al proyecto 1234567-8-PC26//INSUMOS",   # código de proyecto (visto en datos reales)
])
def test_no_tapa_lo_que_no_es_personal(texto):
    # "Don Bosco" SÍ lo tapa (Don + palabra con mayúscula): falso positivo aceptado,
    # preferimos tapar de más que de menos. Se excluye de esta comparación.
    if "Don Bosco" in texto:
        assert limpiar(texto) == "Entrega en Puerto Montt y Don [NOMBRE] 123"
    else:
        assert limpiar(texto) == texto


def test_vacio_y_none():
    assert limpiar("") == ""
    assert limpiar(None) is None
