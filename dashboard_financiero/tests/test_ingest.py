"""Motor de ingesta: formatos de extracto, límites y protecciones."""
import datetime
import io
import unittest
import zipfile

from findash.categorias import categorizar_movimiento
from findash.ingest import (
    ImportacionError, MAX_BYTES, analizar_archivo, reconstruir_movimientos, reconstruir_movimientos_detallado,
)
from findash.ingest.numeros import buscar_fechas, buscar_importes, parse_fecha, parse_numero


def resumen(movs):
    return [(m.fecha.isoformat(), m.concepto, round(m.importe, 2), m.tipo) for m in movs]


class TestNumeros(unittest.TestCase):
    def test_parse_numero(self):
        casos = {"1.234,56": 1234.56, "1,234.56": 1234.56, "12,5": 12.5, "12.5": 12.5, "-45,20": -45.2,
                 "(45,20)": -45.2, "1.234": 1234.0, "0,99": 0.99, "1.234.567,89": 1234567.89, "−12,00": -12.0,
                 "45,20-": -45.2, "1 234,56": 1234.56}
        for texto, esperado in casos.items():
            self.assertAlmostEqual(parse_numero(texto), esperado, msg=texto)
        for malo in ("", "abc", "12.345.67", "1,2,3", "--5"):
            self.assertIsNone(parse_numero(malo), malo)

    def test_fechas(self):
        self.assertEqual(parse_fecha("03/04/2026"), datetime.date(2026, 4, 3))
        self.assertEqual(parse_fecha("2026-04-03"), datetime.date(2026, 4, 3))
        self.assertEqual(parse_fecha("3-4-26"), datetime.date(2026, 4, 3))
        self.assertEqual(parse_fecha("31/02/2026"), None)
        self.assertTrue(buscar_fechas("COMPRA 12 mar 2026 X"))

    def test_importes_requieren_decimales(self):
        self.assertEqual(len(buscar_importes("REF 12345 PAGO -45,20 EUR")), 1)


