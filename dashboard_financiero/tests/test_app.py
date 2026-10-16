"""Autenticación, CSRF, validación y seguridad entre usuarios."""
import datetime
import re
import unittest

from tests.base import AppTestCase


class TestAutenticacion(AppTestCase):
    def test_paginas_privadas_redirigen_al_login(self):
        for url in ("/panel", "/panel/transactions", "/panel/anadir", "/panel/analytics",
                    "/panel/settings", "/exportar_csv", "/panel/historico"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"], url)

    def test_api_privada_devuelve_401_json(self):
        r = self.client.post("/panel/importar_texto", json={"texto": "x"},
                             headers={"Accept": "application/json"})
        self.assertIn(r.status_code, (400, 401))        # sin CSRF: 400; con CSRF y sin sesión: 401
        t = self.token()
        r = self.client.post("/panel/importar_texto", json={"texto": "x"},
                             headers={"Accept": "application/json", "X-CSRFToken": t})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["ok"])

    def test_registro_y_login(self):
        r = self.registrar()
        self.assertEqual(r.status_code, 302)
        fila = self.sql("SELECT email, password FROM usuarios", one=True)
        self.assertEqual(fila["email"], "ana@example.com")
        self.assertNotIn("secreto123", fila["password"])           # nunca en claro
        r = self.entrar()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/panel"))
        self.assertEqual(self.client.get("/panel").status_code, 200)

    def test_registro_valida_datos(self):
        t = self.token(url="/registro")
        casos = [
            {"nombre": "Ana", "email": "no-es-email", "password": "secreto123", "password2": "secreto123"},
            {"nombre": "Ana", "email": "a@b.es", "password": "corta1", "password2": "corta1"},
            {"nombre": "Ana", "email": "a@b.es", "password": "solo-letras-largas", "password2": "solo-letras-largas"},
            {"nombre": "Ana", "email": "a@b.es", "password": "secreto123", "password2": "distinta123"},
            {"nombre": " ", "email": "a@b.es", "password": "secreto123", "password2": "secreto123"},
        ]
        for datos in casos:
            r = self.client.post("/registro", data={"csrf_token": t, **datos})
            self.assertEqual(r.status_code, 400, datos)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM usuarios", one=True)["n"], 0)

    def test_email_duplicado(self):
        self.registrar()
        r = self.registrar()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM usuarios", one=True)["n"], 1)

    def test_honeypot_no_crea_usuario(self):
        t = self.token(url="/registro")
        r = self.client.post("/registro", data={"csrf_token": t, "nombre": "Bot", "email": "bot@example.com",
                                                "password": "secreto123", "password2": "secreto123",
                                                "sitio_web": "http://spam"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM usuarios", one=True)["n"], 0)

    def test_login_incorrecto_y_bloqueo(self):
        self.registrar()
        for _ in range(5):
            t = self.token()
            r = self.client.post("/login", data={"csrf_token": t, "email": "ana@example.com", "password": "mala1234"})
            self.assertEqual(r.status_code, 401)
        t = self.token()
        r = self.client.post("/login", data={"csrf_token": t, "email": "ana@example.com", "password": "secreto123"})
        self.assertEqual(r.status_code, 429)                       # aunque sea la correcta, está bloqueada
        self.assertEqual(self.client.get("/panel").status_code, 302)

    def test_login_correcto_limpia_fallos(self):
        self.registrar()
        for _ in range(2):
            self.client.post("/login", data={"csrf_token": self.token(), "email": "ana@example.com", "password": "x"})
        self.assertEqual(self.entrar().status_code, 302)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM login_intentos WHERE clave LIKE ?", ("login:%",),
                                  one=True)["n"], 0)

    def test_usuario_inexistente_misma_respuesta(self):
        t = self.token()
        r = self.client.post("/login", data={"csrf_token": t, "email": "nadie@example.com", "password": "secreto123"})
        self.assertEqual(r.status_code, 401)

    def test_next_solo_rutas_internas(self):
        self.registrar()
        t = self.token()
        r = self.client.post("/login", data={"csrf_token": t, "email": "ana@example.com",
                                             "password": "secreto123", "next": "https://evil.example/x"})
        self.assertTrue(r.headers["Location"].endswith("/panel"))
        c = self.app.test_client()
        t = self.token(c)
        r = c.post("/login?next=//evil.example", data={"csrf_token": t, "email": "ana@example.com",
                                                        "password": "secreto123"})
        self.assertTrue(r.headers["Location"].endswith("/panel"))
        c = self.app.test_client()
        t = self.token(c)
        r = c.post("/login", data={"csrf_token": t, "email": "ana@example.com", "password": "secreto123",
                                   "next": "/panel/analytics"})
        self.assertTrue(r.headers["Location"].endswith("/panel/analytics"))

    def test_logout_solo_por_post(self):
        self.iniciar()
        r = self.client.get("/logout")                  # un enlace no cierra la sesión
        self.assertEqual(self.client.get("/panel").status_code, 200)
        r = self.post("/logout", pagina="/panel")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/panel").status_code, 302)

    def test_sesion_se_regenera_al_entrar(self):
        self.registrar()
        antes = self.token()
        self.entrar()
        with self.client.session_transaction() as s:
            self.assertNotEqual(s.get("csrf_token"), antes)


