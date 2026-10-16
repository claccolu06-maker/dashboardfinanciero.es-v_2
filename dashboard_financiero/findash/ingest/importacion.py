"""Categorización y volcado a base de datos con deduplicación inteligente."""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..categorias import (
    CATEGORIAS_GASTO, CATEGORIAS_INGRESO, CATEGORIAS_LEGACY, categorizar_movimiento,
)
from .modelos import Movimiento

_COLUMNAS = ("usuario_id", "fecha", "concepto", "categoria", "importe", "tipo")
_CATEGORIAS_CONOCIDAS = set(CATEGORIAS_GASTO) | set(CATEGORIAS_INGRESO) | set(CATEGORIAS_LEGACY)


def categorizar_lote(movimientos: Sequence[Movimiento],
                     reglas_gasto: Optional[Mapping[str, str]] = None,
                     reglas_ingreso: Optional[Mapping[str, str]] = None) -> None:
    """Asigna ``categoria`` a cada movimiento (respetando las reglas aprendidas).

    Si el archivo ya traía una categoría que pertenece al catálogo de FinDash
    (p. ej. una exportación propia), se conserva.
    """
    for m in movimientos:
        if m.categoria and m.categoria in _CATEGORIAS_CONOCIDAS:
            continue
        reglas = reglas_ingreso if m.tipo == "ingreso" else reglas_gasto
        m.categoria = categorizar_movimiento(m.concepto, m.tipo, reglas)


def procesar_importacion(db: Any, usuario_id: int, movimientos: Sequence[Movimiento],
                         dry_run: bool = False) -> Dict[str, Any]:
    """Inserta los movimientos nuevos y omite los ya importados.

    Consulta los movimientos del usuario entre ``f_min`` y ``f_max`` y usa un
    ``Counter`` sobre ``(fecha, concepto, importe, tipo)``. Así, importar dos
    veces el mismo extracto no duplica nada, pero dos compras legítimas idénticas
    del mismo día (dos cafés iguales) se importan las dos la primera vez.

    Todo el volcado se hace en una única transacción: o entra todo o nada.
    """
    if not movimientos:
        return {"insertados": 0, "duplicados": 0, "nuevos": [], "fecha_min": None, "fecha_max": None}

    f_min = min(m.fecha_iso for m in movimientos)
    f_max = max(m.fecha_iso for m in movimientos)
    existentes = db.query(
        "SELECT fecha, concepto, importe, tipo FROM movimientos "
        "WHERE usuario_id = ? AND fecha >= ? AND fecha <= ?",
        (usuario_id, f_min, f_max),
    )
    restantes: Counter = Counter()
    for fila in existentes:
        restantes[Movimiento(_a_fecha(fila["fecha"]), fila["concepto"], float(fila["importe"]),
                             fila["tipo"]).clave()] += 1

    nuevos: List[Movimiento] = []
    duplicados = 0
    for m in movimientos:
        clave = m.clave()
        if restantes[clave] > 0:
            restantes[clave] -= 1
            duplicados += 1
        else:
            nuevos.append(m)

    insertados = 0
    if nuevos and not dry_run:
        filas = [(usuario_id, m.fecha_iso, m.concepto, m.categoria or "Gastos Generales",
                  round(m.importe, 2), m.tipo) for m in nuevos]
        with db.transaction():
            insertados = db.insert_many("movimientos", _COLUMNAS, filas)
    return {
        "insertados": insertados,
        "duplicados": duplicados,
        "nuevos": nuevos,
        "fecha_min": f_min,
        "fecha_max": f_max,
    }


def _a_fecha(iso: str):
    import datetime
    return datetime.date.fromisoformat(str(iso)[:10])
