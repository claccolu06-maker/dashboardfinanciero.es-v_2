"""Copia una base de datos SQLite de FinDash (la actual o una antigua) a otra base.

Pensado para el salto SQLite -> PostgreSQL (Neon), pero funciona con cualquier
destino que soporte :class:`findash.db.Database`.

* Conserva los ``id`` (los movimientos siguen apuntando a su usuario) y, en
  PostgreSQL, deja las secuencias ``SERIAL`` por encima del mayor ``id``.
* Solo copia las columnas que existen en origen y destino: un SQLite antiguo sin
  ``tema`` / ``fecha_objetivo`` / ``recurrentes`` se migra sin problemas.
* Todo ocurre en UNA transacción: o se copia todo o no se copia nada.
* No pisa datos: si el destino ya tiene usuarios se niega a continuar.
* Lo que no se puede copiar sin romper restricciones (filas huérfanas, tipo
  distinto de ingreso/gasto, importes no numéricos...) se **omite y se cuenta**
  en el informe; nunca se descarta en silencio.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .db import Database, init_schema

# Orden respetando claves foráneas. login_intentos es efímera: no se migra.
TABLAS = ["usuarios", "presupuestos", "metas", "recurrentes", "reglas_categoria", "movimientos"]
_TRAMO = 500


class MigracionError(RuntimeError):
    pass


@dataclass
class InformeTabla:
    tabla: str
    en_origen: int = 0
    copiadas: int = 0
    omitidas: Dict[str, int] = field(default_factory=dict)
    ajustadas: Dict[str, int] = field(default_factory=dict)      # filas copiadas tras corregir algún campo

    @property
    def total_omitidas(self) -> int:
        return sum(self.omitidas.values())


@dataclass
class Informe:
    tablas: List[InformeTabla] = field(default_factory=list)
    simulacro: bool = False

    def texto(self) -> str:
        lineas = ["Simulacro (no se ha escrito nada)" if self.simulacro else "Migración completada", ""]
        for t in self.tablas:
            lineas.append(f"  {t.tabla:<18} origen={t.en_origen:<7} copiadas={t.copiadas:<7} omitidas={t.total_omitidas}")
            for motivo, n in sorted(t.omitidas.items()):
                lineas.append(f"      - {n} fila(s) omitida(s): {motivo}")
            for motivo, n in sorted(t.ajustadas.items()):
                lineas.append(f"      - {n} fila(s) copiada(s) con ajuste: {motivo}")
        return "\n".join(lineas)


def _columnas_sqlite(origen: sqlite3.Connection, tabla: str) -> List[str]:
    return [f["name"] for f in origen.execute(f"PRAGMA table_info({tabla})").fetchall()]


def _tabla_existe(origen: sqlite3.Connection, tabla: str) -> bool:
    return origen.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (tabla,)).fetchone() is not None


def _columnas_destino(destino: Database, tabla: str) -> List[str]:
    if destino.is_postgres:
        filas = destino.query(
            "SELECT column_name AS nombre FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = current_schema() ORDER BY ordinal_position", (tabla,))
        return [f["nombre"] for f in filas]
    return [f[1] for f in destino.conn.execute(f"PRAGMA table_info({tabla})").fetchall()]


def _es_numero_valido(valor: Any) -> bool:
    if isinstance(valor, bool):
        return False
    if isinstance(valor, (int, float)):
        return not (isinstance(valor, float) and (math.isnan(valor) or math.isinf(valor)))
    return False


# Reglas por tabla: devuelven el motivo de omisión o None si la fila es válida.
# ``ajustes`` recoge las correcciones menores aplicadas a filas que sí se copian.
def _motivo_invalida(tabla: str, fila: Dict[str, Any], usuarios_validos: set,
                     emails_vistos: set, ajustes: Dict[str, int]) -> Optional[str]:
    if tabla == "usuarios":
        email = fila.get("email")
        if not isinstance(email, str) or not email.strip():
            return "usuario sin email"
        normalizado = email.strip().lower()          # el login siempre busca en minúsculas
        if normalizado in emails_vistos:
            return "email duplicado (tras normalizar mayúsculas/espacios)"
        emails_vistos.add(normalizado)
        if normalizado != email:
            ajustes["email normalizado a minúsculas"] = ajustes.get("email normalizado a minúsculas", 0) + 1
            fila["email"] = normalizado
        if not isinstance(fila.get("nombre"), str) or not fila["nombre"].strip():
            fila["nombre"] = normalizado.split("@")[0][:50]
            ajustes["nombre vacío sustituido por el email"] = ajustes.get("nombre vacío sustituido por el email", 0) + 1
        if not isinstance(fila.get("password"), str) or not fila["password"]:
            fila["password"] = "!"                    # hash inutilizable: no se pierde el usuario ni sus datos
            ajustes["contraseña vacía (cuenta sin acceso por contraseña)"] = \
                ajustes.get("contraseña vacía (cuenta sin acceso por contraseña)", 0) + 1
        return None
    if fila.get("usuario_id") not in usuarios_validos:
        return "huérfana (su usuario no existe)"
    if tabla in ("movimientos", "recurrentes", "reglas_categoria") and fila.get("tipo") not in ("ingreso", "gasto"):
        return "tipo distinto de ingreso/gasto"
    if tabla == "movimientos":
        if not _es_numero_valido(fila.get("importe")):
            return "importe no numérico"
        if not (fila.get("fecha") and fila.get("concepto") and fila.get("categoria")):
            return "falta fecha, concepto o categoría"
    if tabla == "recurrentes":
        if not _es_numero_valido(fila.get("importe")):
            return "importe no numérico"
        dia = fila.get("dia_mes")
        if not isinstance(dia, int) or not 1 <= dia <= 31:
            return "día del mes inválido"
        if not (fila.get("concepto") and fila.get("categoria")):
            return "falta concepto o categoría"
    if tabla == "presupuestos":
        if not _es_numero_valido(fila.get("importe_mensual")) or not fila.get("categoria"):
            return "presupuesto inválido"
    if tabla == "metas":
        if not _es_numero_valido(fila.get("cantidad_objetivo")) or not fila.get("nombre"):
            return "meta inválida"
        if fila.get("cantidad_actual") is None:
            fila["cantidad_actual"] = 0
    if tabla == "reglas_categoria" and not (fila.get("patron") and fila.get("categoria")):
        return "regla inválida"
    return None


class _Simulacro(Exception):
    """Se lanza al final de un simulacro para revertir la transacción."""


def migrar(ruta_sqlite: str, destino: Database, simulacro: bool = False) -> Informe:
    """Copia ``ruta_sqlite`` -> ``destino``. Lanza :class:`MigracionError` si no es seguro hacerlo."""
    try:
        origen = sqlite3.connect(f"file:{ruta_sqlite}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MigracionError(f"No se puede abrir el SQLite de origen: {exc}") from exc
    origen.row_factory = sqlite3.Row
    informe = Informe(simulacro=simulacro)
    try:
        if not _tabla_existe(origen, "usuarios"):
            raise MigracionError("El archivo de origen no parece una base de FinDash (no tiene la tabla 'usuarios').")

        init_schema(destino)
        if destino.scalar("SELECT COUNT(*) FROM usuarios", default=0):
            raise MigracionError(
                "El destino ya contiene usuarios. Por seguridad no se mezclan datos: usa una base de datos vacía.")

        try:
            with destino.transaction():
                usuarios_validos: set = set()
                emails_vistos: set = set()
                for tabla in TABLAS:
                    t = InformeTabla(tabla)
                    informe.tablas.append(t)
                    if not _tabla_existe(origen, tabla):
                        continue                               # p. ej. 'recurrentes' en una base antigua
                    comunes = [c for c in _columnas_sqlite(origen, tabla) if c in set(_columnas_destino(destino, tabla))]
                    if "id" not in comunes:
                        raise MigracionError(f"La tabla '{tabla}' de origen no tiene columna id.")
                    lote: List[Sequence[Any]] = []
                    ids_vistos: set = set()
                    for fila in origen.execute(f"SELECT {', '.join(comunes)} FROM {tabla} ORDER BY id"):
                        t.en_origen += 1
                        datos = {c: fila[c] for c in comunes}
                        motivo = _motivo_invalida(tabla, datos, usuarios_validos, emails_vistos, t.ajustadas)
                        if motivo is None and datos["id"] in ids_vistos:
                            motivo = "id repetido"
                        if motivo is not None:
                            t.omitidas[motivo] = t.omitidas.get(motivo, 0) + 1
                            continue
                        ids_vistos.add(datos["id"])
                        if tabla == "usuarios":
                            usuarios_validos.add(datos["id"])
                        lote.append([datos[c] for c in comunes])
                        if len(lote) >= _TRAMO:
                            t.copiadas += destino.insert_many(tabla, comunes, lote)
                            lote = []
                    if lote:
                        t.copiadas += destino.insert_many(tabla, comunes, lote)

                    if destino.is_postgres:
                        # Las secuencias SERIAL no avanzan al insertar ids explícitos.
                        destino.query(
                            f"SELECT setval(pg_get_serial_sequence('{tabla}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {tabla}), 1), "
                            f"(SELECT COUNT(*) FROM {tabla}) > 0)")

                for t in informe.tablas:                        # comprobación final de cuadre
                    if t.copiadas + t.total_omitidas != t.en_origen:
                        raise MigracionError(f"Descuadre en '{t.tabla}': {t.copiadas} + {t.total_omitidas} != {t.en_origen}")
                    en_destino = destino.scalar(f"SELECT COUNT(*) FROM {t.tabla}", default=0)
                    if en_destino != t.copiadas:
                        raise MigracionError(f"Descuadre en '{t.tabla}': el destino tiene {en_destino}, se copiaron {t.copiadas}")
                if simulacro:
                    raise _Simulacro()
        except _Simulacro:
            pass
        return informe
    finally:
        origen.close()
