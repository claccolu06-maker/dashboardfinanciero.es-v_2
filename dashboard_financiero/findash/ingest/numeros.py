"""Números, importes y fechas en formato europeo y anglosajón.

Funciones puras, sin dependencias de Flask ni de la base de datos.
"""
from __future__ import annotations

import datetime
import math
import re
import unicodedata
from typing import List, NamedTuple, Optional, Sequence

# Signos menos: guion, U+2212 (minus), U+2013 (en dash), U+2014 (em dash)
MENOS = "-−–—"

_RUIDO_MONEDA = re.compile(r"(?i)(?:€|\$|£|\beuros?\b|\beur\b|\busd\b)")


# ---------------------------------------------------------------------------
# Normalización de texto
# ---------------------------------------------------------------------------
def normalizar_entrada(texto: str) -> str:
    """Limpia un texto pegado/extraído: saltos de línea, NBSP, signos menos raros."""
    if not isinstance(texto, str):
        texto = str(texto)
    texto = texto.replace("﻿", "").replace("\x00", "")
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    for raro in (" ", " ", " ", " ", " ", " "):
        texto = texto.replace(raro, " ")
    for invisible in ("​", "‌", "‍", "⁠"):
        texto = texto.replace(invisible, "")
    # Un signo menos Unicode delante de un número se convierte en guion normal.
    texto = re.sub(r"[−–—](?=\s?[\d€$(])", "-", texto)
    return texto


def a_ascii_minusculas(texto: str) -> str:
    """minúsculas sin tildes (conserva la ñ como n)."""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------------------
# Números
# ---------------------------------------------------------------------------
_NUCLEO_NUMERICO = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def _resolver_separadores(nucleo: str, decimal: Optional[str]) -> Optional[str]:
    """Devuelve el número como cadena con '.' decimal o None si es ambiguo/inválido."""
    n_punto, n_coma = nucleo.count("."), nucleo.count(",")
    if decimal in (".", ","):
        miles = "," if decimal == "." else "."
        if nucleo.count(decimal) > 1:
            return None
        return nucleo.replace(miles, "").replace(decimal, ".")
    if n_punto and n_coma:
        decimal = "." if nucleo.rfind(".") > nucleo.rfind(",") else ","
        miles = "," if decimal == "." else "."
        if nucleo.count(decimal) > 1:
            return None
        return nucleo.replace(miles, "").replace(decimal, ".")
    sep = "." if n_punto else ("," if n_coma else "")
    if not sep:
        return nucleo
    cuantos = nucleo.count(sep)
    izquierda, _, derecha = nucleo.rpartition(sep)
    if cuantos > 1:  # 1.234.567 -> miles; 12.345.67 -> inválido
        grupos = nucleo.split(sep)
        if all(len(g) == 3 for g in grupos[1:]) and 1 <= len(grupos[0]) <= 3:
            return nucleo.replace(sep, "")
        return None
    # Un único separador. Con 3 dígitos detrás y 1-3 delante lo tratamos como
    # miles (los importes monetarios llevan como mucho 2 decimales).
    if len(derecha) == 3 and 1 <= len(izquierda) <= 3 and izquierda != "0":
        return izquierda + derecha
    return izquierda + "." + derecha


def parse_numero(texto: object, decimal: Optional[str] = None) -> Optional[float]:
    """Convierte ``'1.234,56 €'``, ``'-45,20'``, ``'(45.20)'``, ``'45,20-'`` en float.

    Devuelve ``None`` si el texto no es un número. El signo se conserva.
    ``decimal`` ('.' o ',') fuerza el separador decimal cuando se conoce.
    """
    if texto is None:
        return None
    if isinstance(texto, bool):
        return None
    if isinstance(texto, (int, float)):
        valor = float(texto)
        return valor if math.isfinite(valor) else None
    s = str(texto).strip()
    if not s:
        return None
    signo = 1
    s_min = s.lower()
    if re.search(r"(?i)\b(db|deb|debito|débito)\s*$", s_min):
        signo = -1
    s = re.sub(r"(?i)\b(cr|db|deb|credito|crédito|debito|débito)\s*$", "", s).strip()
    s = _RUIDO_MONEDA.sub("", s).strip()
    if s.startswith("(") and s.endswith(")"):
        signo = -1
        s = s[1:-1].strip()
    if s and s[0] in MENOS:
        signo = -1
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()
    if s.endswith(tuple(MENOS)):
        signo = -1
        s = s[:-1].strip()
    elif s.endswith("+"):
        s = s[:-1].strip()
    s = s.replace(" ", "")
    if not _NUCLEO_NUMERICO.fullmatch(s):
        return None
    normal = _resolver_separadores(s, decimal)
    if normal is None:
        return None
    try:
        valor = float(normal)
    except ValueError:
        return None
    if not math.isfinite(valor):
        return None
    return signo * valor


