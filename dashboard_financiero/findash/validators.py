"""Validación y saneado de la entrada del usuario (formularios y JSON)."""
from __future__ import annotations

import datetime
import math
import re
from typing import Any, Optional, Tuple

from .ingest.numeros import parse_numero

MAX_IMPORTE = 1_000_000_000.0
MAX_CONCEPTO = 200
MAX_CATEGORIA = 60
MAX_NOMBRE = 50
TIPOS = ("ingreso", "gasto")

_RE_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_RE_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class ValidacionError(ValueError):
    """Error de validación con mensaje apto para el usuario."""


def limpiar_texto(valor: Any, max_len: int, obligatorio: bool = False, nombre: str = "El campo") -> str:
    texto = _RE_CONTROL.sub(" ", str(valor or ""))
    texto = re.sub(r"\s+", " ", texto).strip()
    if obligatorio and not texto:
        raise ValidacionError(f"{nombre} es obligatorio.")
    return texto[:max_len]


def validar_tipo(valor: Any) -> str:
    tipo = str(valor or "").strip().lower()
    if tipo not in TIPOS:
        raise ValidacionError("El tipo de movimiento debe ser «ingreso» o «gasto».")
    return tipo


def validar_importe(valor: Any, permitir_cero: bool = False) -> float:
    """Acepta ``12.5``, ``12,50`` o ``1.234,56``. Rechaza NaN, infinito y negativos."""
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        numero: Optional[float] = float(valor)
    else:
        numero = parse_numero(str(valor or "").strip())
    if numero is None or not math.isfinite(numero):
        raise ValidacionError("El importe no es un número válido.")
    if numero < 0 or (numero == 0 and not permitir_cero):
        raise ValidacionError("El importe debe ser un número mayor que cero.")
    if numero > MAX_IMPORTE:
        raise ValidacionError("El importe es demasiado grande.")
    return round(numero, 2)


def validar_fecha(valor: Any, permitir_futuro: bool = False) -> datetime.date:
    try:
        fecha = datetime.datetime.strptime(str(valor or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValidacionError("Formato de fecha incorrecto.") from None
    if fecha.year < 1990:
        raise ValidacionError("La fecha es demasiado antigua.")
    if not permitir_futuro and fecha > datetime.date.today():
        raise ValidacionError("No es posible registrar movimientos en fechas futuras.")
    return fecha


def validar_email(valor: Any) -> str:
    email = str(valor or "").strip().lower()
    if len(email) > 254 or not _RE_EMAIL.match(email):
        raise ValidacionError("Introduce un correo electrónico válido.")
    return email


def validar_password(valor: Any) -> str:
    password = str(valor or "")
    if len(password) < 8 or not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
        raise ValidacionError("La contraseña debe tener al menos 8 caracteres con letras y números.")
    if len(password) > 128:
        raise ValidacionError("La contraseña es demasiado larga (máximo 128 caracteres).")
    return password


def validar_tema(valor: Any) -> str:
    tema = str(valor or "").strip().lower()
    return tema if tema in ("light", "dark", "system") else "dark"


def validar_dia_mes(valor: Any) -> int:
    try:
        dia = int(str(valor).strip())
    except (TypeError, ValueError):
        raise ValidacionError("El día del mes debe ser un número entre 1 y 31.") from None
    if not 1 <= dia <= 31:
        raise ValidacionError("El día del mes debe estar entre 1 y 31.")
    return dia


def validar_movimiento(datos: Any, permitir_futuro: bool = False) -> Tuple[datetime.date, str, str, float, str]:
    """Valida los campos comunes de alta/edición. Devuelve (fecha, concepto, categoria, importe, tipo)."""
    tipo = validar_tipo(datos.get("tipo"))
    fecha = validar_fecha(datos.get("fecha"), permitir_futuro)
    concepto = limpiar_texto(datos.get("concepto"), MAX_CONCEPTO, True, "El concepto")
    categoria = limpiar_texto(datos.get("categoria"), MAX_CATEGORIA)
    importe = validar_importe(datos.get("importe"))
    return fecha, concepto, categoria, importe, tipo
