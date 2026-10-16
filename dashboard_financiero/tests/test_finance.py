"""Lógica financiera: periodos, colchón, previsión, Wrapped, recurrentes, analítica, metas y presupuestos."""
import datetime
import unittest

from findash.services import finance, demo
from tests.base import AppTestCase

HOY = datetime.date(2026, 3, 15)


class FinanzasBase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.iniciar()
        self.db = self.conexion()

    def tearDown(self):
        self.db.close()
        super().tearDown()


class TestPeriodos(unittest.TestCase):
    def test_periodos_estandar(self):
        s = {}
        p = finance.resolver_periodo({}, s, HOY)
        self.assertEqual((p["inicio"], p["fin"], p["periodo"]), ("2026-03-01", "2026-03-15", "mes"))
        p = finance.resolver_periodo({"periodo": "trimestre"}, s, HOY)
        self.assertEqual(p["inicio"], "2025-12-15")
        p = finance.resolver_periodo({"periodo": "anio"}, s, HOY)
        self.assertEqual(p["inicio"], "2026-01-01")
        self.assertEqual(s["periodo"], "anio")                      # se recuerda en sesión

    def test_periodo_personalizado(self):
        s = {}
        p = finance.resolver_periodo({"periodo": "custom", "desde": "2026-01-10", "hasta": "2026-02-20"}, s, HOY)
        self.assertEqual((p["inicio"], p["fin"], p["dias"], p["error"]), ("2026-01-10", "2026-02-20", 42, None))
        # persistido: la siguiente petición sin parámetros lo conserva
        p = finance.resolver_periodo({}, s, HOY)
        self.assertEqual((p["periodo"], p["inicio"]), ("custom", "2026-01-10"))

    def test_periodo_personalizado_invalido_vuelve_a_mes(self):
        for args in ({"periodo": "custom", "desde": "basura", "hasta": "2026-02-01"},
                     {"periodo": "custom", "desde": "2026-03-01", "hasta": "2026-01-01"},
                     {"periodo": "custom", "desde": "1900-01-01", "hasta": "2026-03-01"},
                     {"periodo": "custom"}):
            p = finance.resolver_periodo(args, {}, HOY)
            self.assertEqual(p["periodo"], "mes", args)
            self.assertTrue(p["error"], args)

    def test_hasta_futuro_se_recorta_a_hoy(self):
        p = finance.resolver_periodo({"periodo": "custom", "desde": "2026-03-01", "hasta": "2030-01-01"}, {}, HOY)
        self.assertEqual(p["fin"], "2026-03-15")

    def test_periodo_desconocido(self):
        self.assertEqual(finance.resolver_periodo({"periodo": "xx"}, {}, HOY)["periodo"], "mes")


class TestRunway(FinanzasBase):
    def test_runway_usa_saldo_acumulado_y_no_el_ahorro_del_periodo(self):
        uid = self.uid
        # Ene-Feb: gastan 1.000/mes. Saldo acumulado = 6000 - 2000 = 4000 (+ marzo)
        self.anadir_mov("2026-01-01", "Nómina", 3000, "ingreso", "Nómina y Salario")
        self.anadir_mov("2026-02-01", "Nómina", 3000, "ingreso", "Nómina y Salario")
        self.anadir_mov("2026-01-10", "Gasto", 1000)
        self.anadir_mov("2026-02-10", "Gasto", 1000)
        # En el mes en curso solo hay gasto (ahorro del periodo negativo): el colchón NO debe ser 0
        self.anadir_mov("2026-03-05", "Gasto", 500)
        r = finance.calcular_runway(self.db, uid, HOY)
        self.assertEqual(r["saldo"], 3500.0)
        self.assertEqual(r["media_gasto"], 1000.0)
        self.assertEqual(r["meses"], 3.5)

    def test_runway_con_saldo_negativo_es_cero(self):
        self.anadir_mov("2026-02-10", "Gasto", 800)
        r = finance.calcular_runway(self.db, self.uid, HOY)
        self.assertEqual(r["meses"], 0.0)

    def test_runway_sin_historial_es_none(self):
        self.assertIsNone(finance.calcular_runway(self.db, self.uid, HOY)["meses"])

    def test_runway_estima_con_el_mes_en_curso_si_no_hay_cerrados(self):
        self.anadir_mov("2026-03-01", "Nómina", 2000, "ingreso", "Nómina y Salario")
        self.anadir_mov("2026-03-10", "Gasto", 300)
        r = finance.calcular_runway(self.db, self.uid, HOY)         # 300 en 15 días -> 620 a fin de mes
        self.assertEqual(r["saldo"], 1700.0)
        self.assertAlmostEqual(r["media_gasto"], 620.0, places=1)
        self.assertEqual(r["meses"], round(1700 / 620.0, 1))

    def test_runway_solo_usa_3_meses_cerrados(self):
        for mes, gasto in (("2025-11", 9000), ("2025-12", 100), ("2026-01", 200), ("2026-02", 300)):
            self.anadir_mov(f"{mes}-10", "Gasto", gasto)
        self.anadir_mov("2025-10-01", "Nómina", 50000, "ingreso", "Nómina y Salario")
        r = finance.calcular_runway(self.db, self.uid, HOY)
        self.assertEqual(r["media_gasto"], 200.0)                    # (100+200+300)/3, sin los 9000 de noviembre


