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


# ---------------------------------------------------------------------------
# TU TAREA: implementar parsear(ruta)
#
# Debe devolver una lista de diccionarios, uno por artículo:
#   [{"norma": "ley_19886", "articulo": "6", "transitorio": False,
#     "texto": "Artículo 6º.- Las bases de licitación deberán ..."}, ...]
#
# Cómo viene el XML (simplificado):
#   <Norma>
#     <EstructurasFuncionales>
#       <EstructuraFuncional tipoParte="Capítulo" ...>        <- contenedor
#         <EstructuraFuncional tipoParte="Artículo"           <- lo que buscas
#                              transitorio="no transitorio"
#                              derogado="no derogado">
#           <Texto> Artículo 6º.- Las bases ...</Texto>
#           <Metadatos>
#             <NombreParte presente="si">6</NombreParte>     <- número limpio
#           </Metadatos>
#
# Pistas:
#   1. ET.parse(ruta).getroot() entrega la raíz <Norma>.
#   2. Los artículos están ANIDADOS dentro de capítulos y párrafos, a distinta
#      profundidad. raiz.iter(NS + "EstructuraFuncional") los recorre todos,
#      sin importar el nivel (find/findall solo miran un nivel).
#   3. Atributos: elem.get("tipoParte"). Hijo directo: elem.find(NS + "Texto").
#      Texto dentro de una etiqueta: .text (puede ser None, ojo).
#   4. Quédate solo con tipoParte == "Artículo".
#   5. Limpia el texto: viene con saltos de línea cada ~60 caracteres
#      (el XML imita el ancho del Diario Oficial). " ".join(texto.split())
#      junta todo en una línea con espacios simples.
#
# Casos raros que vas a encontrar (decide qué hacer y por qué):
#   - Números no estándar: "1°", "13 bis", "primero", "Segundo".
#   - Transitorios: la ley tiene 11, el reglamento 5. ¿Los incluyes? ¿Cómo los
#     distingues, si el reglamento tiene un "Artículo 1" normal Y uno transitorio?
#   - Derogados: hoy hay 0, pero el XML lo marca (derogado="derogado"). ¿Qué
#     debería pasar si en una versión futura aparecen?
#   - Artículos muy largos: ¿hay alguno que sea demasiado largo para un solo
#     chunk? (mide len(texto) y mira el máximo)
# ---------------------------------------------------------------------------

def parsear(ruta: Path) -> list[dict]:
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
