"""Capa de persistencia unificada: SQLite (desarrollo) y PostgreSQL (producción).

Reglas de oro para que el mismo SQL funcione en los dos motores:

* Los parámetros se escriben siempre como ``?``. En PostgreSQL se traducen a
  ``%s``. Por eso el SQL **no puede contener ``%``** (usa ``SUBSTR(fecha,1,7)``
  en lugar de ``strftime('%Y-%m', fecha)``; las fechas son texto ISO).
* ``SUBSTR``, ``COALESCE``, ``CASE`` y ``ON CONFLICT ... DO UPDATE`` existen en
  ambos motores. Nada de ``strftime``, ``lastrowid`` ni ``INSERT OR REPLACE``.
* Los identificadores recién creados se obtienen con :meth:`Database.insert`
  (``lastrowid`` en SQLite, ``RETURNING id`` en PostgreSQL).
* Las filas se devuelven siempre como ``dict`` con ``Decimal`` -> ``float`` y
  ``date`` -> ``'YYYY-MM-DD'``, así las plantillas no notan el motor.

Transacciones
-------------
Fuera de ``with db.transaction():`` cada ``execute`` es atómico (commit al final
o rollback si falla). Dentro, todo lo que ocurra se confirma o se revierte en
bloque. Al terminar la petición, :func:`close_db` revierte cualquier cosa sin
confirmar y cierra la conexión siempre.
"""
from __future__ import annotations

import contextlib
import datetime
import decimal
import logging
import os
import sqlite3
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence
from urllib.parse import urlparse

from flask import current_app, g

log = logging.getLogger("findash.db")

SQLITE = "sqlite"
POSTGRES = "postgres"


class SchemaError(RuntimeError):
    """El esquema existente no es compatible con la aplicación."""


def es_error_integridad(exc: BaseException) -> bool:
    """¿Es una violación de restricción (UNIQUE, CHECK...) en cualquiera de los motores?"""
    return any(c.__name__ in {"IntegrityError", "UniqueViolation"} for c in type(exc).__mro__)


# ---------------------------------------------------------------------------
# Utilidades de traducción
# ---------------------------------------------------------------------------
_PG_SQL_CACHE: Dict[str, str] = {}


def _a_postgres(sql: str) -> str:
    """Traduce los marcadores ``?`` a ``%s`` (con caché)."""
    traducido = _PG_SQL_CACHE.get(sql)
    if traducido is None:
        if "%" in sql:
            raise ValueError(
                "El SQL no puede contener '%'. Usa SUBSTR(fecha,1,7) y pasa los "
                "patrones LIKE como parámetros."
            )
        traducido = sql.replace("?", "%s")
        _PG_SQL_CACHE[sql] = traducido
    return traducido


def _normalizar_valor(valor: Any) -> Any:
    if isinstance(valor, decimal.Decimal):
        return float(valor)
    if isinstance(valor, datetime.datetime):
        return valor.isoformat(sep=" ")
    if isinstance(valor, datetime.date):
        return valor.isoformat()
    return valor


def _normalizar_fila(fila: Any) -> Dict[str, Any]:
    return {clave: _normalizar_valor(valor) for clave, valor in dict(fila).items()}


