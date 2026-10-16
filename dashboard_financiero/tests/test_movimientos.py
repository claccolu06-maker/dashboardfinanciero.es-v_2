"""Alta, edición con aprendizaje de categorías, borrado, duplicado, exportación y preferencias."""
import csv
import datetime
import io
import unittest

from tests.base import AppTestCase

HOY = datetime.date.today().isoformat()


class TestAlta(AppTestCase):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def alta(self, **datos):
        base = {"tipo": "gasto", "fecha": HOY, "concepto": "Compra", "importe": "12.50", "categoria": "Alimentación"}
        base.update(datos)
        return self.post("/panel/anadir", base, pagina="/panel/anadir")

    def test_alta_correcta(self):
        r = self.alta()
        self.assertEqual(r.status_code, 302)
        fila = self.sql("SELECT * FROM movimientos", one=True)
        self.assertEqual((fila["concepto"], float(fila["importe"]), fila["tipo"], fila["categoria"], fila["fecha"]),
                         ("Compra", 12.5, "gasto", "Alimentación", HOY))

    def test_importe_con_coma_y_miles(self):
        self.alta(importe="1.234,56")
        self.assertEqual(float(self.sql("SELECT importe FROM movimientos", one=True)["importe"]), 1234.56)

    def test_categoria_automatica(self):
        self.alta(concepto="NETFLIX.COM", categoria="")
        self.assertEqual(self.sql("SELECT categoria FROM movimientos", one=True)["categoria"], "Suscripciones Digitales")
        self.alta(concepto="NÓMINA EMPRESA", categoria="", tipo="ingreso")
        cats = [f["categoria"] for f in self.sql("SELECT categoria FROM movimientos ORDER BY id")]
        self.assertEqual(cats[1], "Nómina y Salario")

    def test_validaciones(self):
        manana = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        malos = [
            {"importe": "0"}, {"importe": "-5"}, {"importe": "abc"}, {"importe": "nan"}, {"importe": "inf"},
            {"importe": "1e999"}, {"importe": "99999999999"}, {"importe": ""},
            {"tipo": "otro"}, {"tipo": ""}, {"fecha": manana}, {"fecha": "2026-13-45"}, {"fecha": ""},
            {"fecha": "1980-01-01"}, {"concepto": ""}, {"concepto": "   "},
        ]
        for datos in malos:
            r = self.alta(**datos)
            self.assertEqual(r.status_code, 400, datos)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 0)

    def test_concepto_se_sanea_y_trunca(self):
        self.alta(concepto="  Hola\x00\n  mundo  " + "x" * 400)
        c = self.sql("SELECT concepto FROM movimientos", one=True)["concepto"]
        self.assertTrue(c.startswith("Hola mundo"))
        self.assertLessEqual(len(c), 200)
        self.assertNotIn("\x00", c)

    def test_xss_se_escapa_en_listados(self):
        self.alta(concepto='<script>alert(1)</script>', categoria='<img src=x onerror=alert(2)>')
        for url in ("/panel/transactions", "/panel", "/panel/analytics", "/panel/anadir", "/panel/settings"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertNotIn("<script>alert(1)</script>", html, url)
            self.assertNotIn("<img src=x onerror", html, url)

    def test_sugerencia_de_categoria(self):
        r = self.client.get("/panel/sugerir_categoria?tipo=gasto&concepto=MERCADONA%20VALENCIA",
                            headers={"Accept": "application/json"})
        self.assertEqual(r.get_json()["categoria"], "Alimentación")


class TestEdicionYAprendizaje(AppTestCase):
    def setUp(self):
        super().setUp()
        self.iniciar()
        self.anadir_mov("2026-01-05", "COMPRA TARJ. MERCADONA VALENCIA", 40, categoria="Gastos Generales")
        self.anadir_mov("2026-01-12", "MERCADONA ALBORAYA", 25, categoria="Gastos Generales")
        self.anadir_mov("2026-01-15", "BAR PEPE", 8, categoria="Gastos Generales")
        self.ids = [f["id"] for f in self.sql("SELECT id FROM movimientos ORDER BY id")]

    def editar(self, mid, **cambios):
        datos = {"id": mid, "fecha": "2026-01-05", "concepto": "COMPRA TARJ. MERCADONA VALENCIA", "importe": 40,
                 "tipo": "gasto", "categoria": "Gastos Generales"}
        datos.update(cambios)
        return self.post_json("/panel/editar_movimiento", datos)

    def test_edicion_basica(self):
        r = self.editar(self.ids[2], concepto="BAR PEPE (cena)", importe="9,50", fecha="2026-01-16")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        f = self.sql("SELECT * FROM movimientos WHERE id = ?", (self.ids[2],), one=True)
        self.assertEqual((f["concepto"], float(f["importe"]), f["fecha"]), ("BAR PEPE (cena)", 9.5, "2026-01-16"))

    def test_cambiar_categoria_aprende_la_regla(self):
        r = self.editar(self.ids[0], categoria="Alimentación")
        j = r.get_json()
        self.assertEqual(j["regla"], {"clave": "mercadona", "categoria": "Alimentación"})
        self.assertEqual(j["recategorizados"], 0)
        regla = self.sql("SELECT patron, categoria, tipo FROM reglas_categoria", one=True)
        self.assertEqual((regla["patron"], regla["categoria"], regla["tipo"]), ("mercadona", "Alimentación", "gasto"))
        # el otro Mercadona NO se toca si no se pidió
        self.assertEqual(self.sql("SELECT categoria FROM movimientos WHERE id = ?", (self.ids[1],), one=True)["categoria"],
                         "Gastos Generales")

    def test_aplicar_a_similares(self):
        r = self.editar(self.ids[0], categoria="Alimentación", aplicar_similares=True)
        self.assertEqual(r.get_json()["recategorizados"], 1)
        cats = {f["id"]: f["categoria"] for f in self.sql("SELECT id, categoria FROM movimientos")}
        self.assertEqual(cats[self.ids[0]], "Alimentación")
        self.assertEqual(cats[self.ids[1]], "Alimentación")
        self.assertEqual(cats[self.ids[2]], "Gastos Generales")           # otro comercio: intacto

    def test_regla_aprendida_se_usa_en_altas_futuras(self):
        self.editar(self.ids[0], categoria="Salud y Bienestar")           # el usuario discrepa de la heurística
        self.post("/panel/anadir", {"tipo": "gasto", "fecha": HOY, "concepto": "MERCADONA CENTRO", "importe": "10",
                                    "categoria": ""}, pagina="/panel/anadir")
        ultima = self.sql("SELECT categoria FROM movimientos ORDER BY id DESC LIMIT 1", one=True)["categoria"]
        self.assertEqual(ultima, "Salud y Bienestar")

    def test_regla_aprendida_se_usa_al_importar(self):
        self.editar(self.ids[0], categoria="Salud y Bienestar")
        texto = "20/01/2026 MERCADONA SAN JOSE -33,10 500,00\n21/01/2026 CAFETERIA LUNA -2,50 497,50\n"
        r = self.post_json("/panel/importar_texto", {"texto": texto, "dry_run": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        f = self.sql("SELECT categoria FROM movimientos WHERE concepto LIKE ?", ("MERCADONA SAN%",), one=True)
        self.assertEqual(f["categoria"], "Salud y Bienestar")

    def test_sin_cambio_de_categoria_no_aprende(self):
        self.editar(self.ids[0], importe=41)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM reglas_categoria", one=True)["n"], 0)

    def test_la_regla_se_actualiza_no_se_duplica(self):
        self.editar(self.ids[0], categoria="Alimentación")
        self.editar(self.ids[0], categoria="Compras y Ropa")
        reglas = self.sql("SELECT categoria FROM reglas_categoria")
        self.assertEqual([r["categoria"] for r in reglas], ["Compras y Ropa"])

    def test_validaciones_en_edicion(self):
        for malo in ({"importe": "0"}, {"importe": "nan"}, {"tipo": "x"}, {"fecha": "ayer"}, {"concepto": ""}):
            r = self.editar(self.ids[0], **malo)
            self.assertEqual(r.status_code, 400, malo)
            self.assertFalse(r.get_json()["ok"])
        self.assertEqual(float(self.sql("SELECT importe FROM movimientos WHERE id = ?", (self.ids[0],), one=True)["importe"]), 40.0)

    def test_id_inexistente_o_invalido(self):
        self.assertEqual(self.editar(99999).status_code, 404)
        self.assertEqual(self.editar("abc").status_code, 400)

    def test_edicion_por_formulario_sin_js(self):
        r = self.post("/panel/editar_movimiento", {"id": self.ids[2], "fecha": "2026-01-15", "concepto": "BAR PEPE",
                                                   "importe": "8", "tipo": "gasto", "categoria": "Restaurantes y Comida"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sql("SELECT categoria FROM movimientos WHERE id = ?", (self.ids[2],), one=True)["categoria"],
                         "Restaurantes y Comida")


class TestBorrarDuplicarListar(AppTestCase):
    def setUp(self):
        super().setUp()
        self.iniciar()
        for i in range(60):
            self.anadir_mov("2026-01-%02d" % (i % 28 + 1), f"Mov {i}", 1 + i)

    def test_paginacion_acotada(self):
        self.assertEqual(self.client.get("/panel/transactions").status_code, 200)
        for pag in ("0", "-3", "999", "abc", "2", "3"):
            self.assertEqual(self.client.get(f"/panel/transactions?page={pag}").status_code, 200, pag)
        html = self.client.get("/panel/transactions?page=999").get_data(as_text=True)
        self.assertIn("Mov ", html)                                  # recortada a la última página, no vacía
        self.assertEqual(html.count("js-editar"), html.count('data-id="') )

    def test_filtros(self):
        self.anadir_mov("2026-02-01", "Nómina", 1000, "ingreso", "Nómina y Salario")
        html = self.client.get("/panel/transactions?tipo=ingreso").get_data(as_text=True)
        self.assertIn("Nómina", html)
        self.assertNotIn("Mov 1<", html)
        html = self.client.get("/panel/transactions?tipo=xx&categoria=%27%3B--").get_data(as_text=True)
        self.assertIn("Sin resultados", html)

    def test_borrar(self):
        mid = self.sql("SELECT id FROM movimientos ORDER BY id LIMIT 1", one=True)["id"]
        r = self.post(f"/panel/borrar/{mid}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 59)
        r = self.post(f"/panel/borrar/{mid}")                        # ya no existe: sin error
        self.assertEqual(r.status_code, 302)

    def test_borrar_no_redirige_fuera_del_sitio(self):
        mid = self.sql("SELECT id FROM movimientos ORDER BY id LIMIT 1", one=True)["id"]
        r = self.post(f"/panel/borrar/{mid}", headers={"Referer": "https://evil.example/phish"})
        self.assertNotIn("evil.example", r.headers["Location"])

    def test_duplicar(self):
        mid = self.sql("SELECT id FROM movimientos ORDER BY id LIMIT 1", one=True)["id"]
        self.post(f"/panel/duplicar/{mid}")
        copia = self.sql("SELECT concepto, fecha FROM movimientos ORDER BY id DESC LIMIT 1", one=True)
        self.assertTrue(copia["concepto"].endswith("(Copia)"))
        self.assertEqual(copia["fecha"], HOY)

    def test_exportar_csv_y_proteccion_de_formulas(self):
        self.exec_sql("DELETE FROM movimientos")
        self.anadir_mov("2026-01-01", "=HYPERLINK(\"http://evil\")", 10)
        self.anadir_mov("2026-01-02", "+34 600 000 000", 5)
        self.anadir_mov("2026-01-03", "Normal; con punto y coma", 1234.5)
        r = self.client.get("/exportar_csv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/csv")
        self.assertTrue(r.data.startswith(b"\xef\xbb\xbf"))
        filas = list(csv.reader(io.StringIO(r.data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(filas[0], ["Fecha", "Tipo", "Concepto", "Categoria", "Importe (EUR)"])
        conceptos = {f[2] for f in filas[1:]}
        self.assertIn("'=HYPERLINK(\"http://evil\")", conceptos)
        self.assertIn("'+34 600 000 000", conceptos)
        self.assertIn("Normal; con punto y coma", conceptos)
        self.assertIn("1234,50", [f[4] for f in filas[1:]])

    def test_exportar_vacio_no_falla(self):
        self.exec_sql("DELETE FROM movimientos")
        r = self.client.get("/exportar_csv")
        self.assertEqual(r.status_code, 302)

    def test_rutas_antiguas_redirigen(self):
        self.assertIn("/panel/anadir", self.client.get("/anadir").headers["Location"])
        self.assertIn("/panel/transactions", self.client.get("/transactions").headers["Location"])


class TestPreferenciasYTema(AppTestCase):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def test_las_preferencias_se_guardan_en_base_de_datos(self):
        r = self.post_json("/panel/settings/preferencias", {"tema": "light", "privacidad": True, "animaciones": True},
                           pagina="/panel/settings")
        self.assertEqual(r.status_code, 200)
        f = self.sql("SELECT tema, privacidad, animaciones FROM usuarios", one=True)
        self.assertEqual((f["tema"], int(f["privacidad"]), int(f["animaciones"])), ("light", 1, 1))

    def test_tema_persiste_en_un_dispositivo_nuevo(self):
        self.post_json("/panel/settings/preferencias", {"tema": "light", "privacidad": False, "animaciones": False},
                       pagina="/panel/settings")
        otro = self.app.test_client()
        self.entrar(otro)
        for url in ("/panel", "/panel/transactions", "/panel/anadir", "/panel/analytics", "/panel/settings",
                    "/panel/historico"):
            html = otro.get(url).get_data(as_text=True)
            self.assertIn('data-theme="light"', html, url)
            self.assertIn('data-theme-pref="light"', html, url)

    def test_script_de_tema_va_antes_de_la_hoja_de_estilos(self):
        html = self.client.get("/panel").get_data(as_text=True)
        self.assertLess(html.index("data-theme-pref"), html.index("style.css"))
        self.assertLess(html.index("setAttribute('data-theme'"), html.index("style.css"))

    def test_modo_system_no_rompe_el_atributo(self):
        self.post_json("/panel/settings/preferencias", {"tema": "system"}, pagina="/panel/settings")
        html = self.client.get("/panel").get_data(as_text=True)
        self.assertIn('data-theme-pref="system"', html)
        self.assertIn('data-theme="dark"', html)                   # valor por defecto sin JS; el script lo resuelve

    def test_privacidad_y_animaciones_en_el_html(self):
        self.post_json("/panel/settings/preferencias", {"tema": "dark", "privacidad": True, "animaciones": True},
                       pagina="/panel/settings")
        html = self.client.get("/panel/settings").get_data(as_text=True)
        self.assertIn('data-privacy="1"', html)
        self.assertIn('data-reduce-motion="1"', html)

    def test_tema_invalido_se_normaliza(self):
        self.post_json("/panel/settings/preferencias", {"tema": "<script>"}, pagina="/panel/settings")
        self.assertEqual(self.sql("SELECT tema FROM usuarios", one=True)["tema"], "dark")

    def test_login_y_registro_conservan_el_tema_del_navegador(self):
        c = self.app.test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertIn("localStorage.getItem('findash_theme')", html)

    def test_perfil(self):
        self.post("/panel/settings", {"accion": "guardar_perfil", "nombrevisible": "Ana María"}, pagina="/panel/settings")
        self.assertEqual(self.sql("SELECT nombre FROM usuarios", one=True)["nombre"], "Ana María")
        self.post("/panel/settings", {"accion": "guardar_perfil", "nombrevisible": "A"}, pagina="/panel/settings")
        self.assertEqual(self.sql("SELECT nombre FROM usuarios", one=True)["nombre"], "Ana María")

    def test_metas(self):
        self.post("/panel/settings", {"accion": "nueva_meta", "meta_nombre": "Coche", "meta_objetivo": "8.000,50"},
                  pagina="/panel/settings")
        self.assertEqual(float(self.sql("SELECT cantidad_objetivo FROM metas", one=True)["cantidad_objetivo"]), 8000.5)
        for malo in ("0", "-1", "nan", "abc", ""):
            self.post("/panel/settings", {"accion": "nueva_meta", "meta_nombre": "X", "meta_objetivo": malo}, pagina="/panel/settings")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM metas", one=True)["n"], 1)

    def test_accion_desconocida(self):
        r = self.post("/panel/settings", {"accion": "formatear_disco"}, pagina="/panel/settings")
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main()