class TestProyeccion(FinanzasBase):
    def test_proyeccion_basica(self):
        self.anadir_mov("2026-03-01", "Nómina", 2000, "ingreso", "Nómina y Salario")
        self.anadir_mov("2026-03-05", "Gasto", 150)                  # 150 en 15 días = 10/día
        p = finance.proyeccion_fin_de_mes(self.db, self.uid, HOY)
        self.assertEqual(p["saldo_actual"], 1850.0)
        self.assertEqual(p["dias_restantes"], 16)
        self.assertEqual(p["gasto_diario"], 10.0)
        self.assertEqual(p["saldo_proyectado"], round(1850 - 10 * 16, 2))

    def test_proyeccion_incluye_recurrentes_pendientes(self):
        self.anadir_mov("2026-03-02", "Gasto", 150)
        self.exec_sql("INSERT INTO recurrentes (usuario_id, concepto, categoria, importe, tipo, dia_mes, activo) "
                      "VALUES (?, 'Alquiler', 'Vivienda', 600, 'gasto', 28, 1)", (self.uid,))
        self.exec_sql("INSERT INTO recurrentes (usuario_id, concepto, categoria, importe, tipo, dia_mes, activo) "
                      "VALUES (?, 'Nómina', 'Nómina y Salario', 1500, 'ingreso', 30, 1)", (self.uid,))
        self.exec_sql("INSERT INTO recurrentes (usuario_id, concepto, categoria, importe, tipo, dia_mes, activo) "
                      "VALUES (?, 'Pausado', 'Vivienda', 999, 'gasto', 28, 0)", (self.uid,))
        p = finance.proyeccion_fin_de_mes(self.db, self.uid, HOY)
        self.assertEqual(p["fijos"], {"gastos": 600.0, "ingresos": 1500.0})
        self.assertEqual(p["saldo_proyectado"], round(-150 - 10 * 16 - 600 + 1500, 2))

    def test_curva_de_proyeccion(self):
        serie = {"labels": ["2026-03-01", "2026-03-05"], "data": [100.0, 80.0]}
        proy = {"fecha_fin": "2026-03-31", "saldo_proyectado": 20.0}
        c = finance.curva_proyeccion(serie, proy)
        self.assertEqual(c["labels"], ["2026-03-01", "2026-03-05", "2026-03-31"])
        self.assertEqual(c["data"], [None, 80.0, 20.0])


