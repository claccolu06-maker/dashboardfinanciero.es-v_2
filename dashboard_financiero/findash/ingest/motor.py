"""Reconstructor Universal de Datos.

Orquesta las estrategias y elige la mejor:

1. ``_parseador_francotirador``  texto de banca online con ``[CONCEPTO] [FECHA] [IMPORTE] [SALDO]``.
2. ``_intentar_tabla``           CSV / Excel / columnas con delimitador.
3. ``_estrategia_lineas``        operaciones repartidas en varias líneas (modos A y C).
4. ``_estrategia_difusa``        último recurso para texto desestructurado (PDF).

Cada estrategia devuelve un :class:`ResultadoParcial` con una *calidad*
(proporción de filas válidas × fiabilidad de la estrategia × bonus si los
saldos cuadran). Se elige la de mayor calidad; el francotirador, por ser de alta
precisión, gana directamente si reconoce el texto.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import List, Optional, Sequence

from .francotirador import _parseador_francotirador
from .lectores import MARCA_PAGINA, leer_archivo
from .limites import ImportacionError, MAX_LINEAS, MAX_MOVIMIENTOS
from .lineas import _estrategia_difusa, _estrategia_lineas
from .modelos import Movimiento, ResultadoParcial, ResultadoParseo
from .numeros import a_ascii_minusculas, buscar_fechas, buscar_importes, normalizar_entrada
from .tabla import _intentar_tabla

_CALIDAD_MINIMA = 0.35
_VENTAJA_NECESARIA = 1.15   # una estrategia posterior debe superar a la anterior en un 15 %


def _clave_linea(linea: str) -> str:
    return re.sub(r"\s+", " ", a_ascii_minusculas(linea)).strip()


def _quitar_cabeceras_y_pies(paginas: List[List[str]]) -> List[str]:
    """Elimina las líneas de cabecera/pie que se repiten en las páginas de un PDF.

    Solo se consideran las líneas sin fecha ni importe situadas al principio
    (antes de la primera operación) o al final (tras la última) de cada página,
    y solo si se repiten en al menos la mitad de las páginas (mínimo dos).
    """
    if len(paginas) < 2:
        return [l for pag in paginas for l in pag]
    cuentas: Counter = Counter()
    extremos_por_pagina = []
    for pag in paginas:
        con_datos = [i for i, l in enumerate(pag) if buscar_fechas(l) or buscar_importes(l)]
        if con_datos:
            cabecera, pie = pag[:con_datos[0]][:8], pag[con_datos[-1] + 1:][-6:]
        else:
            cabecera, pie = pag[:8], []
        claves = {_clave_linea(l) for l in cabecera + pie if _clave_linea(l)}
        extremos_por_pagina.append(claves)
        cuentas.update(claves)
    umbral = max(2, math.ceil(len(paginas) * 0.5))
    ruido = {t for t, c in cuentas.items() if c >= umbral}
    return [l for pag in paginas for l in pag if _clave_linea(l) not in ruido]


def _preparar_lineas(texto: str) -> List[str]:
    lineas = [l.rstrip() for l in normalizar_entrada(texto).split("\n")]
    paginas: List[List[str]] = [[]]
    for l in lineas:
        if l.strip() == MARCA_PAGINA:
            paginas.append([])
        elif l.strip():
            paginas[-1].append(l)
    paginas = [p for p in paginas if p]
    lineas = _quitar_cabeceras_y_pies(paginas) if paginas else []
    if len(lineas) > MAX_LINEAS:
        raise ImportacionError(
            f"El texto tiene {len(lineas):,} líneas; el máximo es {MAX_LINEAS:,}. Divídelo en varios archivos."
            .replace(",", "."))
    return lineas


def _candidatos(lineas: Sequence[str]) -> List[ResultadoParcial]:
    candidatos: List[ResultadoParcial] = []

    sniper = _parseador_francotirador(list(lineas))
    if sniper is not None:
        candidatos.append(sniper)

    tabla = _intentar_tabla(lineas)
    if tabla is not None:
        candidatos.append(tabla)

    candidatos.extend(_estrategia_lineas(lineas))

    difusa = _estrategia_difusa(lineas)
    if difusa is not None:
        candidatos.append(difusa)
    return candidatos


def _elegir(candidatos: Sequence[ResultadoParcial]) -> Optional[ResultadoParcial]:
    """El orden de ``candidatos`` es el de prioridad; solo se desplaza al
    anterior si el siguiente es claramente mejor."""
    mejor: Optional[ResultadoParcial] = None
    for c in candidatos:
        if not c.movimientos:
            continue
        if mejor is None or c.calidad > mejor.calidad * _VENTAJA_NECESARIA:
            mejor = c
    return mejor


def _orden_cronologico(movs: List[Movimiento]) -> List[Movimiento]:
    """Si el archivo viene con lo más reciente primero, lo invierte para que los
    ids de base de datos respeten el orden cronológico dentro de cada día."""
    if len(movs) < 2:
        return movs
    sube = sum(1 for a, b in zip(movs, movs[1:]) if b.fecha > a.fecha)
    baja = sum(1 for a, b in zip(movs, movs[1:]) if b.fecha < a.fecha)
    return list(reversed(movs)) if baja > sube else movs


def reconstruir_movimientos_detallado(texto: str) -> ResultadoParseo:
    """Convierte cualquier texto de extracto en movimientos estructurados.

    Lanza :class:`ImportacionError` (mensaje apto para el usuario) si no se
    reconoce ningún movimiento o se superan los límites.
    """
    lineas = _preparar_lineas(texto)
    if not lineas:
        raise ImportacionError("No hay texto que analizar.")
    elegido = _elegir(_candidatos(lineas))
    if elegido is None or elegido.calidad < _CALIDAD_MINIMA:
        raise ImportacionError(
            "No he podido reconocer movimientos en ese contenido. Comprueba que incluye fecha, "
            "concepto e importe en cada operación (por ejemplo: «COMPRA MERCADONA 12/03/2026 -45,20 € 1.234,56 €»).")
    if len(elegido.movimientos) > MAX_MOVIMIENTOS:
        raise ImportacionError(
            f"Se han detectado {len(elegido.movimientos):,} movimientos; el máximo por importación es "
            f"{MAX_MOVIMIENTOS:,}. Divide el archivo por periodos.".replace(",", "."))
    movimientos = _orden_cronologico(list(elegido.movimientos))
    return ResultadoParseo(
        movimientos=movimientos,
        estrategia=elegido.estrategia,
        avisos=list(elegido.avisos),
        descartadas=elegido.descartadas,
        lineas_analizadas=len(lineas),
        calidad=round(elegido.calidad, 3),
    )


def reconstruir_movimientos(texto: str) -> List[Movimiento]:
    return reconstruir_movimientos_detallado(texto).movimientos


def analizar_archivo(nombre: str, contenido: bytes) -> ResultadoParseo:
    """Lee un CSV/XLSX/PDF y lo analiza (en PDF prueba texto normal y 'layout')."""
    _tipo, textos = leer_archivo(nombre, contenido)
    mejor: Optional[ResultadoParseo] = None
    ultimo_error: Optional[ImportacionError] = None
    for texto in textos:
        try:
            res = reconstruir_movimientos_detallado(texto)
        except ImportacionError as exc:
            ultimo_error = exc
            continue
        if mejor is None or res.calidad > mejor.calidad * _VENTAJA_NECESARIA or \
                (len(res.movimientos) > len(mejor.movimientos) and res.calidad >= mejor.calidad):
            mejor = res
    if mejor is None:
        raise ultimo_error or ImportacionError("No he podido reconocer movimientos en el archivo.")
    return mejor
