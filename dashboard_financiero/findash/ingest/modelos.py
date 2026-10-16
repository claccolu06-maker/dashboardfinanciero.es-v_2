"""Estructuras de datos del motor de ingesta."""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Movimiento:
    fecha: datetime.date
    concepto: str
    importe: float                      # siempre positivo
    tipo: str                           # 'ingreso' | 'gasto'
    saldo: Optional[float] = None
    categoria: Optional[str] = None
    tipo_adivinado: bool = False        # el signo no venía en los datos

    @property
    def fecha_iso(self) -> str:
        return self.fecha.isoformat()

    def clave(self) -> Tuple[str, str, float, str]:
        """Clave de deduplicación: (fecha, concepto normalizado, importe, tipo)."""
        concepto = re.sub(r"\s+", " ", self.concepto).strip().casefold()
        return (self.fecha_iso, concepto, round(self.importe, 2), self.tipo)

    def como_dict(self) -> dict:
        return {
            "fecha": self.fecha_iso,
            "concepto": self.concepto,
            "importe": round(self.importe, 2),
            "tipo": self.tipo,
            "categoria": self.categoria,
            "saldo": None if self.saldo is None else round(self.saldo, 2),
        }


@dataclass
class ResultadoParcial:
    """Salida de una estrategia concreta, antes de elegir la mejor."""
    movimientos: List[Movimiento]
    estrategia: str
    descartadas: int = 0
    avisos: List[str] = field(default_factory=list)
    confianza_estructura: float = 0.5   # lo fiable que es la propia estrategia
    saldo_verificado: bool = False      # los saldos cuadran con los importes
    candidatas: int = 0                 # filas que parecían movimientos

    @property
    def calidad(self) -> float:
        n = len(self.movimientos)
        if n == 0:
            return 0.0
        base = n / max(n + self.descartadas, 1)
        if self.saldo_verificado:
            base *= 1.15
        adivinados = sum(1 for m in self.movimientos if m.tipo_adivinado)
        if adivinados:
            base *= 1.0 - 0.4 * (adivinados / n)
        return base * self.confianza_estructura


@dataclass
class ResultadoParseo:
    """Resultado final de ``reconstruir_movimientos_detallado``."""
    movimientos: List[Movimiento]
    estrategia: str
    avisos: List[str]
    descartadas: int
    lineas_analizadas: int
    calidad: float

    def como_dict(self) -> dict:
        return {
            "estrategia": self.estrategia,
            "avisos": self.avisos,
            "descartadas": self.descartadas,
            "lineas_analizadas": self.lineas_analizadas,
            "detectados": len(self.movimientos),
        }