class TestRecurrentes(FinanzasBase):
    def crear(self, concepto="Netflix", importe=12.99, dia=10, tipo="gasto", activo=1):
        self.exec_sql("INSERT INTO recurrentes (usuario_id, concepto, categoria, importe, tipo, dia_mes, activo) "
                      "VALUES (?, ?, 'Suscripciones Digitales', ?, ?, ?, ?)",
                      (self.uid, concepto, importe, tipo, dia, activo))

    def test_registra_los_que_ya_tocan_una_sola_vez(self):
        self.crear(dia=10)
        self.crear("Futuro", 5, 20)
        self.assertEqual(finance.procesar_recurrentes(self.db, self.uid, HOY), 1)
        self.assertEqual(finance.procesar_recurrentes(self.db, self.uid, HOY), 0)     # idempotente
        movs = self.sql("SELECT fecha, concepto, importe FROM movimientos")
        self.assertEqual(len(movs), 1)
        self.assertEqual(movs[0]["fecha"], "2026-03-10")
        self.assertTrue(movs[0]["concepto"].startswith("🔄 "))
        self.assertEqual(float(movs[0]["importe"]), 12.99)

    def test_al_mes_siguiente_vuelve_a_registrar(self):
        self.crear(dia=10)
        finance.procesar_recurrentes(self.db, self.uid, HOY)
        finance.procesar_recurrentes(self.db, self.uid, datetime.date(2026, 4, 12))
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 2)

    def test_dia_31_en_mes_corto(self):
        self.crear("Fin de mes", 100, 31)
        self.assertEqual(finance.procesar_recurrentes(self.db, self.uid, datetime.date(2026, 2, 27)), 0)
        self.assertEqual(finance.procesar_recurrentes(self.db, self.uid, datetime.date(2026, 2, 28)), 1)
        self.assertEqual(self.sql("SELECT fecha FROM movimientos", one=True)["fecha"], "2026-02-28")

    def test_pausados_no_se_registran(self):
        self.crear(activo=0)
        self.assertEqual(finance.procesar_recurrentes(self.db, self.uid, HOY), 0)

    def test_reclamo_atomico_dos_conexiones(self):
        """Dos peticiones simultáneas no pueden duplicar el movimiento."""
        self.crear(dia=1)
        otra = self.conexion()
        try:
            a = finance.procesar_recurrentes(self.db, self.uid, HOY)
            b = finance.procesar_recurrentes(otra, self.uid, HOY)
        finally:
            otra.close()
        self.assertEqual((a, b), (1, 0))
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)

    def test_el_dashboard_los_registra_solo(self):
        self.crear(dia=1)
        self.client.get("/panel")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)
        self.client.get("/panel")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)

    def test_crud_desde_la_interfaz(self):
        pag = "/panel/settings"
        hoy = datetime.date.today()
        r = self.post("/panel/settings", {
            "accion": "crear_recurrente", "tipo": "gasto", "concepto": "Spotify", "importe": "10,99",
            "dia_mes": "1", "categoria": "", "aplicar_este_mes": "1"}, pagina=pag)
        self.assertEqual(r.status_code, 302)
        fila = self.sql("SELECT * FROM recurrentes", one=True)
        self.assertEqual(fila["categoria"], "Suscripciones Digitales")            # auto-categorizado
        self.assertEqual(float(fila["importe"]), 10.99)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)   # «aplicar este mes»
        rid = fila["id"]
        self.post("/panel/settings", {"accion": "editar_recurrente", "recurrente_id": rid, "tipo": "gasto",
                                      "concepto": "Spotify Duo", "importe": "14.99", "dia_mes": "5",
                                      "categoria": "Ocio y Tiempo Libre"}, pagina=pag)
        fila = self.sql("SELECT * FROM recurrentes", one=True)
        self.assertEqual((fila["concepto"], float(fila["importe"]), fila["dia_mes"], fila["categoria"]),
                         ("Spotify Duo", 14.99, 5, "Ocio y Tiempo Libre"))
        self.post("/panel/settings", {"accion": "alternar_recurrente", "recurrente_id": rid}, pagina=pag)
        self.assertEqual(int(self.sql("SELECT activo FROM recurrentes", one=True)["activo"]), 0)
        self.post("/panel/settings", {"accion": "alternar_recurrente", "recurrente_id": rid}, pagina=pag)
        self.assertEqual(int(self.sql("SELECT activo FROM recurrentes", one=True)["activo"]), 1)
        self.assertIn("Spotify Duo", self.client.get(pag).get_data(as_text=True))
        self.post("/panel/settings", {"accion": "borrar_recurrente", "recurrente_id": rid}, pagina=pag)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM recurrentes", one=True)["n"], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)   # el histórico se conserva

    def test_validacion_de_recurrentes(self):
        malos = [
            {"tipo": "gasto", "concepto": "X", "importe": "0", "dia_mes": "5"},
            {"tipo": "gasto", "concepto": "X", "importe": "10", "dia_mes": "32"},
            {"tipo": "gasto", "concepto": "X", "importe": "10", "dia_mes": "0"},
            {"tipo": "otro", "concepto": "X", "importe": "10", "dia_mes": "5"},
            {"tipo": "gasto", "concepto": "", "importe": "10", "dia_mes": "5"},
            {"tipo": "gasto", "concepto": "X", "importe": "nan", "dia_mes": "5"},
            {"tipo": "gasto", "concepto": "X", "importe": "inf", "dia_mes": "5"},
            {"tipo": "gasto", "concepto": "X", "importe": "-5", "dia_mes": "5"},
        ]
        for datos in malos:
            self.post("/panel/settings", {"accion": "crear_recurrente", **datos}, pagina="/panel/settings")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM recurrentes", one=True)["n"], 0)


