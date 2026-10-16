"""Escudo Antimisiles V3: parser ultrarrápido de texto copiado de la banca online.

Reconoce líneas con la estructura::

    [CONCEPTO] [DD/MM/YYYY] [+/-IMPORTE€] [SALDO€]

ignorando las líneas de paginación (``2/18``), normalizando separadores
europeos (``1.234,56``) y comprobando, cuando el importe no trae signo, la
cadena de saldos. Es una estrategia de **alta precisión**: si no reconoce al
menos el 70 % de las líneas que parecen movimientos, devuelve ``None`` y el
motor prueba las estrategias más tolerantes.
"""
from __future__ import annotations

import re
from typing import List, Optional

from .modelos import Movimiento, ResultadoParcial
from .numeros import buscar_fechas, detectar_orden_fecha, limpiar_concepto, parse_fecha
from .tokens import _consistencia, es_ruido, tipo_por_palabras

_FECHA = r"(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2})"
_IMPORTE = (r"[+\-−–]?\s?(?:[€$]\s?)?"
            r"(?:\d{1,3}(?:[.,]\d{3})+|\d+)[.,]\d{2}")
_MONEDA = r"\s*(?:€|eur(?:os)?\b)?"

RE_FRANCOTIRADOR = re.compile(
    rf"^\s*(?P<concepto>.*?\S)\s+(?P<fecha>{_FECHA})\s+"
    rf"(?P<importe>{_IMPORTE}){_MONEDA}"
    rf"(?:\s+(?P<saldo>{_IMPORTE}){_MONEDA})?\s*$",
    re.IGNORECASE,
)

_UMBRAL_ACEPTACION = 0.7


def _signo_y_valor(texto: str):
    from .numeros import buscar_importes
    toks = buscar_importes(texto.strip())
    if len(toks) != 1:
        return None
    return toks[0].valor, toks[0].signo


def _parseador_francotirador(lineas: List[str]) -> Optional[ResultadoParcial]:
    """Devuelve el resultado o ``None`` si el texto no encaja con el patrón."""
    todas_fechas = []
    for linea in lineas:
        todas_fechas.extend(re.findall(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}", linea))
    orden = detectar_orden_fecha(todas_fechas)

    filas = []          # (fecha, concepto, valor, signo, saldo)
    candidatas = 0
    for linea in lineas:
        if es_ruido(linea):
            continue
        if buscar_fechas(linea, orden):
            candidatas += 1
        m = RE_FRANCOTIRADOR.match(linea)
        if not m:
            continue
        fecha = parse_fecha(m.group("fecha"), orden)
        imp = _signo_y_valor(m.group("importe"))
        if fecha is None or imp is None:
            continue
        saldo = None
        if m.group("saldo"):
            s = _signo_y_valor(m.group("saldo"))
            if s is None:
                continue
            saldo = -s[0] if s[1] == -1 else s[0]
        concepto = limpiar_concepto(m.group("concepto"))
        if not concepto or not re.search(r"[A-Za-zÀ-ÿ]", concepto):
            continue
        filas.append((fecha, concepto, imp[0], imp[1], saldo))

    if not filas or candidatas == 0 or len(filas) / candidatas < _UMBRAL_ACEPTACION:
        return None

    avisos: List[str] = []
    signos = [f[3] for f in filas]
    saldo_verificado = False
    if any(s is None for s in signos):
        deducidos, ratio, _orden = _consistencia([f[2] for f in filas], [f[4] for f in filas])
        if ratio >= 0.8:
            saldo_verificado = True
            signos = [s if s is not None else d for s, d in zip(signos, deducidos)]

    movimientos: List[Movimiento] = []
    adivinados = 0
    for (fecha, concepto, valor, _s, saldo), signo in zip(filas, signos):
        adivinado = False
        if signo is None:
            tipo = tipo_por_palabras(concepto)
            if tipo is None:
                tipo = "gasto"
            adivinado = True
            adivinados += 1
        else:
            tipo = "ingreso" if signo > 0 else "gasto"
        movimientos.append(Movimiento(fecha, concepto, valor, tipo, saldo, tipo_adivinado=adivinado))
    if adivinados:
        avisos.append(
            f"En {adivinados} movimiento(s) el archivo no indica si es ingreso o gasto y los saldos no permiten "
            "deducirlo: se han clasificado por su concepto. Revisa la columna «Tipo» antes de importar "
            "(también puedes corregirlos después con el lápiz).")

    return ResultadoParcial(
        movimientos=movimientos,
        estrategia="francotirador",
        descartadas=max(candidatas - len(filas), 0),
        avisos=avisos,
        confianza_estructura=1.0,
        saldo_verificado=saldo_verificado or all(f[4] is not None for f in filas) and _verifica_saldos(filas, signos),
        candidatas=candidatas,
    )


def _verifica_saldos(filas, signos) -> bool:
    """Con signos explícitos y saldos, comprueba que la cadena cuadra."""
    valores = [f[2] if s is None else f[2] * s for f, s in zip(filas, signos)]
    saldos = [f[4] for f in filas]
    if len(filas) < 2 or any(s is None for s in signos):
        return False
    ok_asc = sum(abs((saldos[i] - saldos[i - 1]) - valores[i]) <= 0.011 for i in range(1, len(filas)))
    ok_desc = sum(abs((saldos[i] - saldos[i + 1]) - valores[i]) <= 0.011 for i in range(len(filas) - 1))
    return max(ok_asc, ok_desc) / (len(filas) - 1) >= 0.8
