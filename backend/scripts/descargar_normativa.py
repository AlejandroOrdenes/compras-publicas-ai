"""Descarga el texto oficial y vigente de la normativa de compras públicas desde LeyChile (BCN).

Ejecutar desde la carpeta backend, con el venv activo:
    python -m scripts.descargar_normativa

Guarda un XML por norma en data/normativa/. LeyChile entrega el texto ya separado
por artículo, lo que facilita cortarlo en trozos ("chunks") en el paso siguiente.
"""
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESTINO = ROOT / "data" / "normativa"

# idNorma = identificador interno de cada norma en LeyChile.
NORMAS = {
    "ley_19886": 213004,          # Ley 19.886 (texto consolidado, incluye cambios de Ley 21.634)
    "reglamento_dto_661": 1209290,  # Decreto 661/2024 de Hacienda, reglamento vigente
}

URL = "https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}"
USER_AGENT = "compras-publicas-ai (proyecto de aprendizaje)"


def descargar(nombre: str, id_norma: int) -> Path:
    req = urllib.request.Request(URL.format(id_norma=id_norma), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        contenido = resp.read()

    if not contenido.lstrip().startswith(b"<?xml"):
        raise ValueError(f"{nombre}: la respuesta no es XML (¿cambió el servicio de LeyChile?)")

    ruta = DESTINO / f"{nombre}.xml"
    ruta.write_bytes(contenido)
    return ruta


def main():
    DESTINO.mkdir(parents=True, exist_ok=True)
    for i, (nombre, id_norma) in enumerate(NORMAS.items()):
        if i > 0:
            time.sleep(2)  # ser amable con el servidor: una petición a la vez
        ruta = descargar(nombre, id_norma)
        print(f"OK  {nombre:<20} {ruta.stat().st_size / 1024:>6.0f} KB  -> {ruta.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
