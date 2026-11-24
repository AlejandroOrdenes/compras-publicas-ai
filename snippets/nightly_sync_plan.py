def clasificar(activas: dict[str, datetime | None], guardadas: dict[str, tuple], cerradas: list[str],
               ahora: datetime, max_nuevas: int = MAX_NUEVAS,
               max_sin_ficha: int = MAX_SIN_FICHA) -> dict[str, list[str]]:
    """Decide qué consultar. Sin base ni API: se puede probar con datos inventados.

    activas:   {codigo: fecha_cierre} según la lista de abiertas de la API.
    guardadas: {codigo: (estado, fecha_cierre, ficha_leida_en)} según nuestra base.
    cerradas:  códigos en estado Cerrada, los más desactualizados primero.
    """
    faltan = [c for c in activas if c not in guardadas]
    # Más tiempo para postular primero: son las más útiles para quien busca (sin fecha, al final).
    faltan.sort(key=lambda c: activas[c] or datetime.min, reverse=True)
    cambiaron = [c for c, cierre in activas.items()
                 if c in guardadas and (guardadas[c][0] != "Publicada" or guardadas[c][1] != cierre)]
    # Abiertas sin cambios pero sin ficha (guardadas antes del lector) o con ficha vieja.
    sin_ficha = [c for c in activas if c in guardadas and c not in cambiaron
                 and (guardadas[c][2] is None or ahora - guardadas[c][2] > RELEER_FICHA_CADA)]
    dejaron_de_estar_abiertas = [c for c, (estado, *_) in guardadas.items()
                                 if estado == "Publicada" and c not in activas]
    return {
        "cambiaron": cambiaron,
        "dejaron_de_estar_abiertas": dejaron_de_estar_abiertas,
        "sin_ficha": sin_ficha[:max_sin_ficha],
        "cerradas": [c for c in cerradas if c not in activas],
        "nuevas": faltan[:max_nuevas],
        "faltan_en_total": faltan,
    }