class TestFormatosDeTexto(unittest.TestCase):
    def test_banca_online_concepto_fecha_importe_saldo(self):
        texto = (
            "COMPRA TARJ. MERCADONA VALENCIA 12/03/2026 -45,20 € 1.954,80 €\n"
            "NOMINA EMPRESA SL 13/03/2026 1.850,00 € 3.804,80 €\n"
            "RECIBO IBERDROLA 14/03/2026 -54,30 € 3.750,50 €\n")
        r = reconstruir_movimientos_detallado(texto)
        self.assertEqual(r.estrategia, "francotirador")
        self.assertEqual(resumen(r.movimientos), [
            ("2026-03-12", "COMPRA TARJ. MERCADONA VALENCIA", 45.2, "gasto"),
            ("2026-03-13", "NOMINA EMPRESA SL", 1850.0, "ingreso"),
            ("2026-03-14", "RECIBO IBERDROLA", 54.3, "gasto")])

    def test_csv_punto_y_coma_con_cabecera_e_importes_sin_signo(self):
        csv = ("Fecha;Concepto;Importe;Saldo\n"
               "01/03/2026;COMPRA ZARA;50,00;950,00\n"
               "02/03/2026;NOMINA;1.500,00;2.450,00\n"
               "03/03/2026;RESTAURANTE LA PLAZA;30,00;2.420,00\n")
        r = reconstruir_movimientos_detallado(csv)
        self.assertEqual([m.tipo for m in r.movimientos], ["gasto", "ingreso", "gasto"])   # deducido de la cadena de saldos

    def test_csv_ordenado_de_mas_reciente_a_mas_antiguo(self):
        csv = ("Fecha;Concepto;Importe;Saldo\n"
               "03/03/2026;RESTAURANTE LA PLAZA;30,00;2.420,00\n"
               "02/03/2026;NOMINA;1.500,00;2.450,00\n"
               "01/03/2026;COMPRA ZARA;50,00;950,00\n")
        r = reconstruir_movimientos_detallado(csv)
        self.assertEqual([m.fecha.day for m in r.movimientos], [1, 2, 3])                  # se devuelve cronológico
        self.assertEqual([m.tipo for m in r.movimientos], ["gasto", "ingreso", "gasto"])

    def test_debe_haber(self):
        csv = ("Fecha;Concepto;Debe;Haber;Saldo\n"
               "01/03/2026;COMPRA ZARA;50,00;;950,00\n"
               "02/03/2026;NOMINA;;1.500,00;2.450,00\n")
        r = reconstruir_movimientos_detallado(csv)
        self.assertEqual([(m.tipo, m.importe) for m in r.movimientos], [("gasto", 50.0), ("ingreso", 1500.0)])

    def test_csv_ingles_con_comas_entre_comillas(self):
        csv = ('Date,Description,Amount\n'
               '2026-03-01,"AMAZON, MARKETPLACE",-25.99\n'
               '2026-03-02,SALARY ACME INC,"2,500.00"\n')
        r = reconstruir_movimientos_detallado(csv)
        self.assertEqual(resumen(r.movimientos), [("2026-03-01", "AMAZON, MARKETPLACE", 25.99, "gasto"),
                                                  ("2026-03-02", "SALARY ACME INC", 2500.0, "ingreso")])

    def test_texto_pegado_con_tabuladores(self):
        texto = "01/03/2026\tCOMPRA ZARA\t-50,00\t950,00\n02/03/2026\tNOMINA\t1.500,00\t2.450,00\n"
        r = reconstruir_movimientos_detallado(texto)
        self.assertEqual([m.importe for m in r.movimientos], [50.0, 1500.0])
        self.assertEqual([m.tipo for m in r.movimientos], ["gasto", "ingreso"])

    def test_movimientos_en_varias_lineas(self):
        texto = ("12/03/2026\nCOMPRA MERCADONA\n-45,20 EUR\n\n"
                 "13/03/2026\nNOMINA EMPRESA\n1.850,00 EUR\n\n"
                 "14/03/2026\nRECIBO LUZ\n-54,30 EUR\n")
        r = reconstruir_movimientos_detallado(texto)
        self.assertEqual(len(r.movimientos), 3)
        self.assertEqual([m.importe for m in r.movimientos], [45.2, 1850.0, 54.3])
        self.assertEqual([m.tipo for m in r.movimientos], ["gasto", "ingreso", "gasto"])

    def test_meses_en_texto(self):
        r = reconstruir_movimientos_detallado("12 mar 2026 COMPRA ZARA -45,20\n13 mar 2026 NOMINA 1.850,00\n"
                                              "14 mar 2026 RECIBO LUZ -54,30\n")
        self.assertEqual([m.fecha.day for m in r.movimientos], [12, 13, 14])

    def test_fechas_estadounidenses_se_detectan(self):
        csv = ("Date,Description,Amount\n03/25/2026,STORE A,-10.00\n03/26/2026,STORE B,-20.00\n"
               "03/27/2026,STORE C,-30.00\n")
        r = reconstruir_movimientos_detallado(csv)
        self.assertEqual([m.fecha.isoformat() for m in r.movimientos], ["2026-03-25", "2026-03-26", "2026-03-27"])

    def test_texto_plano_sin_saltos(self):
        texto = "12/03/2026 COMPRA ZARA -45,20 13/03/2026 NOMINA EMPRESA 1.850,00 14/03/2026 RECIBO LUZ -54,30"
        r = reconstruir_movimientos_detallado(texto)
        self.assertEqual(len(r.movimientos), 3)

    def test_basura_se_rechaza_con_mensaje_claro(self):
        for texto in ("hola que tal\neste texto no tiene nada\nde interes", "1234\n5678", "   \n\n"):
            with self.assertRaises(ImportacionError):
                reconstruir_movimientos_detallado(texto)

    def test_limite_de_lineas(self):
        texto = "\n".join(f"01/03/2026 COMPRA {i} -1,00" for i in range(20_500))
        with self.assertRaises(ImportacionError) as ctx:
            reconstruir_movimientos_detallado(texto)
        self.assertIn("líneas", str(ctx.exception))

    def test_limite_de_movimientos(self):
        texto = "\n".join(f"01/03/2026 COMPRA {i} -1,{i % 100:02d}" for i in range(5_500))
        with self.assertRaises(ImportacionError) as ctx:
            reconstruir_movimientos_detallado(texto)
        self.assertIn("movimientos", str(ctx.exception))

    def test_importe_siempre_positivo_y_tipo_valido(self):
        r = reconstruir_movimientos("01/03/2026 COMPRA X -10,00\n02/03/2026 ABONO Y 20,00\n03/03/2026 COMPRA Z -5,00\n")
        for m in r:
            self.assertGreater(m.importe, 0)
            self.assertIn(m.tipo, ("ingreso", "gasto"))