class TestWrapped(FinanzasBase):
    def datos_febrero(self):
        self.anadir_mov("2026-01-10", "Gasto enero", 400)
        self.anadir_mov("2026-02-01", "Nómina", 2000, "ingreso", "Nómina y Salario")
        self.anadir_mov("2026-02-03", "Alquiler", 700, "gasto", "Vivienda")
        self.anadir_mov("2026-02-10", "Super", 300, "gasto", "Alimentación")
        self.anadir_mov("2026-02-11", "Super 2", 100, "gasto", "Alimentación")

    def test_wrapped_del_mes_anterior(self):
        self.datos_febrero()
        w = finance.wrapped_mes_anterior(self.db, self.uid, HOY)
        self.assertEqual(w["mes"], "2026-02")
        self.assertEqual((w["ingresos"], w["gastos"], w["ahorro"]), (2000.0, 1100.0, 900.0))
        self.assertEqual(w["ratio_ahorro"], 45.0)
        self.assertEqual(w["top_categorias"][0], {"categoria": "Vivienda", "total": 700.0})
        self.assertEqual(w["mayor_gasto"]["concepto"], "Alquiler")
        self.assertEqual(w["variacion_gastos"], 175.0)                 # 1100 vs 400

    def test_wrapped_none_si_el_mes_estuvo_vacio(self):
        self.anadir_mov("2026-01-10", "x", 1)
        self.assertIsNone(finance.wrapped_mes_anterior(self.db, self.uid, HOY))

    def test_wrapped_cruza_el_cambio_de_ano(self):
        self.anadir_mov("2025-12-20", "Navidad", 50)
        w = finance.wrapped_mes_anterior(self.db, self.uid, datetime.date(2026, 1, 3))
        self.assertEqual(w["mes"], "2025-12")

    def test_el_panel_muestra_y_oculta_el_wrapped(self):
        hoy = datetime.date.today()
        anterior = (hoy.replace(day=1) - datetime.timedelta(days=1))
        self.anadir_mov(anterior.replace(day=1).isoformat(), "Nómina", 2000, "ingreso", "Nómina y Salario")
        self.anadir_mov(anterior.replace(day=2).isoformat(), "Gasto", 500)
        html = self.client.get("/panel").get_data(as_text=True)
        self.assertIn("wrapped-card", html)
        self.assertIn("en cifras", html)
        mes = anterior.strftime("%Y-%m")
        self.post("/panel/cerrar_wrapped", {"mes": mes}, pagina="/panel")
        self.assertEqual(self.sql("SELECT wrapped_visto FROM usuarios", one=True)["wrapped_visto"], mes)
        self.assertNotIn("wrapped-card", self.client.get("/panel").get_data(as_text=True))

    def test_cerrar_wrapped_ignora_meses_invalidos(self):
        self.post("/panel/cerrar_wrapped", {"mes": "2999-01"}, pagina="/panel")
        self.post("/panel/cerrar_wrapped", {"mes": "'; DROP TABLE usuarios;--"}, pagina="/panel")
        self.assertIsNone(self.sql("SELECT wrapped_visto FROM usuarios", one=True)["wrapped_visto"])