def detectar_decimal(celdas: Sequence[str]) -> Optional[str]:
    """Decide si una columna usa ',' o '.' como separador decimal."""
    coma = punto = 0
    for c in celdas:
        c = _RUIDO_MONEDA.sub("", str(c)).strip()
        if re.search(r",\d{1,2}$", c):
            coma += 1
        elif re.search(r"\.\d{1,2}$", c):
            punto += 1
    if coma and not punto:
        return ","
    if punto and not coma:
        return "."
    return None


# --- tokens de importe dentro de una línea de texto -------------------------
_CUERPO_IMPORTE = r"(?:\d{1,3}(?:[.,]\d{3})+|\d+)[.,]\d{2}"
RE_IMPORTE = re.compile(
    r"(?<![\w.,/+\-−–—])"
    r"(?:(?P<pre>[+\-−–—])\s?)?(?:(?P<par>\()\s?)?(?:[€$]\s?)?"
    rf"(?P<num>{_CUERPO_IMPORTE})"
    r"(?:\s?(?:€|eur(?:os)?\b))?(?P<par2>\))?(?P<post>-)?"
    r"(?![\d%]|[.,]\d)",
    re.IGNORECASE,
)


class ImporteTok(NamedTuple):
    inicio: int
    fin: int
    valor: float          # siempre positivo
    signo: Optional[int]  # +1, -1 o None si no lleva signo explícito
    texto: str


def buscar_importes(texto: str, decimal: Optional[str] = None) -> List[ImporteTok]:
    """Localiza importes monetarios (siempre con dos decimales)."""
    resultado: List[ImporteTok] = []
    for m in RE_IMPORTE.finditer(texto):
        valor = parse_numero(m.group("num"), decimal)
        if valor is None:
            continue
        signo: Optional[int] = None
        pre = m.group("pre")
        if pre:
            signo = 1 if pre == "+" else -1
        if m.group("par") and m.group("par2"):
            signo = -1
        if m.group("post"):
            signo = -1
        resultado.append(ImporteTok(m.start(), m.end(), abs(valor), signo, m.group(0)))
    return resultado


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------
_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12, "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "sept": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "apr": 4, "aug": 8, "dec": 12,
}

RE_FECHA_ISO = re.compile(
    r"(?<![\d/.\-:])(\d{4})([/.\-])(\d{1,2})\2(\d{1,2})(?![\d/\-:]|[.,]\d)(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?")
RE_FECHA_NUM = re.compile(
    r"(?<![\d/.\-:])(\d{1,2})([/.\-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/\-:]|[.,]\d)(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")
RE_FECHA_TXT = re.compile(
    r"(?<![\d/.\-:])(\d{1,2})[\s\-/.]*(?:de\s+)?([A-Za-zÁÉÍÓÚáéíóúñÑ]{3,10})\.?[\s\-/.,]*(?:de\s+)?(\d{4}|\d{2})(?![\d/\-:])")
RE_FECHA_COMPACTA = re.compile(r"(?<![\d.,])(\d{4})(\d{2})(\d{2})(?![\d.,])")


class FechaTok(NamedTuple):
    inicio: int
    fin: int
    fecha: datetime.date
    texto: str


def _anio_completo(a: int) -> int:
    if a >= 100:
        return a
    return 2000 + a if a <= 69 else 1900 + a


