"""Utilidades comunes para las pruebas de la aplicación."""
from __future__ import annotations

import datetime
import os
import re
import shutil
import tempfile
import unittest
from typing import Any, Dict, Optional

from findash import create_app
from findash.db import connect, init_schema

PG_URL = os.environ.get("FINDASH_TEST_DATABASE_URL")
_TABLAS = ["login_intentos", "reglas_categoria", "recurrentes", "metas", "presupuestos", "movimientos", "usuarios"]


class AppTestCase(unittest.TestCase):
    """Cada test arranca con una base de datos vacía (SQLite temporal o PostgreSQL)."""

    config_extra: Dict[str, Any] = {}

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="findash_test_")
        cfg: Dict[str, Any] = {
            "TESTING": True, "SECRET_KEY": "x" * 40, "INSTANCE_DIR": self.tmp,
            "DATABASE_PATH": os.path.join(self.tmp, "t.db"), "DATABASE_URL": PG_URL,
            "LOGIN_MAX_FALLOS": 5, "REGISTRO_MAX_POR_IP": 50,
        }
        cfg.update(self.config_extra)
        self.app = create_app(cfg)
        self._vaciar()
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- base de datos --------------------------------------------------------------------
    def conexion(self):
        return connect(self.app.config)

    def _vaciar(self) -> None:
        db = self.conexion()
        try:
            init_schema(db)
            if db.is_postgres:
                db.execute("TRUNCATE " + ", ".join(_TABLAS) + " RESTART IDENTITY CASCADE")
        finally:
            db.close()

    def sql(self, consulta: str, args=(), one: bool = False):
        db = self.conexion()
        try:
            return db.query(consulta, args, one=one)
        finally:
            db.close()

    def exec_sql(self, consulta: str, args=()) -> int:
        db = self.conexion()
        try:
            return db.execute(consulta, args)
        finally:
            db.close()

    # -- peticiones -----------------------------------------------------------------------
    @staticmethod
    def token_de(html: str) -> str:
        m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html) or \
            re.search(r'name="csrf-token" content="([0-9a-f]+)"', html)
        assert m, "no hay token CSRF en la página"
        return m.group(1)

    def token(self, client=None, url: str = "/login") -> str:
        c = client or self.client
        return self.token_de(c.get(url).get_data(as_text=True))

    def registrar(self, client=None, email: str = "ana@example.com", nombre: str = "Ana",
                  password: str = "secreto123"):
        c = client or self.client
        t = self.token(c, "/registro")
        return c.post("/registro", data={"csrf_token": t, "nombre": nombre, "email": email,
                                         "password": password, "password2": password})

    def entrar(self, client=None, email: str = "ana@example.com", password: str = "secreto123"):
        c = client or self.client
        t = self.token(c, "/login")
        return c.post("/login", data={"csrf_token": t, "email": email, "password": password})

    def cliente_con_sesion(self, email: str = "ana@example.com", nombre: str = "Ana"):
        """Cliente nuevo con un usuario registrado y la sesión iniciada."""
        c = self.app.test_client()
        self.registrar(c, email, nombre)
        r = self.entrar(c, email)
        assert r.status_code == 302, r.status_code
        return c

    def iniciar(self, email: str = "ana@example.com", nombre: str = "Ana") -> None:
        self.client = self.cliente_con_sesion(email, nombre)

    def post(self, url: str, datos: Optional[Dict[str, Any]] = None, client=None, pagina: str = "/panel/transactions",
             **kw):
        c = client or self.client
        datos = dict(datos or {})
        datos.setdefault("csrf_token", self.token(c, pagina))
        return c.post(url, data=datos, **kw)

    def post_json(self, url: str, datos: Dict[str, Any], client=None, pagina: str = "/panel/transactions"):
        c = client or self.client
        return c.post(url, json=datos, headers={"X-CSRFToken": self.token(c, pagina), "Accept": "application/json"})

    @property
    def uid(self) -> int:
        return int(self.sql("SELECT id FROM usuarios ORDER BY id LIMIT 1", one=True)["id"])

    def anadir_mov(self, fecha: str, concepto: str, importe: float, tipo: str = "gasto",
                   categoria: str = "Gastos Generales", uid: Optional[int] = None) -> None:
        self.exec_sql(
            "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)",
            (uid or self.uid, fecha, concepto, categoria, importe, tipo))