class TestMetasYPresupuestos(FinanzasBase):
    def test_progreso_de_metas(self):
        self.anadir_mov("2026-01-01", "Nómina", 1500, "ingreso", "Nómina y Salario")
        self.exec_sql("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'Viaje', 3000)", (self.uid,))
        self.exec_sql("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo) VALUES (?, 'Casa', 1000)", (self.uid,))
        metas = {m["nombre"]: m for m in finance.progreso_metas(self.db, self.uid)}
        self.assertEqual(metas["Viaje"]["porcentaje"], 50.0)
        self.assertFalse(metas["Viaje"]["completada"])
        self.assertEqual(metas["Casa"]["porcentaje"], 100.0)
        self.assertTrue(metas["Casa"]["completada"])
        self.assertEqual(metas["Viaje"]["restante"], 1500.0)

    def test_alertas_de_presupuesto(self):
        self.exec_sql("INSERT INTO presupuestos (usuario_id, categoria, importe_mensual) VALUES (?, 'Alimentación', 100)", (self.uid,))
        self.exec_sql("INSERT INTO presupuestos (usuario_id, categoria, importe_mensual) VALUES (?, 'Vivienda', 100)", (self.uid,))
        self.anadir_mov("2026-03-02", "Super", 85, categoria="Alimentación")
        self.anadir_mov("2026-03-03", "Alquiler", 120, categoria="Vivienda")
        self.anadir_mov("2026-02-03", "Super", 999, categoria="Alimentación")      # otro mes: no cuenta
        alertas = finance.alertas_presupuesto(self.db, self.uid, "2026-03")
        self.assertEqual([a["nivel"] for a in alertas], ["danger", "warning"])

    def test_guardar_presupuestos_es_atomico_y_no_confunde_prefijos(self):
        pag = "/panel/settings"
        self.post("/panel/settings", {"accion": "guardar_presupuestos", "presu_Alimentación": "250,50",
                                      "presu_Vivienda": "700", "presu_Ocio": ""}, pagina=pag)
        filas = {f["categoria"]: float(f["importe_mensual"]) for f in self.sql("SELECT * FROM presupuestos")}
        self.assertEqual(filas, {"Alimentación": 250.5, "Vivienda": 700.0})
        # un valor inválido no destruye lo que ya había
        self.post("/panel/settings", {"accion": "guardar_presupuestos", "presu_Alimentación": "abc"}, pagina=pag)
        filas = {f["categoria"]: float(f["importe_mensual"]) for f in self.sql("SELECT * FROM presupuestos")}
        self.assertEqual(filas, {"Alimentación": 250.5, "Vivienda": 700.0})
        # categoría con el texto «presu_» dentro: no se corrompe
        self.post("/panel/settings", {"accion": "guardar_presupuestos", "presu_Mi presu_categoría": "10"}, pagina=pag)
        self.assertEqual([f["categoria"] for f in self.sql("SELECT categoria FROM presupuestos")], ["Mi presu_categoría"])


