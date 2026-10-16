"""Endpoints de importación: vista previa, confirmación, deduplicación, idempotencia y límites."""
import base64
import datetime
import io
import unittest

from findash.ingest import MAX_BYTES
from tests.base import AppTestCase

EXTRACTO = (
    "COMPRA TARJ. MERCADONA VALENCIA 12/03/2026 -45,20 € 1.954,80 €\n"
    "NOMINA EMPRESA SL 13/03/2026 1.850,00 € 3.804,80 €\n"
    "RECIBO IBERDROLA 14/03/2026 -54,30 € 3.750,50 €\n")
CSV = ("Fecha;Concepto;Importe;Saldo\n01/03/2026;COMPRA ZARA;-50,00;950,00\n"
       "02/03/2026;NOMINA;1.500,00;2.450,00\n03/03/2026;RESTAURANTE LA PLAZA;-30,00;2.420,00\n")


class TestImportar(AppTestCase):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def n_mov(self):
        return int(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"])

    def texto(self, texto=EXTRACTO, **extra):
        return self.post_json("/panel/importar_texto", {"texto": texto, **extra})

    def archivo(self, nombre, contenido, **extra):
        datos = {"archivo": (io.BytesIO(contenido), nombre), **extra}
        return self.post("/panel/importar_csv", datos, headers={"Accept": "application/json"},
                         content_type="multipart/form-data")

    def test_vista_previa_no_escribe_nada(self):
        r = self.texto(dry_run=True)
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j["ok"] and j["dry_run"])
        self.assertEqual((j["detectados"], j["nuevos"], j["duplicados"], j["insertados"]), (3, 3, 0, 0))
        self.assertEqual(j["total_gastos"], 99.5)
        self.assertEqual(j["total_ingresos"], 1850.0)
        self.assertEqual(len(j["muestra"]), 3)
        self.assertEqual(j["muestra"][0]["categoria"], "Alimentación")
        self.assertEqual(self.n_mov(), 0)

    def test_importar_texto(self):
        j = self.texto(dry_run=False).get_json()
        self.assertEqual((j["insertados"], j["duplicados"]), (3, 0))
        filas = self.sql("SELECT fecha, concepto, categoria, importe, tipo FROM movimientos ORDER BY fecha")
        self.assertEqual([f["tipo"] for f in filas], ["gasto", "ingreso", "gasto"])
        self.assertEqual([f["categoria"] for f in filas], ["Alimentación", "Nómina y Salario", "Servicios del Hogar"])

    def test_reimportar_no_duplica(self):
        self.texto(dry_run=False)
        j = self.texto(dry_run=False).get_json()
        self.assertEqual((j["insertados"], j["duplicados"]), (0, 3))
        self.assertEqual(self.n_mov(), 3)

    def test_importacion_parcial_solo_anade_lo_nuevo(self):
        self.texto(dry_run=False)
        nuevo = EXTRACTO + "COMPRA ZARA 15/03/2026 -30,00 € 3.720,50 €\n"
        j = self.texto(nuevo, dry_run=False).get_json()
        self.assertEqual((j["insertados"], j["duplicados"]), (1, 3))

    def test_dos_compras_identicas_el_mismo_dia_se_conservan_las_dos(self):
        texto = ("CAFETERIA LUNA 12/03/2026 -2,50 € 100,00 €\nCAFETERIA LUNA 12/03/2026 -2,50 € 97,50 €\n"
                 "NOMINA EMPRESA 13/03/2026 1.000,00 € 1.097,50 €\n")
        self.assertEqual(self.texto(texto, dry_run=False).get_json()["insertados"], 3)
        j = self.texto(texto, dry_run=False).get_json()                 # reimportar: ninguna nueva
        self.assertEqual((j["insertados"], j["duplicados"]), (0, 3))
        self.assertEqual(self.n_mov(), 3)

    def test_importar_csv_multipart(self):
        r = self.archivo("extracto.csv", CSV.encode("utf-8"), dry_run="0")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["insertados"], 3)
        self.assertEqual(self.n_mov(), 3)

    def test_importar_json_base64(self):
        b64 = base64.b64encode(CSV.encode()).decode()
        r = self.post_json("/panel/importar", {"nombre": "extracto.csv", "contenido": b64, "dry_run": True})
        self.assertEqual(r.get_json()["detectados"], 3)
        r = self.post_json("/panel/importar", {"nombre": "extracto.csv", "contenido": b64})
        self.assertEqual(r.get_json()["insertados"], 3)
        r = self.post_json("/panel/importar", {"texto": EXTRACTO})
        self.assertEqual(r.get_json()["insertados"], 3)

    def test_base64_invalido_y_json_sin_contenido(self):
        self.assertEqual(self.post_json("/panel/importar", {"nombre": "x.csv", "contenido": "###no-b64###"}).status_code, 400)
        self.assertEqual(self.post_json("/panel/importar", {"nombre": "x.csv"}).status_code, 400)

    def test_idempotencia_con_clave(self):
        clave = "abcdef123456-uuid"
        j1 = self.texto(dry_run=False, clave=clave).get_json()
        j2 = self.texto(dry_run=False, clave=clave).get_json()
        self.assertFalse(j1["repetido"])
        self.assertTrue(j2["repetido"])
        self.assertEqual(j2["insertados"], j1["insertados"])             # devuelve el resultado anterior
        self.assertEqual(self.n_mov(), 3)
        # tras borrar y con una clave nueva, vuelve a importar
        self.exec_sql("DELETE FROM movimientos")
        j3 = self.texto(dry_run=False, clave="otra-clave-distinta").get_json()
        self.assertEqual(j3["insertados"], 3)

    def test_la_clave_caduca(self):
        self.texto(dry_run=False, clave="clave-que-caduca-1")
        with self.client.session_transaction() as s:
            for v in s["imp_claves"].values():
                v["ts"] -= 601
        self.exec_sql("DELETE FROM movimientos")
        j = self.texto(dry_run=False, clave="clave-que-caduca-1").get_json()
        self.assertFalse(j["repetido"])
        self.assertEqual(j["insertados"], 3)

    def test_clave_invalida_se_ignora(self):
        self.texto(dry_run=False, clave="x y z <script>")
        with self.client.session_transaction() as s:
            self.assertFalse(s.get("imp_claves"))

    def test_la_vista_previa_no_consume_la_clave(self):
        self.texto(dry_run=True, clave="clave-de-preview-1")
        j = self.texto(dry_run=False, clave="clave-de-preview-1").get_json()
        self.assertFalse(j["repetido"])
        self.assertEqual(j["insertados"], 3)

    def test_errores_de_usuario(self):
        r = self.texto("esto no es un extracto")
        self.assertEqual(r.status_code, 422)
        self.assertIn("No he podido reconocer", r.get_json()["error"])
        self.assertEqual(self.texto("   ").status_code, 400)
        r = self.archivo("vacio.csv", b"")
        self.assertEqual(r.status_code, 400)
        r = self.archivo("raro.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 50)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.n_mov(), 0)

    def test_tamano_maximo(self):
        r = self.archivo("grande.csv", b"a" * (MAX_BYTES + 10))
        self.assertEqual(r.status_code, 413)
        r = self.post_json("/panel/importar_texto", {"texto": "a" * (MAX_BYTES + 10)})
        self.assertIn(r.status_code, (413,))

    def test_importacion_atomica(self):
        """Si falla a mitad no queda nada a medias."""
        from findash.ingest import importacion
        original = importacion.procesar_importacion
        db = self.conexion()
        try:
            from findash.ingest.modelos import Movimiento
            movs = [Movimiento(datetime.date(2026, 3, 1), "OK", 10.0, "gasto", categoria="Gastos Generales"),
                    Movimiento(datetime.date(2026, 3, 2), "MAL", 10.0, "inventado", categoria="Gastos Generales")]
            uid = self.uid
            with self.assertRaises(Exception):
                importacion.procesar_importacion(db, uid, movs)           # viola el CHECK(tipo)
        finally:
            db.close()
        self.assertEqual(self.n_mov(), 0)

    def test_no_se_mezclan_usuarios(self):
        otra = self.cliente_con_sesion("bea@example.com", "Bea")
        self.texto(dry_run=False)
        r = self.post_json("/panel/importar_texto", {"texto": EXTRACTO, "dry_run": False}, client=otra)
        self.assertEqual(r.get_json()["insertados"], 3)                  # lo de Ana no cuenta como duplicado para Bea
        self.assertEqual(self.n_mov(), 6)

    def test_categorias_de_exportacion_propia_se_respetan(self):
        csv = ("Fecha;Tipo;Concepto;Categoria;Importe (EUR)\n"
               "01/03/2026;GASTO;COMPRA CUALQUIERA;Ocio y Tiempo Libre;25,00\n"
               "02/03/2026;INGRESO;PAGA;Otros Ingresos;100,00\n"
               "03/03/2026;GASTO;OTRA COSA;Vivienda;50,00\n")
        j = self.archivo("findash.csv", csv.encode("utf-8-sig"), dry_run="0").get_json()
        self.assertEqual(j["insertados"], 3, j)
        filas = self.sql("SELECT categoria, tipo FROM movimientos ORDER BY fecha")
        self.assertEqual([(f["tipo"], f["categoria"]) for f in filas],
                         [("gasto", "Ocio y Tiempo Libre"), ("ingreso", "Otros Ingresos"), ("gasto", "Vivienda")])

    def test_ida_y_vuelta_exportar_importar(self):
        for i in range(5):
            self.anadir_mov(f"2026-02-{i + 1:02d}", f"COMPRA Nº {i}", 10 + i)
        self.anadir_mov("2026-02-10", "NOMINA", 1500, "ingreso", "Nómina y Salario")
        csv = self.client.get("/exportar_csv").data
        self.exec_sql("DELETE FROM movimientos")
        j = self.archivo("findash.csv", csv, dry_run="0").get_json()
        self.assertEqual(j["insertados"], 6, j)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos WHERE tipo = 'ingreso'", one=True)["n"], 1)

    def test_la_pagina_de_historial_incluye_el_panel_de_importacion(self):
        html = self.client.get("/panel/transactions").get_data(as_text=True)
        self.assertIn('id="importar"', html)
        self.assertIn("importar.js", html)

    def test_importar_por_formulario_sin_js_redirige(self):
        r = self.post("/panel/importar_texto", {"texto": EXTRACTO, "dry_run": ""}, pagina="/panel/transactions")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.n_mov(), 3)

    def test_limite_de_peticiones(self):
        self.app.config["TESTING"] = True
        codigos = [self.texto("sin sentido", dry_run=True).status_code for _ in range(32)]
        self.assertIn(429, codigos)


if __name__ == "__main__":
    unittest.main()