def _construir(d: int, m: int, a: int) -> Optional[datetime.date]:
    a = _anio_completo(a)
    hoy = datetime.date.today()
    if not (1990 <= a <= hoy.year + 1):
        return None
    try:
        return datetime.date(a, m, d)
    except ValueError:
        return None


def _desde_numerica(g1: int, g2: int, anio: int, orden: str) -> Optional[datetime.date]:
    if orden == "mdy":
        primero, segundo = (g2, g1)  # día, mes
    else:
        primero, segundo = (g1, g2)
    fecha = _construir(primero, segundo, anio)
    if fecha is None and segundo > 12 >= primero:
        fecha = _construir(segundo, primero, anio)  # formato contrario evidente
    return fecha


def buscar_fechas(texto: str, orden: str = "dmy") -> List[FechaTok]:
    """Localiza fechas en una línea (numéricas, ISO y con mes en texto)."""
    encontradas: List[FechaTok] = []
    ocupado: List[tuple] = []

    def libre(i: int, f: int) -> bool:
        return all(f <= a or i >= b for a, b in ocupado)

    for m in RE_FECHA_ISO.finditer(texto):
        fecha = _construir(int(m.group(4)), int(m.group(3)), int(m.group(1)))
        if fecha and libre(m.start(), m.end()):
            encontradas.append(FechaTok(m.start(), m.end(), fecha, m.group(0)))
            ocupado.append((m.start(), m.end()))
    for m in RE_FECHA_NUM.finditer(texto):
        fecha = _desde_numerica(int(m.group(1)), int(m.group(3)), int(m.group(4)), orden)
        if fecha and libre(m.start(), m.end()):
            encontradas.append(FechaTok(m.start(), m.end(), fecha, m.group(0)))
            ocupado.append((m.start(), m.end()))
    for m in RE_FECHA_TXT.finditer(texto):
        mes = _MESES.get(a_ascii_minusculas(m.group(2)))
        if not mes:
            continue
        fecha = _construir(int(m.group(1)), mes, int(m.group(3)))
        if fecha and libre(m.start(), m.end()):
            encontradas.append(FechaTok(m.start(), m.end(), fecha, m.group(0)))
            ocupado.append((m.start(), m.end()))
    for m in RE_FECHA_COMPACTA.finditer(texto):
        fecha = _construir(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if fecha and libre(m.start(), m.end()):
            encontradas.append(FechaTok(m.start(), m.end(), fecha, m.group(0)))
            ocupado.append((m.start(), m.end()))
    encontradas.sort(key=lambda f: f.inicio)
    return encontradas


def parse_fecha(texto: object, orden: str = "dmy") -> Optional[datetime.date]:
    """Interpreta una celda que ES una fecha (admite hora al final)."""
    if texto is None:
        return None
    if isinstance(texto, datetime.datetime):
        return texto.date()
    if isinstance(texto, datetime.date):
        return texto
    s = str(texto).strip()
    if not s or len(s) > 40:
        return None
    for tok in buscar_fechas(s, orden):
        if tok.inicio == 0:
            resto = s[tok.fin:].strip(" ,;")
            if not resto or re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", resto):
                return tok.fecha
    return None


def detectar_orden_fecha(muestras: Sequence[str]) -> str:
    """'dmy' (por defecto, España) o 'mdy' si los datos lo delatan."""
    primero_mayor = segundo_mayor = False
    for s in muestras:
        m = re.match(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", str(s))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            primero_mayor = True
        if b > 12:
            segundo_mayor = True
    if segundo_mayor and not primero_mayor:
        return "mdy"
    return "dmy"


# ---------------------------------------------------------------------------
# Conceptos
# ---------------------------------------------------------------------------
def limpiar_concepto(texto: str, max_len: int = 200) -> str:
    """Compacta espacios y quita adornos de los extremos."""
    texto = re.sub(r"[\x00-\x1f\x7f]+", " ", str(texto))
    texto = re.sub(r"\s+", " ", texto).strip(" \t-–—|;:,.*·•")
    return texto[:max_len].strip()