class TestAnalitica(FinanzasBase):
    def test_agrupa_por_dia_hasta_60_dias_y_por_mes_despues(self):
        self.anadir_mov("2026-03-01", "a", 10)
        self.anadir_mov("2026-03-02", "b", 20)
        self.anadir_mov("2026-02-15", "c", 30)
        corto = finance.analitica(self.db, self.uid, "2026-03-01", "2026-03-15", 15)
        self.assertEqual(corto["agrupacion"], "dia")
        self.assertEqual(corto["chart_gastos_mes"]["labels"], ["2026-03-01", "2026-03-02"])
        largo = finance.analitica(self.db, self.uid, "2026-01-01", "2026-03-15", 74)
        self.assertEqual(largo["agrupacion"], "mes")
        self.assertEqual(largo["chart_gastos_mes"]["labels"], ["2026-02", "2026-03"])
        self.assertEqual(largo["chart_gastos_mes"]["data"], [30.0, 30.0])
        justo = finance.analitica(self.db, self.uid, "2026-01-15", "2026-03-15", 60)
        self.assertEqual(justo["agrupacion"], "dia")

    def test_top_10_categorias_y_otras(self):
        for i in range(13):
            self.anadir_mov("2026-03-02", f"g{i}", 100 - i, categoria=f"Cat {i:02d}")
        d = finance.analitica(self.db, self.uid, "2026-03-01", "2026-03-15", 15)["chart_gastos_categoria"]
        self.assertEqual(len(d["labels"]), 10)
        self.assertEqual(d["labels"][-1], "Otras categorías")
        self.assertEqual(round(sum(d["data"]), 2), round(sum(100 - i for i in range(13)), 2))

    def test_totales_y_saldo(self):
        self.anadir_mov("2026-03-01", "Nómina", 1000, "ingreso", "Nómina y Salario")
        self.anadir_mov("2026-03-02", "Gasto", 250.55)
        t = finance.totales(self.db, self.uid, "2026-03-01", "2026-03-31")
        self.assertEqual((t["ingresos"], t["gastos"], t["ahorro"], t["ratio_ahorro"]), (1000.0, 250.55, 749.45, 74.9))
        self.assertEqual(finance.saldo_total(self.db, self.uid), 749.45)
        s = finance.serie_saldo(self.db, self.uid, "2026-03-02", "2026-03-31")
        self.assertEqual(s, {"labels": ["2026-03-02"], "data": [749.45]})      # parte del saldo previo (1000)

    def test_paginas_de_analitica(self):
        self.anadir_mov("2026-01-05", "Gasto", 10)
        for qs in ("", "?periodo=trimestre", "?periodo=anio", "?periodo=custom&desde=2026-01-01&hasta=2026-02-01",
                   "?periodo=custom&desde=mal&hasta=peor", "?periodo=%27%3B--"):
            self.assertEqual(self.client.get("/panel/analytics" + qs).status_code, 200, qs)
            self.assertEqual(self.client.get("/panel" + qs).status_code, 200, qs)


class TestDemoYDespegue(FinanzasBase):
    def test_usuario_nuevo_ve_despegue(self):
        html = self.client.get("/panel").get_data(as_text=True)
        self.assertIn("Todo listo para despegar", html)
        self.assertNotIn("kpi-grid", html)

    def test_cargar_y_borrar_demo(self):
        self.post("/panel/cargar_demo", {}, pagina="/panel")
        n = self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"]
        self.assertGreater(n, 50)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos WHERE demo = 1", one=True)["n"], n)
        html = self.client.get("/panel").get_data(as_text=True)
        self.assertIn("kpi-grid", html)
        self.assertIn("datos de ejemplo", html)
        self.assertNotIn("Todo listo para despegar", html)
        # fechas nunca en el futuro
        futuro = self.sql("SELECT COUNT(*) AS n FROM movimientos WHERE fecha > ?", (datetime.date.today().isoformat(),), one=True)
        self.assertEqual(futuro["n"], 0)
        # no se duplica
        self.post("/panel/cargar_demo", {}, pagina="/panel")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], n)
        self.post("/panel/borrar_demo", {}, pagina="/panel")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 0)

    def test_borrar_demo_conserva_lo_propio(self):
        self.post("/panel/cargar_demo", {}, pagina="/panel")
        self.anadir_mov("2026-03-01", "Mío", 5)
        self.post("/panel/borrar_demo", {}, pagina="/panel")
        self.assertEqual([m["concepto"] for m in self.sql("SELECT concepto FROM movimientos")], ["Mío"])

    def test_demo_no_se_carga_si_ya_hay_datos(self):
        self.anadir_mov("2026-03-01", "Mío", 5)
        self.post("/panel/cargar_demo", {}, pagina="/panel")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM movimientos", one=True)["n"], 1)


if __name__ == "__main__":
    unittest.main()
