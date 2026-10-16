"""Controlador mínimo con la API de ``psycopg2`` construido sobre libpq (ctypes).

SOLO PARA PRUEBAS. Existe para poder ejecutar la suite contra un servidor
PostgreSQL real en entornos donde no se puede instalar ``psycopg2-binary``.
En un entorno normal (``pip install psycopg2-binary``) este módulo no se usa:
``tests/__init__.py`` solo lo instala si ``import psycopg2`` falla.

Imita lo que usa FinDash: ``connect(dsn, cursor_factory=RealDictCursor, ...)``,
``autocommit``, ``cursor().execute(sql, args)`` con marcadores ``%s``,
``fetchone/fetchall/rowcount``, transacciones implícitas (BEGIN al primer
comando; ``commit()``/``rollback()``), conversión de ``NUMERIC`` a ``Decimal`` y
la jerarquía de excepciones (``IntegrityError``...). Los parámetros se
interpolan en el cliente con ``PQescapeLiteral``, igual que hace psycopg2.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import datetime
import decimal
import sys
import types
from typing import Any, Dict, List, Optional, Sequence

_ruta = ctypes.util.find_library("pq") or "libpq.so.5"
_pq = ctypes.CDLL(_ruta)

_c = ctypes
for _nombre, _res, _args in [
    ("PQconnectdb", _c.c_void_p, [_c.c_char_p]),
    ("PQstatus", _c.c_int, [_c.c_void_p]),
    ("PQerrorMessage", _c.c_char_p, [_c.c_void_p]),
    ("PQfinish", None, [_c.c_void_p]),
    ("PQexec", _c.c_void_p, [_c.c_void_p, _c.c_char_p]),
    ("PQresultStatus", _c.c_int, [_c.c_void_p]),
    ("PQresultErrorMessage", _c.c_char_p, [_c.c_void_p]),
    ("PQresultErrorField", _c.c_char_p, [_c.c_void_p, _c.c_int]),
    ("PQntuples", _c.c_int, [_c.c_void_p]),
    ("PQnfields", _c.c_int, [_c.c_void_p]),
    ("PQfname", _c.c_char_p, [_c.c_void_p, _c.c_int]),
    ("PQftype", _c.c_uint, [_c.c_void_p, _c.c_int]),
    ("PQgetvalue", _c.c_char_p, [_c.c_void_p, _c.c_int, _c.c_int]),
    ("PQgetisnull", _c.c_int, [_c.c_void_p, _c.c_int, _c.c_int]),
    ("PQcmdTuples", _c.c_char_p, [_c.c_void_p]),
    ("PQclear", None, [_c.c_void_p]),
    ("PQescapeLiteral", _c.c_void_p, [_c.c_void_p, _c.c_char_p, _c.c_size_t]),
    ("PQfreemem", None, [_c.c_void_p]),
    ("PQtransactionStatus", _c.c_int, [_c.c_void_p]),
    ("PQsetClientEncoding", _c.c_int, [_c.c_void_p, _c.c_char_p]),
]:
    _f = getattr(_pq, _nombre)
    _f.restype, _f.argtypes = _res, _args

_NOTICE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)
_pq.PQsetNoticeProcessor.restype = ctypes.c_void_p
_pq.PQsetNoticeProcessor.argtypes = [ctypes.c_void_p, _NOTICE_CB, ctypes.c_void_p]
_ignorar_aviso = _NOTICE_CB(lambda _arg, _msg: None)   # los NOTICE de «IF NOT EXISTS» no son errores

CONNECTION_OK = 0
PGRES_COMMAND_OK, PGRES_TUPLES_OK, PGRES_EMPTY_QUERY = 1, 2, 0
PQTRANS_IDLE = 0
PG_DIAG_SQLSTATE = ord("C")


# --- excepciones (misma jerarquía que psycopg2) ---------------------------------------------
class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class OperationalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class DataError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class UniqueViolation(IntegrityError):
    pass


class errors:                      # noqa: N801 - imita psycopg2.errors
    UniqueViolation = UniqueViolation


def _excepcion(sqlstate: str, mensaje: str) -> Exception:
    if sqlstate == "23505":
        return UniqueViolation(mensaje)
    if sqlstate.startswith("23"):
        return IntegrityError(mensaje)
    if sqlstate.startswith("22"):
        return DataError(mensaje)
    if sqlstate.startswith(("08", "53", "57", "58")):
        return OperationalError(mensaje)
    if sqlstate.startswith("25") or sqlstate.startswith("40"):
        return InternalError(mensaje)
    return ProgrammingError(mensaje)


# --- conversión de tipos de salida -----------------------------------------------------------
_OID_BOOL, _OID_INT8, _OID_INT2, _OID_INT4, _OID_FLOAT4, _OID_FLOAT8, _OID_NUMERIC, _OID_DATE = (
    16, 20, 21, 23, 700, 701, 1700, 1082)


def _convertir(oid: int, texto: str) -> Any:
    if oid in (_OID_INT2, _OID_INT4, _OID_INT8):
        return int(texto)
    if oid in (_OID_FLOAT4, _OID_FLOAT8):
        return float(texto)
    if oid == _OID_NUMERIC:
        return decimal.Decimal(texto)
    if oid == _OID_BOOL:
        return texto == "t"
    if oid == _OID_DATE:
        return datetime.date.fromisoformat(texto)
    return texto


class RealDictCursor:                 # marcador, como en psycopg2.extras
    pass


class _extras(types.ModuleType):
    RealDictCursor = RealDictCursor


# --- conexión y cursor -----------------------------------------------------------------------
class Connection:
    def __init__(self, dsn: str, cursor_factory: Any = None, **opciones: Any) -> None:
        validas = {k: v for k, v in opciones.items() if v is not None}
        if dsn.startswith(("postgres://", "postgresql://")):
            # Formato URI: las opciones adicionales van en la query string.
            extra = "&".join(f"{k}={v}" for k, v in validas.items())
            conninfo = dsn + (("&" if "?" in dsn else "?") + extra if extra else "")
        else:
            conninfo = " ".join([dsn] + [f"{k}={v}" for k, v in validas.items()])
        self._h = _pq.PQconnectdb(conninfo.encode())
        if not self._h or _pq.PQstatus(self._h) != CONNECTION_OK:
            mensaje = (_pq.PQerrorMessage(self._h) or b"").decode(errors="replace")
            if self._h:
                _pq.PQfinish(self._h)
            self._h = None
            raise OperationalError(mensaje.strip() or "no se pudo conectar")
        _pq.PQsetNoticeProcessor(self._h, _ignorar_aviso, None)
        _pq.PQsetClientEncoding(self._h, b"UTF8")
        self.autocommit = False
        self.closed = 0

    # -- interno
    def _ejecutar(self, sql: str):
        res = _pq.PQexec(self._h, sql.encode("utf-8"))
        if not res:
            raise OperationalError((_pq.PQerrorMessage(self._h) or b"").decode(errors="replace"))
        estado = _pq.PQresultStatus(res)
        if estado not in (PGRES_COMMAND_OK, PGRES_TUPLES_OK, PGRES_EMPTY_QUERY):
            sqlstate = (_pq.PQresultErrorField(res, PG_DIAG_SQLSTATE) or b"").decode()
            mensaje = (_pq.PQresultErrorMessage(res) or b"").decode(errors="replace").strip()
            _pq.PQclear(res)
            raise _excepcion(sqlstate, mensaje)
        return res

    def _literal(self, valor: Any) -> str:
        if valor is None:
            return "NULL"
        if isinstance(valor, bool):
            return "true" if valor else "false"
        if isinstance(valor, int):
            return str(valor)
        if isinstance(valor, float):
            if valor != valor or valor in (float("inf"), float("-inf")):
                return "'%s'::float8" % repr(valor)
            return repr(valor)
        if isinstance(valor, decimal.Decimal):
            return str(valor)
        if isinstance(valor, (datetime.datetime, datetime.date)):
            valor = valor.isoformat()
        if isinstance(valor, (bytes, bytearray)):
            valor = bytes(valor).decode("utf-8")
        datos = str(valor).encode("utf-8")
        if b"\x00" in datos:
            raise DataError("A string literal cannot contain NUL (0x00) characters.")
        escapado = _pq.PQescapeLiteral(self._h, datos, len(datos))
        try:
            return ctypes.string_at(escapado).decode("utf-8")
        finally:
            _pq.PQfreemem(escapado)

    def _begin_si_hace_falta(self) -> None:
        if not self.autocommit and _pq.PQtransactionStatus(self._h) == PQTRANS_IDLE:
            _pq.PQclear(self._ejecutar("BEGIN"))

    # -- API pública
    def cursor(self, cursor_factory: Any = None) -> "Cursor":
        if self._h is None:
            raise InterfaceError("connection already closed")
        return Cursor(self)

    def commit(self) -> None:
        if self._h is not None and _pq.PQtransactionStatus(self._h) != PQTRANS_IDLE:
            _pq.PQclear(self._ejecutar("COMMIT"))

    def rollback(self) -> None:
        if self._h is not None and _pq.PQtransactionStatus(self._h) != PQTRANS_IDLE:
            _pq.PQclear(self._ejecutar("ROLLBACK"))

    def close(self) -> None:
        if self._h is not None:
            _pq.PQfinish(self._h)
            self._h = None
            self.closed = 1


class Cursor:
    def __init__(self, conexion: Connection) -> None:
        self._con = conexion
        self._filas: List[Dict[str, Any]] = []
        self._pos = 0
        self.rowcount = -1

    def execute(self, sql: str, args: Optional[Sequence[Any]] = None) -> None:
        con = self._con
        if args:
            partes = sql.split("%s")
            if len(partes) - 1 != len(args):
                raise ProgrammingError(f"número de parámetros incorrecto ({len(partes) - 1} marcadores, {len(args)} valores)")
            sql = partes[0] + "".join(con._literal(a) + resto for a, resto in zip(args, partes[1:]))
        con._begin_si_hace_falta()
        res = con._ejecutar(sql)
        try:
            self._filas, self._pos = [], 0
            if _pq.PQresultStatus(res) == PGRES_TUPLES_OK:
                columnas = [(_pq.PQfname(res, j).decode(), _pq.PQftype(res, j)) for j in range(_pq.PQnfields(res))]
                for i in range(_pq.PQntuples(res)):
                    fila = {}
                    for j, (nombre, oid) in enumerate(columnas):
                        fila[nombre] = None if _pq.PQgetisnull(res, i, j) else _convertir(
                            oid, _pq.PQgetvalue(res, i, j).decode("utf-8"))
                    self._filas.append(fila)
                self.rowcount = len(self._filas)
            else:
                afectadas = (_pq.PQcmdTuples(res) or b"").decode()
                self.rowcount = int(afectadas) if afectadas else -1
        finally:
            _pq.PQclear(res)

    def fetchone(self) -> Optional[Dict[str, Any]]:
        if self._pos >= len(self._filas):
            return None
        self._pos += 1
        return self._filas[self._pos - 1]

    def fetchall(self) -> List[Dict[str, Any]]:
        resto = self._filas[self._pos:]
        self._pos = len(self._filas)
        return resto

    def close(self) -> None:
        self._filas = []


def connect(dsn: str, cursor_factory: Any = None, **opciones: Any) -> Connection:
    return Connection(dsn, cursor_factory, **opciones)


def instalar() -> None:
    """Registra este módulo como ``psycopg2`` (y ``psycopg2.extras``) en ``sys.modules``."""
    modulo = sys.modules[__name__]
    extras = _extras("psycopg2.extras")
    modulo.extras = extras                      # type: ignore[attr-defined]
    modulo.__path__ = []                        # type: ignore[attr-defined]  # se comporta como paquete
    sys.modules["psycopg2"] = modulo
    sys.modules["psycopg2.extras"] = extras
