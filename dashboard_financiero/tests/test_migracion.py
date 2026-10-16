"""Migración SQLite -> base nueva (SQLite siempre; PostgreSQL si FINDASH_TEST_DATABASE_URL)."""
import os
import sqlite3
import tempfile
import unittest

from findash.db import connect, init_schema
from findash.migracion import MigracionError, migrar

PG_URL = os.getenv("FINDASH_TEST_DATABASE_URL")

ORIGEN = """
    CREATE TABLE usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT, email TEXT UNIQUE, password TEXT);
    CREATE TABLE movimientos (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, fecha TEXT, concepto TEXT,
                              categoria TEXT, importe REAL, tipo TEXT);
    CREATE TABLE presupuestos (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, categoria TEXT, importe_mensual REAL);
    CREATE TABLE metas (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, nombre TEXT, cantidad_objetivo REAL,
                        cantidad_actual REAL DEFAULT 0);
    INSERT INTO usuarios (id, nombre, email, password) VALUES (7, 'Ana', 'Ana@Example.com ', 'hash1');
    INSERT INTO usuarios (id, nombre, email, password) VALUES (9, '', 'bob@example.com', NULL);
    INSERT INTO usuarios (id, nombre, email, password) VALUES (12, 'Otra Ana', 'ana@example.com', 'hash3');
    INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES
        (7, '2025-05-05', 'Cafe', 'Ocio', 2.5, 'gasto'),
        (7, '2025-05-06', 'Nomina', 'Nómina', 1500, 'ingreso'),
        (9, '2025-05-07', 'Gym', 'Salud', 30, 'gasto'),
        (99, '2025-05-07', 'Huérfano', 'Ocio', 5, 'gasto'),
        (7, '2025-05-08', 'Raro', 'Ocio', 5, 'transferencia'),
        (12, '2025-05-09', 'De la duplicada', 'Ocio', 5, 'gasto');
    INSERT INTO presupuestos (usuario_id, categoria, importe_mensual) VALUES (7, 'Ocio', 100);
    INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (7, 'Viaje', 500);
"""


class TestMigracion(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.origen = os.path.join(self.dir.name, "origen.db")
        con = sqlite3.connect(self.origen)
        con.executescript(ORIGEN)
        con.commit()
        con.close()
        if PG_URL:
            self.destino = connect({"DATABASE_URL": PG_URL})
            init_schema(self.destino)
            self.destino.execute("TRUNCATE usuarios, movimientos, presupuestos, metas, recurrentes, "
                                 "reglas_categoria, login_intentos RESTART IDENTITY CASCADE")
        else:
            self.destino = connect({"DATABASE_URL": None, "DATABASE_PATH": os.path.join(self.dir.name, "destino.db")})

    def tearDown(self):
        self.destino.close()
        self.dir.cleanup()

    def test_copia_conserva_ids_y_cuenta_lo_omitido(self):
        informe = migrar(self.origen, self.destino)
        por_tabla = {t.tabla: t for t in informe.tablas}
        self.assertEqual((por_tabla["usuarios"].en_origen, por_tabla["usuarios"].copiadas), (3, 2))
        self.assertEqual(por_tabla["usuarios"].omitidas, {"email duplicado (tras normalizar mayúsculas/espacios)": 1})
        # 6 movimientos: 1 huérfano, 1 tipo raro, 1 del usuario omitido -> quedan 3
        self.assertEqual(por_tabla["movimientos"].copiadas, 3)
        self.assertEqual(por_tabla["movimientos"].total_omitidas, 3)
        self.assertEqual(por_tabla["recurrentes"].en_origen, 0)       # tabla inexistente en el origen
        d = self.destino
        self.assertEqual([f["id"] for f in d.query("SELECT id FROM usuarios ORDER BY id")], [7, 9])
        self.assertEqual(d.scalar("SELECT email FROM usuarios WHERE id = 7"), "ana@example.com")
        self.assertEqual(d.scalar("SELECT password FROM usuarios WHERE id = 9"), "!")
        self.assertEqual(d.scalar("SELECT nombre FROM usuarios WHERE id = 9"), "bob")
        self.assertEqual(d.scalar("SELECT COUNT(*) FROM movimientos WHERE usuario_id = 7"), 2)
        self.assertEqual(d.scalar("SELECT importe FROM movimientos WHERE concepto = 'Cafe'"), 2.5)
        self.assertEqual(d.scalar("SELECT demo FROM movimientos WHERE concepto = 'Cafe'"), 0)   # columna nueva con su valor por defecto
        self.assertEqual(d.scalar("SELECT cantidad_actual FROM metas"), 0)
        self.assertIn("omitida", informe.texto())

    def test_los_nuevos_ids_continuan_despues_de_los_migrados(self):
        migrar(self.origen, self.destino)
        nuevo = self.destino.insert(
            "INSERT INTO usuarios (nombre, email, password) VALUES (?, ?, ?)", ("Nuevo", "nuevo@example.com", "h"))
        self.assertGreater(nuevo, 9)          # en PostgreSQL exige reajustar la secuencia

    def test_simulacro_no_escribe(self):
        informe = migrar(self.origen, self.destino, simulacro=True)
        self.assertTrue(informe.simulacro)
        self.assertEqual(self.destino.scalar("SELECT COUNT(*) FROM usuarios"), 0)
        self.assertEqual(self.destino.scalar("SELECT COUNT(*) FROM movimientos"), 0)

    def test_se_niega_a_mezclar_con_un_destino_con_datos(self):
        migrar(self.origen, self.destino)
        with self.assertRaises(MigracionError):
            migrar(self.origen, self.destino)
        self.assertEqual(self.destino.scalar("SELECT COUNT(*) FROM usuarios"), 2)

    def test_importe_no_numerico_se_omite_y_se_cuenta(self):
        con = sqlite3.connect(self.origen)
        con.execute("UPDATE movimientos SET importe = 'abc' WHERE concepto = 'Gym'")   # SQLite lo permite; se omite y se cuenta
        con.commit()
        con.close()
        informe = migrar(self.origen, self.destino)
        mov = {t.tabla: t for t in informe.tablas}["movimientos"]
        self.assertEqual(mov.omitidas.get("importe no numérico"), 1)

    def test_origen_que_no_es_findash(self):
        ruta = os.path.join(self.dir.name, "otra.db")
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE cosas (id INTEGER)")
        con.commit()
        con.close()
        with self.assertRaises(MigracionError):
            migrar(ruta, self.destino)
        with self.assertRaises(MigracionError):
            migrar(os.path.join(self.dir.name, "no_existe.db"), self.destino)


if __name__ == "__main__":
    unittest.main()
