"""Capa de base de datos unificada (SQLite y PostgreSQL), esquema y migraciones."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from findash import config as cfg
from findash.db import (
    SchemaError, _a_postgres, _url_con_ssl, connect, es_error_integridad, init_schema,
)
from tests.base import PG_URL, AppTestCase


class TestTraduccion(unittest.TestCase):
    def test_marcadores(self):
        self.assertEqual(_a_postgres("SELECT * FROM t WHERE a = ? AND b = ?"), "SELECT * FROM t WHERE a = %s AND b = %s")

    def test_sql_con_porcentaje_se_rechaza(self):
        with self.assertRaises(ValueError):
            _a_postgres("SELECT strftime('%Y-%m', fecha) FROM t")

    def test_ssl_en_urls_remotas(self):
        self.assertTrue(_url_con_ssl("postgresql://u:p@ep-x.neon.tech/db").endswith("?sslmode=require"))
        self.assertTrue(_url_con_ssl("postgresql://u:p@ep-x.neon.tech/db?x=1").endswith("&sslmode=require"))
        self.assertEqual(_url_con_ssl("postgresql://u:p@localhost/db"), "postgresql://u:p@localhost/db")
        self.assertEqual(_url_con_ssl("postgresql://u:p@h/db?sslmode=disable"), "postgresql://u:p@h/db?sslmode=disable")

    def test_normalizar_url(self):
        self.assertEqual(cfg.normalizar_database_url("postgres://a@b/c"), "postgresql://a@b/c")
        self.assertIsNone(cfg.normalizar_database_url(""))
        self.assertIsNone(cfg.normalizar_database_url(None))


class TestBaseDeDatos(AppTestCase):
    def setUp(self):
        super().setUp()
        self.db = self.conexion()
        self.db.execute("INSERT INTO usuarios (nombre, email, password) VALUES ('A', 'a@a.es', 'x')")
        self.u = self.db.scalar("SELECT id FROM usuarios WHERE email = 'a@a.es'")

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_tablas_requeridas(self):
        tablas = {"usuarios", "movimientos", "presupuestos", "metas", "recurrentes"}
        for t in tablas:
            self.assertEqual(self.db.scalar(f"SELECT COUNT(*) FROM {t}"), 1 if t == "usuarios" else 0, t)

    def test_insert_devuelve_id(self):
        a = self.db.insert("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'A', 10)", (self.u,))
        b = self.db.insert("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'B', 10)", (self.u,))
        self.assertEqual(b, a + 1)

    def test_tipos_normalizados(self):
        self.db.execute("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) "
                        "VALUES (?, '2026-01-02', 'x', 'c', 12.35, 'gasto')", (self.u,))
        fila = self.db.query("SELECT fecha, importe FROM movimientos", one=True)
        self.assertIsInstance(fila["importe"], float)             # Decimal -> float en PostgreSQL
        self.assertEqual(fila["fecha"], "2026-01-02")
        self.assertIsInstance(fila["fecha"], str)
        self.assertEqual(fila["importe"], 12.35)

    def test_funciones_de_fecha_portables(self):
        for d, imp in (("2026-01-05", 1), ("2026-01-20", 2), ("2026-02-03", 4)):
            self.db.execute("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) "
                            "VALUES (?, ?, 'x', 'c', ?, 'gasto')", (self.u, d, imp))
        filas = self.db.query("SELECT SUBSTR(fecha, 1, 7) AS mes, SUM(importe) AS t FROM movimientos "
                              "GROUP BY SUBSTR(fecha, 1, 7) ORDER BY mes")
        self.assertEqual([(f["mes"], f["t"]) for f in filas], [("2026-01", 3.0), ("2026-02", 4.0)])

    def test_transaccion_confirma(self):
        with self.db.transaction():
            self.db.execute("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'A', 1)", (self.u,))
            self.db.execute("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'B', 1)", (self.u,))
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM metas", one=True)["n"], 2)

    def test_transaccion_revierte_si_falla(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.execute("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'A', 1)", (self.u,))
                raise RuntimeError("boom")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM metas", one=True)["n"], 0)
        # la conexión sigue siendo utilizable
        self.db.execute("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'C', 1)", (self.u,))
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM metas", one=True)["n"], 1)

    def test_transacciones_anidadas(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                with self.db.transaction():
                    self.db.execute("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'A', 1)", (self.u,))
                raise RuntimeError("fallo exterior")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM metas", one=True)["n"], 0)

    def test_error_de_integridad_revierte_y_se_reconoce(self):
        with self.assertRaises(Exception) as ctx:
            self.db.execute("INSERT INTO usuarios (nombre, email, password) VALUES ('B', 'a@a.es', 'x')")
        self.assertTrue(es_error_integridad(ctx.exception))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM usuarios"), 1)     # la conexión no queda envenenada

    def test_check_de_tipo(self):
        with self.assertRaises(Exception) as ctx:
            self.db.execute("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) "
                            "VALUES (?, '2026-01-01', 'x', 'c', 1, 'otro')", (self.u,))
        self.assertTrue(es_error_integridad(ctx.exception))

    def test_insert_many_en_trozos(self):
        filas = [(self.u, "2026-01-01", f"c{i}", "cat", float(i + 1), "gasto") for i in range(2500)]
        n = self.db.insert_many("movimientos", ("usuario_id", "fecha", "concepto", "categoria", "importe", "tipo"), filas)
        self.assertEqual(n, 2500)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM movimientos"), 2500)
        self.assertEqual(self.db.insert_many("movimientos", ("usuario_id",), []), 0)

    def test_insert_many_atomico(self):
        filas = [(self.u, "2026-01-01", "ok", "cat", 1.0, "gasto")] * 1500 + [(self.u, "2026-01-01", "mal", "cat", 1.0, "xx")]
        with self.assertRaises(Exception):
            self.db.insert_many("movimientos", ("usuario_id", "fecha", "concepto", "categoria", "importe", "tipo"), filas)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM movimientos"), 0)

    def test_upsert(self):
        sql = ("INSERT INTO reglas_categoria (usuario_id, tipo, patron, categoria) VALUES (?, 'gasto', 'x', ?) "
               "ON CONFLICT (usuario_id, tipo, patron) DO UPDATE SET categoria = excluded.categoria")
        self.db.execute(sql, (self.u, "A"))
        self.db.execute(sql, (self.u, "B"))
        self.assertEqual(self.db.query("SELECT categoria FROM reglas_categoria"), [{"categoria": "B"}])

    def test_borrado_en_cascada(self):
        self.db.execute("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) "
                        "VALUES (?, '2026-01-01', 'x', 'c', 1, 'gasto')", (self.u,))
        self.db.execute("DELETE FROM usuarios WHERE id = ?", (self.u,))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM movimientos"), 0)

    def test_dos_conexiones_no_se_pisan(self):
        otra = self.conexion()
        try:
            otra.execute("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'otra', 1)", (self.u,))
            self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM metas"), 1)
        finally:
            otra.close()

    def test_las_conexiones_se_cierran_al_acabar_la_peticion(self):
        self.iniciar()
        for _ in range(30):
            self.client.get("/panel")
        # si se filtraran conexiones, SQLite acabaría bloqueado y PostgreSQL agotaría max_connections
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_esquema_idempotente(self):
        init_schema(self.db)
        init_schema(self.db)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM usuarios"), 1)


@unittest.skipIf(PG_URL, "la prueba de migración usa un SQLite propio")
class TestMigracionDeEsquemaAntiguo(unittest.TestCase):
    """La base de datos real de producción (esquema original) debe abrirse sin perder nada."""
    ORIGINAL = """
        CREATE TABLE usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT, email TEXT UNIQUE, password TEXT);
        CREATE TABLE movimientos (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, fecha TEXT, concepto TEXT,
                                  categoria TEXT, importe REAL, tipo TEXT);
        CREATE TABLE presupuestos (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, categoria TEXT, importe_mensual REAL);
        CREATE TABLE metas (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, nombre TEXT, cantidad_objetivo REAL,
                            cantidad_actual REAL DEFAULT 0);
        INSERT INTO usuarios (nombre, email, password) VALUES ('Vieja', 'vieja@x.es', 'hash');
        INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (1, '2025-05-05', 'Cafe', 'Ocio', 2.5, 'gasto');
        INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (1, 'Viaje', 500);
    """

    def test_migracion_conserva_los_datos(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "viejo.db")
            con = sqlite3.connect(ruta)
            con.executescript(self.ORIGINAL)
            con.commit()
            con.close()
            db = connect({"DATABASE_URL": None, "DATABASE_PATH": ruta})
            try:
                init_schema(db)
                self.assertEqual(db.scalar("SELECT COUNT(*) FROM movimientos"), 1)
                self.assertEqual(db.scalar("SELECT categoria FROM movimientos"), "Ocio")      # categoría antigua intacta
                self.assertEqual(db.scalar("SELECT nombre FROM usuarios"), "Vieja")
                self.assertEqual(db.scalar("SELECT COUNT(*) FROM recurrentes"), 0)
                db.execute("UPDATE usuarios SET tema = 'light', wrapped_visto = '2025-05' WHERE id = 1")   # columnas añadidas
                self.assertEqual(db.scalar("SELECT demo FROM movimientos"), 0)
                init_schema(db)                                                                  # segunda vez: no pasa nada
            finally:
                db.close()

    def test_esquema_incompatible_no_se_destruye(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "raro.db")
            con = sqlite3.connect(ruta)
            con.executescript("CREATE TABLE usuarios (id INTEGER PRIMARY KEY, email TEXT);"
                              "CREATE TABLE movimientos (id INTEGER PRIMARY KEY, es_ingreso INTEGER);"
                              "INSERT INTO usuarios VALUES (1, 'a@a.es');")
            con.commit()
            con.close()
            db = connect({"DATABASE_URL": None, "DATABASE_PATH": ruta})
            try:
                with self.assertRaises(SchemaError):
                    init_schema(db)
                self.assertEqual(db.scalar("SELECT COUNT(*) FROM usuarios"), 1)               # no se borró nada
            finally:
                db.close()


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_produccion_exige_secret_key(self):
        os.environ["FINDASH_ENV"] = "production"
        os.environ.pop("SECRET_KEY", None)
        with self.assertRaises(RuntimeError):
            cfg.construir_config({"INSTANCE_DIR": tempfile.mkdtemp()})

    def test_secret_key_corta_se_rechaza(self):
        os.environ["SECRET_KEY"] = "corta"
        with self.assertRaises(RuntimeError):
            cfg.construir_config()

    def test_produccion_activa_cookie_segura(self):
        os.environ.update({"FINDASH_ENV": "production", "SECRET_KEY": "k" * 40})
        c = cfg.construir_config()
        self.assertTrue(c["SESSION_COOKIE_SECURE"])
        self.assertFalse(c["DEBUG"])

    def test_desarrollo_persiste_una_clave_local(self):
        os.environ.pop("SECRET_KEY", None)
        os.environ["FINDASH_ENV"] = "development"
        d = tempfile.mkdtemp()
        a = cfg.construir_config({"INSTANCE_DIR": d})["SECRET_KEY"]
        b = cfg.construir_config({"INSTANCE_DIR": d})["SECRET_KEY"]
        self.assertEqual(a, b)
        self.assertNotIn("findash_secreto", a)

    def test_database_url_postgres(self):
        os.environ.update({"DATABASE_URL": "postgres://u:p@h/db", "SECRET_KEY": "k" * 40})
        self.assertEqual(cfg.construir_config()["DATABASE_URL"], "postgresql://u:p@h/db")

    def test_valores_vacios_usan_el_valor_por_defecto(self):
        # Un .env copiado de .env.example deja claves vacías: no deben desactivar nada.
        os.environ.update({"FINDASH_ENV": "production", "SECRET_KEY": "k" * 40,
                           "SESSION_COOKIE_SECURE": "", "GOOGLE_AUD_PREFIX": "", "DATABASE_PATH": ""})
        c = cfg.construir_config()
        self.assertTrue(c["SESSION_COOKIE_SECURE"])
        self.assertEqual(c["GOOGLE_AUD_PREFIX"], "566983233923-")
        self.assertTrue(c["DATABASE_PATH"].endswith("database.db"))

    def test_dotenv_no_pisa_el_entorno_ni_carga_vacios(self):
        os.environ.pop("FINDASH_NO_DOTENV", None)
        for k in ("FD_A", "FD_B", "FD_C", "FD_D", "FD_E", "FD_F"):
            os.environ.pop(k, None)
        os.environ["FD_B"] = "del-entorno"
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / ".env"
            ruta.write_text("# comentario\nFD_A=uno\nFD_B=del-fichero\nexport FD_C=\"con espacios\"\n"
                            "FD_D='x=y'\nFD_E=\nFD_F=valor # comentario\nsin_igual\n", encoding="utf-8")
            cfg.cargar_dotenv(ruta)
        self.assertEqual(os.environ["FD_A"], "uno")
        self.assertEqual(os.environ["FD_B"], "del-entorno")
        self.assertEqual(os.environ["FD_C"], "con espacios")
        self.assertEqual(os.environ["FD_D"], "x=y")
        self.assertNotIn("FD_E", os.environ)
        self.assertEqual(os.environ["FD_F"], "valor")
        cfg.cargar_dotenv(Path("/no/existe/.env"))          # sin fichero: no pasa nada


if __name__ == "__main__":
    unittest.main()