# ---------------------------------------------------------------------------
# Wrapper de conexión
# ---------------------------------------------------------------------------
class Database:
    """Envoltorio fino con la misma API para SQLite y PostgreSQL."""

    def __init__(self, dialecto: str, conexion: Any) -> None:
        self.dialect = dialecto
        self.conn = conexion
        self._depth = 0

    # -- propiedades --------------------------------------------------------
    @property
    def is_postgres(self) -> bool:
        return self.dialect == POSTGRES

    # -- internos -----------------------------------------------------------
    def _sql(self, sql: str) -> str:
        return _a_postgres(sql) if self.is_postgres else sql

    def _cursor(self) -> Any:
        return self.conn.cursor()

    def _commit(self) -> None:
        try:
            if self.is_postgres:
                self.conn.commit()
            else:
                self.conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            if self.is_postgres:
                self.conn.rollback()
            elif self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        except Exception:  # pragma: no cover - la conexión ya estaba rota
            log.exception("Fallo al hacer rollback")

    # -- transacciones ------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Agrupa varias sentencias en una transacción atómica (anidable)."""
        exterior = self._depth == 0
        if exterior and not self.is_postgres:
            # BEGIN IMMEDIATE: coge el bloqueo de escritura desde el principio y
            # evita SQLITE_BUSY al promocionar una lectura a escritura.
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if exterior:
                self._rollback()
            raise
        else:
            self._depth -= 1
            if exterior:
                self._commit()

    # -- lectura ------------------------------------------------------------
    def query(self, sql: str, args: Sequence[Any] = (), one: bool = False) -> Any:
        """Ejecuta un SELECT. Devuelve lista de dicts, o un dict/None si ``one``."""
        cur = self._cursor()
        try:
            cur.execute(self._sql(sql), tuple(args))
            filas = [_normalizar_fila(f) for f in cur.fetchall()]
        finally:
            cur.close()
        if self.is_postgres and self._depth == 0:
            self.conn.rollback()  # cierra la transacción implícita de lectura
        if one:
            return filas[0] if filas else None
        return filas

    def scalar(self, sql: str, args: Sequence[Any] = (), default: Any = None) -> Any:
        fila = self.query(sql, args, one=True)
        if not fila:
            return default
        valor = next(iter(fila.values()))
        return default if valor is None else valor

    # -- escritura ----------------------------------------------------------
    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        """Ejecuta INSERT/UPDATE/DELETE/DDL. Devuelve las filas afectadas."""
        with self.transaction():
            cur = self._cursor()
            try:
                cur.execute(self._sql(sql), tuple(args))
                return max(cur.rowcount, 0)
            finally:
                cur.close()

    def insert(self, sql: str, args: Sequence[Any] = ()) -> int:
        """INSERT que devuelve el ``id`` generado (portable)."""
        with self.transaction():
            cur = self._cursor()
            try:
                if self.is_postgres:
                    cur.execute(self._sql(sql) + " RETURNING id", tuple(args))
                    return int(cur.fetchone()["id"])
                cur.execute(sql, tuple(args))
                return int(cur.lastrowid)
            finally:
                cur.close()

    def insert_many(self, tabla: str, columnas: Sequence[str],
                    filas: Iterable[Sequence[Any]]) -> int:
        """Inserta muchas filas con sentencias multi-VALUES (pocas idas y vueltas).

        ``tabla`` y ``columnas`` son siempre literales del código, nunca entrada
        del usuario. Se trocea para respetar el límite de variables de SQLite.
        """
        filas = list(filas)
        if not filas:
            return 0
        por_trozo = max(1, 900 // len(columnas))
        marcador = "(" + ", ".join("?" for _ in columnas) + ")"
        cabecera = f"INSERT INTO {tabla} ({', '.join(columnas)}) VALUES "
        total = 0
        with self.transaction():
            for i in range(0, len(filas), por_trozo):
                trozo = filas[i:i + por_trozo]
                plano: List[Any] = [v for fila in trozo for v in fila]
                total += self.execute(cabecera + ", ".join([marcador] * len(trozo)), plano)
        return total

    # -- ciclo de vida ------------------------------------------------------
    def close(self) -> None:
        try:
            if self._depth > 0 or self.is_postgres or getattr(self.conn, "in_transaction", False):
                self._rollback()
        finally:
            try:
                self.conn.close()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def _url_con_ssl(url: str) -> str:
    """Añade ``sslmode=require`` a URLs remotas (Neon lo exige) si falta."""
    if "sslmode=" in url:
        return url
    host = (urlparse(url).hostname or "").lower()
    if host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("/"):
        return url
    return url + ("&" if "?" in url else "?") + "sslmode=require"


def connect(config: Dict[str, Any]) -> Database:
    """Abre una conexión nueva según la configuración."""
    url = config.get("DATABASE_URL")
    if url:
        try:
            import psycopg2  # import perezoso: SQLite no lo necesita
            import psycopg2.extras
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL apunta a PostgreSQL pero falta el controlador. "
                "Instálalo con: pip install psycopg2-binary") from exc

        conexion = psycopg2.connect(
            _url_con_ssl(url),
            cursor_factory=psycopg2.extras.RealDictCursor,
            connect_timeout=10,
            application_name="findash",
        )
        conexion.autocommit = False
        return Database(POSTGRES, conexion)

    ruta = config["DATABASE_PATH"]
    carpeta = os.path.dirname(os.path.abspath(ruta))
    os.makedirs(carpeta, exist_ok=True)
    conexion = sqlite3.connect(ruta, timeout=15, isolation_level=None)  # autocommit
    conexion.row_factory = sqlite3.Row
    conexion.execute("PRAGMA foreign_keys = ON")
    return Database(SQLITE, conexion)


