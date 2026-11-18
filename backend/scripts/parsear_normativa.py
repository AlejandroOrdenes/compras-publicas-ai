"""Convierte los XML de LeyChile en una lista de artículos (los "chunks" del RAG).

Ejecutar desde la carpeta backend, con el venv activo:
    python -m scripts.parsear_normativa

Requiere haber corrido antes: python -m scripts.descargar_normativa
"""
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CARPETA = ROOT / "data" / "normativa"

# El XML declara xmlns="http://www.leychile.cl/esquemas", así que TODAS sus
# etiquetas viven en ese "namespace". ElementTree las nombra con el namespace
# entre llaves: "{http://www.leychile.cl/esquemas}Texto", no solo "Texto".
NS = "{http://www.leychile.cl/esquemas}"


def parsear(ruta: Path) -> list[dict]:
    """Devuelve un diccionario por artículo de la norma.

    Estructura del XML (simplificada):
        <EstructuraFuncional tipoParte="Capítulo">         contenedor, se omite
          <EstructuraFuncional tipoParte="Artículo" transitorio="...">
            <Texto>Artículo 6º.- Las bases ...</Texto>
            <Metadatos><NombreParte>6</NombreParte></Metadatos>

    Decisiones:
    - Los artículos están anidados a distinta profundidad (capítulos, párrafos),
      por eso se recorre con iter() y se filtra por tipoParte.
    - Los transitorios se incluyen pero se marcan: comparten numeración con los
      permanentes (hay dos "Artículo 1") y no deben confundirse al citar.
    - El texto se normaliza a una línea: el XML trae saltos cada ~60 caracteres.

    Pendiente: normalizar números ("4°", "primero"), subdividir artículos muy
    largos (> 4.000 caracteres) y definir qué hacer con artículos derogados.
    """
    raiz = ET.parse(ruta).getroot()
    resultado = []
    for art in raiz.iter(NS + "EstructuraFuncional"):
        if art.get("tipoParte") == "Artículo":
            resultado.append({
                "norma": ruta.stem,
                "articulo": art.find(NS + "Metadatos").find(NS + "NombreParte").text.strip(),
                "transitorio": art.get("transitorio") == "transitorio",
                "texto": " ".join(art.find(NS + "Texto").text.split()),
            })
    return resultado


def main():
    for ruta in sorted(CARPETA.glob("*.xml")):
        articulos = parsear(ruta)
        largos = [len(a["texto"]) for a in articulos]
        print(f"{ruta.stem:<20} {len(articulos):>4} artículos | "
              f"caracteres: mín {min(largos)}, máx {max(largos)}, "
              f"promedio {sum(largos) // len(largos)}")
        print(f"  ejemplo: {articulos[0]}\n"[:300])


if __name__ == "__main__":
    main()
