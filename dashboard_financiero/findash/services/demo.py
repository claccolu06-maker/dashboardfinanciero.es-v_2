"""Datos de demostración para el «Modo Despegue» (usuarios sin movimientos)."""
from __future__ import annotations

import datetime
import random
from typing import Any, List, Optional, Tuple

_COLUMNAS = ("usuario_id", "fecha", "concepto", "categoria", "importe", "tipo", "demo")

# (concepto, categoría, mínimo, máximo, veces por mes)
_GASTOS_VARIABLES: List[Tuple[str, str, float, float, int]] = [
    ("MERCADONA COMPRA", "Alimentación", 22, 68, 5),
    ("CARREFOUR EXPRESS", "Alimentación", 9, 31, 2),
    ("RESTAURANTE LA TAGLIATELLA", "Restaurantes y Comida", 18, 46, 2),
    ("GLOVO PEDIDO", "Restaurantes y Comida", 12, 29, 2),
    ("CAFETERÍA CENTRAL", "Restaurantes y Comida", 2.4, 7.5, 4),
    ("REPSOL GASOLINERA", "Vehículo y Gasolina", 38, 62, 2),
    ("CINES YELMO", "Ocio y Tiempo Libre", 8.5, 24, 1),
    ("FARMACIA", "Salud y Bienestar", 6, 28, 1),
    ("ZARA", "Compras y Ropa", 19, 79, 1),
    ("METRO ABONO", "Transporte Público", 8.5, 12, 2),
]
# (concepto, categoría, importe, día)
_GASTOS_FIJOS: List[Tuple[str, str, float, int]] = [
    ("ALQUILER PISO", "Vivienda", 640.00, 3),
    ("IBERDROLA RECIBO LUZ", "Servicios del Hogar", 54.30, 8),
    ("NETFLIX", "Suscripciones Digitales", 12.99, 12),
    ("SPOTIFY", "Suscripciones Digitales", 10.99, 15),
]


def _fechas_de_meses(hoy: datetime.date, meses: int) -> List[Tuple[int, int]]:
    resultado = []
    anio, mes = hoy.year, hoy.month
    for _ in range(meses):
        resultado.append((anio, mes))
        mes -= 1
        if mes == 0:
            anio, mes = anio - 1, 12
    return list(reversed(resultado))


def generar_movimientos(usuario_id: int, hoy: Optional[datetime.date] = None,
                        meses: int = 4) -> List[tuple]:
    """Genera ``meses`` meses de actividad creíble que termina hoy (nunca en el futuro)."""
    import calendar
    hoy = hoy or datetime.date.today()
    azar = random.Random(usuario_id * 7919 + 17)
    filas: List[tuple] = []

    def anadir(fecha: datetime.date, concepto: str, categoria: str, importe: float, tipo: str) -> None:
        if fecha <= hoy:
            filas.append((usuario_id, fecha.isoformat(), concepto, categoria, round(importe, 2), tipo, 1))

    for anio, mes in _fechas_de_meses(hoy, meses):
        ultimo = calendar.monthrange(anio, mes)[1]
        anadir(datetime.date(anio, mes, 1), "NÓMINA EMPRESA SL", "Nómina y Salario", 1850.00, "ingreso")
        if mes % 3 == 0:
            anadir(datetime.date(anio, mes, min(20, ultimo)), "BIZUM DEVOLUCIÓN", "Transferencias",
                   round(azar.uniform(20, 90), 2), "ingreso")
        for concepto, categoria, importe, dia in _GASTOS_FIJOS:
            anadir(datetime.date(anio, mes, min(dia, ultimo)), concepto, categoria, importe, "gasto")
        for concepto, categoria, minimo, maximo, veces in _GASTOS_VARIABLES:
            for _ in range(veces):
                dia = azar.randint(1, ultimo)
                anadir(datetime.date(anio, mes, dia), concepto, categoria, azar.uniform(minimo, maximo), "gasto")
    filas.sort(key=lambda f: (f[1], f[2]))
    return filas


def cargar_demo(db: Any, usuario_id: int, hoy: Optional[datetime.date] = None) -> int:
    """Inserta la demo solo si el usuario no tiene ningún movimiento."""
    with db.transaction():
        if int(db.scalar("SELECT COUNT(*) FROM movimientos WHERE usuario_id = ?", (usuario_id,), 0)) > 0:
            return 0
        return db.insert_many("movimientos", _COLUMNAS, generar_movimientos(usuario_id, hoy))


def borrar_demo(db: Any, usuario_id: int) -> int:
    return db.execute("DELETE FROM movimientos WHERE usuario_id = ? AND demo = 1", (usuario_id,))


def tiene_demo(db: Any, usuario_id: int) -> bool:
    return int(db.scalar("SELECT COUNT(*) FROM movimientos WHERE usuario_id = ? AND demo = 1",
                         (usuario_id,), 0)) > 0