class TestCSRF(AppTestCase):
    def rutas_post(self):
        reglas = []
        for r in self.app.url_map.iter_rules():
            if "POST" in r.methods and r.endpoint != "static":
                url = re.sub(r"<int:[^>]+>", "1", r.rule)
                reglas.append((r.endpoint, url))
        return reglas

    def test_todas_las_rutas_post_exigen_token(self):
        """Recorre el mapa de URLs: ningún POST puede aceptar una petición sin token CSRF."""
        self.iniciar()
        rutas = self.rutas_post()
        self.assertGreater(len(rutas), 15)
        for endpoint, url in rutas:
            for datos in ({}, {"csrf_token": "falso"}):
                r = self.client.post(url, data=datos)
                self.assertIn(r.status_code, (302, 400), f"{endpoint} {url} aceptó POST sin CSRF: {r.status_code}")
                if r.status_code == 302:
                    self.assertNotIn("Operación", r.get_data(as_text=True))
            r = self.client.post(url, json={}, headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 400, f"{endpoint} {url} sin cabecera CSRF")
            self.assertTrue(r.get_json().get("csrf"))

    def test_cabecera_x_csrftoken_vale(self):
        self.iniciar()
        r = self.post_json("/panel/settings/preferencias", {"tema": "light", "privacidad": 0, "animaciones": 0})
        self.assertEqual(r.status_code, 200)

    def test_sin_csrf_no_se_modifica_nada(self):
        self.iniciar()
        self.anadir_mov("2026-01-10", "Café", 2.0)
        mid = self.sql("SELECT id FROM movimientos", one=True)["id"]
        self.client.post(f"/panel/borrar/{mid}", data={})
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)
        self.client.post("/panel/editar_movimiento", json={"id": mid, "concepto": "Hack"})
        self.assertEqual(self.sql("SELECT concepto FROM movimientos", one=True)["concepto"], "Café")


class TestCabeceras(AppTestCase):
    def test_cabeceras_de_seguridad(self):
        r = self.client.get("/login")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_paginas_privadas_no_se_cachean(self):
        self.iniciar()
        r = self.client.get("/panel")
        self.assertIn("no-store", r.headers["Cache-Control"])

    def test_cookie_de_sesion(self):
        self.registrar()
        r = self.entrar()
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_error_404_y_405(self):
        self.assertEqual(self.client.get("/no-existe").status_code, 404)
        self.assertEqual(self.client.get("/panel/importar").status_code, 405)

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.get_json()["status"], "ok")


class TestAislamientoEntreUsuarios(AppTestCase):
    def setUp(self):
        super().setUp()
        self.ana = self.cliente_con_sesion("ana@example.com", "Ana")
        self.bea = self.cliente_con_sesion("bea@example.com", "Bea")
        uid_ana = self.sql("SELECT id FROM usuarios WHERE email = 'ana@example.com'", one=True)["id"]
        self.anadir_mov("2026-01-10", "Secreto de Ana", 99.0, uid=uid_ana)
        self.mov_ana = self.sql("SELECT id FROM movimientos", one=True)["id"]

    def test_no_se_puede_borrar_el_movimiento_de_otro(self):
        self.post(f"/panel/borrar/{self.mov_ana}", client=self.bea)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)

    def test_no_se_puede_duplicar_el_de_otro(self):
        self.post(f"/panel/duplicar/{self.mov_ana}", client=self.bea)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)

    def test_no_se_puede_editar_el_de_otro(self):
        r = self.post_json("/panel/editar_movimiento", {
            "id": self.mov_ana, "fecha": "2026-01-10", "concepto": "Robado", "categoria": "Vivienda",
            "importe": 1, "tipo": "gasto"}, client=self.bea)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.sql("SELECT concepto FROM movimientos", one=True)["concepto"], "Secreto de Ana")

    def test_listados_solo_muestran_lo_propio(self):
        self.assertNotIn("Secreto de Ana", self.bea.get("/panel/transactions").get_data(as_text=True))
        self.assertNotIn("Secreto de Ana", self.bea.get("/exportar_csv", follow_redirects=True).get_data(as_text=True))
        self.assertIn("Secreto de Ana", self.ana.get("/panel/transactions").get_data(as_text=True))

    def test_no_se_pueden_tocar_metas_ni_recurrentes_ajenos(self):
        self.post("/panel/settings", {"accion": "nueva_meta", "meta_nombre": "Viaje", "meta_objetivo": "1000"},
                  client=self.ana, pagina="/panel/settings")
        meta = self.sql("SELECT id FROM metas", one=True)["id"]
        self.post("/panel/settings", {"accion": "borrar_meta", "meta_id": meta}, client=self.bea, pagina="/panel/settings")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM metas", one=True)["n"], 1)
        self.post("/panel/settings", {"accion": "crear_recurrente", "tipo": "gasto", "concepto": "Gym", "importe": "30",
                                      "dia_mes": "5", "categoria": ""}, client=self.ana, pagina="/panel/settings")
        rid = self.sql("SELECT id FROM recurrentes", one=True)["id"]
        for accion in ("borrar_recurrente", "alternar_recurrente"):
            self.post("/panel/settings", {"accion": accion, "recurrente_id": rid}, client=self.bea, pagina="/panel/settings")
        fila = self.sql("SELECT activo FROM recurrentes", one=True)
        self.assertEqual(int(fila["activo"]), 1)


if __name__ == "__main__":
    unittest.main()