class TestArchivos(unittest.TestCase):
    CSV = ("Fecha;Concepto;Importe;Saldo\n01/03/2026;COMPRA ZARA;-50,00;950,00\n"
           "02/03/2026;NOMINA;1.500,00;2.450,00\n03/03/2026;RESTAURANTE;-30,00;2.420,00\n")

    def test_csv_utf8_y_cp1252(self):
        for datos in (self.CSV.encode("utf-8"), self.CSV.encode("utf-8-sig"), self.CSV.replace("NOMINA", "NÓMINA ÑU").encode("cp1252")):
            r = analizar_archivo("extracto.csv", datos)
            self.assertEqual(len(r.movimientos), 3)

    def test_xlsx_con_preambulo_y_tipos_nativos(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Banco Ejemplo", None, None, None])
        ws.append(["Cuenta ES12 3456", None, None, None])
        ws.append([])
        ws.append(["Fecha", "Concepto", "Importe", "Saldo"])
        ws.append([datetime.date(2026, 3, 1), "COMPRA ZARA", -50.0, 950.0])
        ws.append([datetime.date(2026, 3, 2), "NOMINA", 1500.0, 2450.0])
        ws.append([datetime.date(2026, 3, 3), "RESTAURANTE", -30, 2420])
        buf = io.BytesIO()
        wb.save(buf)
        r = analizar_archivo("extracto.xlsx", buf.getvalue())
        self.assertEqual(resumen(r.movimientos), [("2026-03-01", "COMPRA ZARA", 50.0, "gasto"),
                                                  ("2026-03-02", "NOMINA", 1500.0, "ingreso"),
                                                  ("2026-03-03", "RESTAURANTE", 30.0, "gasto")])

    def test_pdf_de_dos_paginas_con_cabeceras_repetidas(self):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        filas = [["12/03/2026 COMPRA MERCADONA -45,20 954,80", "13/03/2026 NOMINA EMPRESA 1.850,00 2.804,80"],
                 ["14/03/2026 RECIBO LUZ -54,30 2.750,50", "15/03/2026 COMPRA ZARA -30,00 2.720,50"]]
        for pagina in filas:
            c.setFont("Helvetica", 10)
            c.drawString(50, 800, "BANCO EJEMPLO - EXTRACTO DE CUENTA")
            c.drawString(50, 785, "Titular: ANA GARCIA  IBAN ES00 0000")
            y = 740
            for linea in pagina:
                c.drawString(50, y, linea)
                y -= 18
            c.drawString(50, 40, "Pagina de extracto - BANCO EJEMPLO S.A.")
            c.showPage()
        c.save()
        r = analizar_archivo("extracto.pdf", buf.getvalue())
        self.assertEqual([m.importe for m in r.movimientos], [45.2, 1850.0, 54.3, 30.0])
        self.assertNotIn("BANCO", " ".join(m.concepto for m in r.movimientos))

    def test_tipos_no_admitidos(self):
        with self.assertRaises(ImportacionError):
            analizar_archivo("viejo.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        with self.assertRaises(ImportacionError):
            analizar_archivo("foto.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with self.assertRaises(ImportacionError):
            analizar_archivo("vacio.csv", b"")
        with self.assertRaises(ImportacionError):
            analizar_archivo("binario.csv", bytes(range(256)) * 20)

    def test_zip_bomb_xlsx(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("xl/workbook.xml", "<workbook/>")
            z.writestr("xl/worksheets/sheet1.xml", "0" * (110 * 1024 * 1024))     # 110 MB que comprimen a ~110 KB
        self.assertLess(len(buf.getvalue()), MAX_BYTES)
        with self.assertRaises(ImportacionError) as ctx:
            analizar_archivo("bomba.xlsx", buf.getvalue())
        self.assertIn("demasiado grande", str(ctx.exception))

    def test_xlsx_corrupto(self):
        with self.assertRaises(ImportacionError):
            analizar_archivo("roto.xlsx", b"PK\x03\x04" + b"basura" * 50)

    def test_pdf_corrupto(self):
        with self.assertRaises(ImportacionError):
            analizar_archivo("roto.pdf", b"%PDF-1.4\n" + b"basura" * 50)


class TestCategorizador(unittest.TestCase):
    def test_categorias_estandar(self):
        casos = [
            ("COMPRA MERCADONA VALENCIA", "gasto", "Alimentación"),
            ("NETFLIX.COM", "gasto", "Suscripciones Digitales"),
            ("SPOTIFY AB", "gasto", "Suscripciones Digitales"),
            ("REPSOL ESTACION SERVICIO", "gasto", "Vehículo y Gasolina"),
            ("RESTAURANTE LA PLAZA", "gasto", "Restaurantes y Comida"),
            ("FARMACIA GARCIA", "gasto", "Salud y Bienestar"),
            ("RECIBO IBERDROLA", "gasto", "Servicios del Hogar"),
            ("ALQUILER PISO MARZO", "gasto", "Vivienda"),
            ("RYANAIR", "gasto", "Viajes y Vacaciones"),
            ("MASTERCARD COMPRA XYZ", "gasto", "Gastos Generales"),         # falso positivo conocido: no debe ser Educación
            ("COMERCIO DESCONOCIDO 123", "gasto", "Gastos Generales"),
            ("NOMINA EMPRESA SL", "ingreso", "Nómina y Salario"),
            ("BIZUM DE JUAN", "ingreso", "Transferencias"),
            ("INGRESO VARIOS", "ingreso", "Otros Ingresos"),
        ]
        for concepto, tipo, esperado in casos:
            self.assertEqual(categorizar_movimiento(concepto, tipo), esperado, concepto)

    def test_reglas_del_usuario_mandan(self):
        self.assertEqual(categorizar_movimiento("MERCADONA X", "gasto", {"mercadona": "Compras y Ropa"}), "Compras y Ropa")

    def test_acentos_y_mayusculas(self):
        self.assertEqual(categorizar_movimiento("cafetería ñandú", "gasto"), categorizar_movimiento("CAFETERIA NANDU", "gasto"))


if __name__ == "__main__":
    unittest.main()