def get_db() -> Database:
    """Conexión de la petición actual (una por petición)."""
    db = g.get("_findash_db")
    if db is None:
        db = g._findash_db = connect(current_app.config)
    return db


def close_db(_error: Optional[BaseException] = None) -> None:
    db = g.pop("_findash_db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------
def _ddl(dialecto: str) -> List[str]:
    pg = dialecto == POSTGRES
    pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    dinero = "NUMERIC(14,2)" if pg else "REAL"
    return [
        f"""CREATE TABLE IF NOT EXISTS usuarios (
                id {pk},
                nombre TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                tema TEXT DEFAULT 'dark',
                privacidad INTEGER DEFAULT 0,
                animaciones INTEGER DEFAULT 0,
                wrapped_visto TEXT,
                creado_en TEXT
            )""",
        f"""CREATE TABLE IF NOT EXISTS movimientos (
                id {pk},
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                fecha TEXT NOT NULL,
                concepto TEXT NOT NULL,
                categoria TEXT NOT NULL,
                importe {dinero} NOT NULL,
                tipo TEXT NOT NULL CHECK (tipo IN ('ingreso', 'gasto')),
                demo INTEGER NOT NULL DEFAULT 0
            )""",
        f"""CREATE TABLE IF NOT EXISTS presupuestos (
                id {pk},
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                categoria TEXT NOT NULL,
                importe_mensual {dinero} NOT NULL,
                UNIQUE (usuario_id, categoria)
            )""",
        f"""CREATE TABLE IF NOT EXISTS metas (
                id {pk},
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                nombre TEXT NOT NULL,
                cantidad_objetivo {dinero} NOT NULL,
                cantidad_actual {dinero} NOT NULL DEFAULT 0,
                fecha_objetivo TEXT
            )""",
        f"""CREATE TABLE IF NOT EXISTS recurrentes (
                id {pk},
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                concepto TEXT NOT NULL,
                categoria TEXT NOT NULL,
                importe {dinero} NOT NULL,
                tipo TEXT NOT NULL CHECK (tipo IN ('ingreso', 'gasto')),
                dia_mes INTEGER NOT NULL CHECK (dia_mes BETWEEN 1 AND 31),
                ultimo_mes_procesado TEXT,
                activo INTEGER NOT NULL DEFAULT 1
            )""",
        f"""CREATE TABLE IF NOT EXISTS reglas_categoria (
                id {pk},
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL CHECK (tipo IN ('ingreso', 'gasto')),
                patron TEXT NOT NULL,
                categoria TEXT NOT NULL,
                UNIQUE (usuario_id, tipo, patron)
            )""",
        """CREATE TABLE IF NOT EXISTS login_intentos (
                clave TEXT PRIMARY KEY,
                fallos INTEGER NOT NULL DEFAULT 0,
                ultimo_fallo BIGINT NOT NULL
            )""",
    ]


def _indices() -> List[str]:
    return [
        "CREATE INDEX IF NOT EXISTS idx_mov_usuario_fecha ON movimientos (usuario_id, fecha)",
        "CREATE INDEX IF NOT EXISTS idx_rec_usuario ON recurrentes (usuario_id)",
        "CREATE INDEX IF NOT EXISTS idx_reglas_usuario ON reglas_categoria (usuario_id)",
    ]


# Columnas que pueden faltar en bases de datos creadas por versiones anteriores.
_COLUMNAS_OPCIONALES = {
    "usuarios": {
        "tema": "TEXT DEFAULT 'dark'",
        "privacidad": "INTEGER DEFAULT 0",
        "animaciones": "INTEGER DEFAULT 0",
        "wrapped_visto": "TEXT",
        "creado_en": "TEXT",
    },
    "metas": {"fecha_objetivo": "TEXT"},
    "movimientos": {"demo": "INTEGER NOT NULL DEFAULT 0"},
}

# Columnas sin las cuales la aplicación no puede funcionar.
_COLUMNAS_REQUERIDAS = {
    "usuarios": {"id", "nombre", "email", "password"},
    "movimientos": {"id", "usuario_id", "fecha", "concepto", "categoria", "importe", "tipo"},
    "presupuestos": {"id", "usuario_id", "categoria", "importe_mensual"},
    "metas": {"id", "usuario_id", "nombre", "cantidad_objetivo", "cantidad_actual"},
    "recurrentes": {"id", "usuario_id", "concepto", "categoria", "importe", "tipo",
                    "dia_mes", "ultimo_mes_procesado", "activo"},
    "reglas_categoria": {"id", "usuario_id", "tipo", "patron", "categoria"},
}


def _columnas_de(db: Database, tabla: str) -> set:
    if db.is_postgres:
        filas = db.query(
            "SELECT column_name AS nombre FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = current_schema()", (tabla,))
    else:
        filas = [{"nombre": f["name"]} for f in
                 [dict(r) for r in db.conn.execute(f"PRAGMA table_info({tabla})").fetchall()]]
    return {f["nombre"] for f in filas}


def init_schema(db: Database) -> None:
    """Crea las tablas que falten, añade columnas nuevas y verifica el esquema.

    Es idempotente y segura con varios procesos arrancando a la vez (en
    PostgreSQL se serializa con un bloqueo asesor).
    """
    with db.transaction():
        if db.is_postgres:
            db.execute("SELECT pg_advisory_xact_lock(727274)")
        for sentencia in _ddl(db.dialect):
            db.execute(sentencia)
        for tabla, columnas in _COLUMNAS_OPCIONALES.items():
            existentes = _columnas_de(db, tabla)
            for columna, definicion in columnas.items():
                if columna not in existentes:
                    log.info("Migración: añadiendo %s.%s", tabla, columna)
                    db.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")
        problemas = []
        for tabla, requeridas in _COLUMNAS_REQUERIDAS.items():
            faltan = requeridas - _columnas_de(db, tabla)
            if faltan:
                problemas.append(f"{tabla}: faltan {', '.join(sorted(faltan))}")
        if not problemas:                 # los índices van al final: dependen de que las columnas existan
            for sentencia in _indices():
                db.execute(sentencia)
        if problemas:
            raise SchemaError(
                "El esquema existente no es compatible con esta versión de FinDash ("
                + "; ".join(problemas)
                + "). Si es una base de datos de pruebas antigua (p. ej. el esquema "
                "con 'es_ingreso'), vacíala o apunta DATABASE_URL a una base nueva. "
                "FinDash nunca borra tablas por su cuenta."
            )


def init_app(app: Any) -> None:
    app.teardown_appcontext(close_db)
    if app.config.get("AUTO_INIT_DB", True):
        db = connect(app.config)
        try:
            init_schema(db)
        finally:
            db.close()
