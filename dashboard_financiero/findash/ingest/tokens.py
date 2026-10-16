"""Tokenización de líneas y heurísticas compartidas entre estrategias."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .numeros import (
    FechaTok, ImporteTok, a_ascii_minusculas, buscar_fechas, buscar_importes,
    limpiar_concepto,
)

# ---------------------------------------------------------------------------
# Ruido: paginación, cabeceras repetidas, líneas decorativas
# ---------------------------------------------------------------------------
_RE_PAGINACION = re.compile(
    r"^\s*(?:p[aá]g(?:ina)?\.?\s*)?\d{1,3}\s*(?:/|de|of)\s*\d{1,3}\s*$", re.IGNORECASE)
_RE_DECORACION = re.compile(r"^[\s\-_=*.·•|~]+$")

_LEXICO_CABECERA = {
    "fecha", "fechas", "valor", "operacion", "contable", "concepto", "descripcion", "detalle",
    "importe", "importes", "saldo", "saldos", "movimiento", "movimientos", "debe", "haber",
    "cargo", "cargos", "abono", "abonos", "divisa", "moneda", "eur", "euros", "total",
    "extracto", "iban", "titular", "oficina", "cuenta", "periodo", "disponible", "pagina",
    "anterior", "final", "inicial", "de", "del", "la", "el", "date", "description", "amount",
    "balance", "details", "debit", "credit", "ref", "referencia", "categoria", "tipo", "n",
    "num", "numero", "hoja", "pag", "ultimos", "ultimas", "desde", "hasta", "entre",
    "operaciones", "resumen", "a", "y", "en", "por", "dia", "mes",
}


def es_ruido(texto: str) -> bool:
    """¿La línea es paginación, decoración o una cabecera repetida?"""
    t = texto.strip()
    if not t:
        return True
    if _RE_PAGINACION.match(t) or _RE_DECORACION.match(t):
        return True
    palabras = re.findall(r"[a-z]+", a_ascii_minusculas(t))
    if palabras and len(palabras) >= 1 and all(p in _LEXICO_CABECERA for p in palabras):
        # solo vocabulario de cabecera/resumen y sin números sueltos relevantes
        return not re.search(r"\d", t)
    return False


def es_linea_resumen(texto_residual: str, tiene_fecha: bool) -> bool:
    """'Saldo anterior 1.234,56 €', 'Total ...': filas de resumen, no movimientos."""
    if tiene_fecha or not texto_residual:
        return False
    palabras = re.findall(r"[a-z]+", a_ascii_minusculas(texto_residual))
    return bool(palabras) and all(p in _LEXICO_CABECERA for p in palabras)


@dataclass
class Linea:
    idx: int
    original: str
    fechas: List[FechaTok] = field(default_factory=list)
    importes: List[ImporteTok] = field(default_factory=list)
    texto: str = ""

    @property
    def solo_fecha(self) -> bool:
        return bool(self.fechas) and not self.importes and not self.texto

    @property
    def solo_importes(self) -> bool:
        return bool(self.importes) and not self.fechas and not self.texto


_RESIDUO_MONEDA = re.compile(r"(?i)(?<![A-Za-z])(?:€|eur(?:os)?)(?![A-Za-z])")


def tokenizar_linea(idx: int, linea: str, orden: str = "dmy",
                    decimal: Optional[str] = None) -> Linea:
    """Separa fechas, importes y texto residual de una línea."""
    fechas = buscar_fechas(linea, orden)
    if fechas:
        piezas = list(linea)
        for f in fechas:
            for i in range(f.inicio, f.fin):
                piezas[i] = " "
        sin_fechas = "".join(piezas)
    else:
        sin_fechas = linea
    importes = buscar_importes(sin_fechas, decimal)
    huecos = sorted([(f.inicio, f.fin) for f in fechas] + [(i.inicio, i.fin) for i in importes])
    trozos: List[str] = []
    cursor = 0
    for a, b in huecos:
        if a > cursor:
            trozos.append(linea[cursor:a])
        cursor = max(cursor, b)
    trozos.append(linea[cursor:])
    residuo = _RESIDUO_MONEDA.sub(" ", " ".join(trozos))
    return Linea(idx, linea, fechas, importes, limpiar_concepto(residuo))


# ---------------------------------------------------------------------------
# Deducción de signo por palabras clave (último recurso)
# ---------------------------------------------------------------------------
_RE_INGRESO = re.compile(
    r"\b(nomina|salario|sueldo|haberes|ingreso|abono|devolucion|reembolso|intereses|"
    r"pension|premio|cashback|transferencia (?:recibida|de)|bizum (?:de|recibido)|"
    r"traspaso a su favor|dividendo|reintegro hacienda|liquidacion)\b")
_RE_GASTO = re.compile(
    r"\b(compra|pago|recibo|adeudo|cargo|comision|retirada|reintegro|cajero|"
    r"transferencia (?:a|enviada|emitida)|bizum (?:a|enviado)|domiciliacion|"
    r"tpv|pos|suscripcion|cuota|factura|impuesto|tasa|seguro)\b")


def tipo_por_palabras(concepto: str) -> Optional[str]:
    """'ingreso' / 'gasto' si el concepto lo delata; ``None`` si no."""
    t = a_ascii_minusculas(concepto)
    a, b = _RE_INGRESO.search(t), _RE_GASTO.search(t)
    if a and not b:
        return "ingreso"
    if b and not a:
        return "gasto"
    if a and b:
        return "ingreso" if a.start() < b.start() else "gasto"
    return None


# ---------------------------------------------------------------------------
# Cadena de saldos
# ---------------------------------------------------------------------------
_TOL = 0.011


def _consistencia(importes: Sequence[float], saldos: Sequence[Optional[float]]
                  ) -> Tuple[List[Optional[int]], float, str]:
    """Deduce el signo de cada importe usando la columna de saldo.

    ``importes`` son valores absolutos; ``saldos[i]`` es el saldo *tras* el
    movimiento i (o ``None``). Se prueban los dos órdenes posibles del archivo:
    ``asc`` (más antiguo primero) y ``desc`` (más reciente primero) y se elige el
    que mejor cuadra. Devuelve ``(signos, ratio, orden)`` donde ``signos[i]`` es
    +1 (ingreso), -1 (gasto) o ``None`` si no se pudo resolver, y ``ratio`` la
    proporción de pares consecutivos que cuadran exactamente.
    """
    n = len(importes)

    def evaluar(orden: str) -> Tuple[List[Optional[int]], int, int]:
        signos: List[Optional[int]] = [None] * n
        ok = total = 0
        for i in range(n):
            j = i - 1 if orden == "asc" else i + 1
            if not (0 <= j < n):
                continue
            if saldos[i] is None or saldos[j] is None:
                continue
            total += 1
            delta = saldos[i] - saldos[j]
            if abs(abs(delta) - importes[i]) <= _TOL:
                ok += 1
                if importes[i] > _TOL:
                    signos[i] = 1 if delta > 0 else -1
        return signos, ok, total

    mejor = None
    for orden in ("asc", "desc"):
        signos, ok, total = evaluar(orden)
        ratio = ok / total if total else 0.0
        if mejor is None or (ratio, ok) > (mejor[1], mejor[3]):
            mejor = (signos, ratio, orden, ok)
    return mejor[0], mejor[1], mejor[2]
