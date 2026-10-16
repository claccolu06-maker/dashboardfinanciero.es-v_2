"""Lógica de negocio financiera (sin Flask): periodos, KPIs, runway, proyección,
alertas, metas, wrapped mensual y piloto automático de recurrentes.

Todas las funciones reciben el objeto ``Database`` y el ``usuario_id``; todo el
SQL usa solo ``?``, ``SUBSTR`` y funciones comunes a SQLite y PostgreSQL.
"""
from __future__ import annotations

import calendar
import datetime
from typing import Any, Dict, List, Mapping, Optional

# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------
def format_currency(valor: Any) -> str:
    try:
        return f"{float(valor):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "0,00 €"


MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
            "septiembre", "octubre", "noviembre", "diciembre"]


def nombre_mes(clave: str) -> str:
    """'2026-03' -> 'marzo 2026'."""
    anio, mes = clave.split("-")
    return f"{MESES_ES[int(mes) - 1]} {anio}"


def ultimo_dia(anio: int, mes: int) -> int:
    return calendar.monthrange(anio, mes)[1]


# ---------------------------------------------------------------------------
# Periodos
# ---------------------------------------------------------------------------
PERIODOS = ("mes", "trimestre", "anio", "custom")
ETIQUETAS = {"mes": "Este mes", "trimestre": "Últimos 90 días", "anio": "Año actual", "custom": "Personalizado"}
MAX_DIAS_RANGO = 3660


def _fecha_iso(valor: Any) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(valor).strip())
    except (TypeError, ValueError):
        return None


def resolver_periodo(args: Mapping[str, Any], sesion: Any, hoy: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Interpreta ``?periodo=`` (+ ``desde``/``hasta`` si es ``custom``).

    Devuelve ``inicio``/``fin`` (ISO) listos para SQL. El periodo se recuerda en
    la sesión; si el rango personalizado no es válido se vuelve a «mes» y se
    informa en ``error``.
    """
    hoy = hoy or datetime.date.today()
    periodo = args.get("periodo") or sesion.get("periodo") or "mes"
    if periodo not in PERIODOS:
        periodo = "mes"
    error = None
    desde = hasta = None
    if periodo == "custom":
        desde = _fecha_iso(args.get("desde") or sesion.get("desde"))
        hasta = _fecha_iso(args.get("hasta") or sesion.get("hasta"))
        if not desde or not hasta:
            error, periodo = "Indica una fecha de inicio y otra de fin válidas.", "mes"
        else:
            hasta = min(hasta, hoy)
            if desde > hasta:
                error, periodo = "La fecha de inicio no puede ser posterior a la de fin.", "mes"
            elif (hasta - desde).days > MAX_DIAS_RANGO:
                error, periodo = "El rango máximo es de 10 años.", "mes"
    if periodo == "trimestre":
        inicio, fin = hoy - datetime.timedelta(days=90), hoy
    elif periodo == "anio":
        inicio, fin = hoy.replace(month=1, day=1), hoy
    elif periodo == "custom":
        inicio, fin = desde, hasta
    else:
        inicio, fin = hoy.replace(day=1), hoy
    sesion["periodo"] = periodo
    if periodo == "custom":
        sesion["desde"], sesion["hasta"] = inicio.isoformat(), fin.isoformat()
    return {
        "periodo": periodo,
        "inicio": inicio.isoformat(),
        "fin": fin.isoformat(),
        "dias": (fin - inicio).days + 1,
        "desde": inicio.isoformat(),
        "hasta": fin.isoformat(),
        "etiqueta": ETIQUETAS[periodo],
        "error": error,
    }


# ---------------------------------------------------------------------------
# Totales y series
# ---------------------------------------------------------------------------
_SUMA_NETA = "COALESCE(SUM(CASE WHEN tipo = 'ingreso' THEN importe ELSE -importe END), 0)"


def totales(db, uid: int, inicio: str, fin: str) -> Dict[str, float]:
    fila = db.query(
        "SELECT COALESCE(SUM(CASE WHEN tipo = 'ingreso' THEN importe ELSE 0 END), 0) AS ingresos, "
        "COALESCE(SUM(CASE WHEN tipo = 'gasto' THEN importe ELSE 0 END), 0) AS gastos "
        "FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ?",
        (uid, inicio, fin), one=True)
    ingresos, gastos = round(float(fila["ingresos"]), 2), round(float(fila["gastos"]), 2)
    ahorro = round(ingresos - gastos, 2)
    ratio = round(ahorro / ingresos * 100, 1) if ingresos > 0 else 0.0
    return {"ingresos": ingresos, "gastos": gastos, "ahorro": ahorro, "ratio_ahorro": ratio}


def saldo_total(db, uid: int, hasta: Optional[str] = None) -> float:
    if hasta:
        valor = db.scalar(f"SELECT {_SUMA_NETA} FROM movimientos WHERE usuario_id = ? AND fecha <= ?", (uid, hasta), 0)
    else:
        valor = db.scalar(f"SELECT {_SUMA_NETA} FROM movimientos WHERE usuario_id = ?", (uid,), 0)
    return round(float(valor), 2)


def contar_movimientos(db, uid: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM movimientos WHERE usuario_id = ?", (uid,), 0))


def serie_saldo(db, uid: int, inicio: str, fin: str) -> Dict[str, List[Any]]:
    """Saldo acumulado al cierre de cada día con movimientos dentro del periodo."""
    previo = float(db.scalar(f"SELECT {_SUMA_NETA} FROM movimientos WHERE usuario_id = ? AND fecha < ?",
                             (uid, inicio), 0))
    filas = db.query(
        f"SELECT fecha, {_SUMA_NETA} AS neto FROM movimientos "
        "WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? GROUP BY fecha ORDER BY fecha",
        (uid, inicio, fin))
    etiquetas, valores = [], []
    saldo = previo
    for f in filas:
        saldo += float(f["neto"])
        etiquetas.append(f["fecha"])
        valores.append(round(saldo, 2))
    return {"labels": etiquetas, "data": valores}


def ultimos_movimientos(db, uid: int, inicio: str, fin: str, limite: int = 10) -> List[Dict[str, Any]]:
    return db.query(
        "SELECT id, fecha, concepto, categoria, importe, tipo FROM movimientos "
        "WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? ORDER BY fecha DESC, id DESC LIMIT ?",
        (uid, inicio, fin, limite))


# ---------------------------------------------------------------------------
# Presupuestos
# ---------------------------------------------------------------------------
def alertas_presupuesto(db, uid: int, mes: str) -> List[Dict[str, str]]:
    """Semáforo del mes ``YYYY-MM``: ≥80 % aviso, ≥100 % peligro."""
    presupuestos = {f["categoria"]: float(f["importe_mensual"]) for f in db.query(
        "SELECT categoria, importe_mensual FROM presupuestos WHERE usuario_id = ?", (uid,))}
    if not presupuestos:
        return []
    gastos = db.query(
        "SELECT categoria, SUM(importe) AS total FROM movimientos "
        "WHERE usuario_id = ? AND tipo = 'gasto' AND SUBSTR(fecha, 1, 7) = ? GROUP BY categoria",
        (uid, mes))
    alertas = []
    for g in gastos:
        tope = presupuestos.get(g["categoria"])
        if not tope or tope <= 0:
            continue
        total = float(g["total"])
        ratio = total / tope
        if ratio >= 1.0:
            alertas.append((ratio, {"nivel": "danger", "texto":
                f"Límite superado en {g['categoria']}: {format_currency(total)} / {format_currency(tope)}"}))
        elif ratio >= 0.8:
            alertas.append((ratio, {"nivel": "warning", "texto":
                f"Atención: {ratio * 100:.0f}% consumido en {g['categoria']} "
                f"({format_currency(total)} / {format_currency(tope)})"}))
    alertas.sort(key=lambda x: x[0], reverse=True)
    return [a for _r, a in alertas]


# ---------------------------------------------------------------------------
# Colchón financiero (runway)
# ---------------------------------------------------------------------------
def calcular_runway(db, uid: int, hoy: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Meses que cubre el **saldo neto acumulado** a la media de gasto mensual.

    La media usa los últimos 3 meses *cerrados* con gastos. Si aún no hay
    ninguno, se estima con el mes en curso (a partir del día 5). El colchón es
    0 cuando el saldo acumulado no es positivo.
    """
    hoy = hoy or datetime.date.today()
    mes_actual = hoy.strftime("%Y-%m")
    saldo = saldo_total(db, uid)
    filas = db.query(
        "SELECT SUBSTR(fecha, 1, 7) AS mes, SUM(importe) AS total FROM movimientos "
        "WHERE usuario_id = ? AND tipo = 'gasto' AND SUBSTR(fecha, 1, 7) < ? "
        "GROUP BY SUBSTR(fecha, 1, 7) ORDER BY mes DESC LIMIT 3",
        (uid, mes_actual))
    media: Optional[float] = None
    base = None
    if filas:
        media = sum(float(f["total"]) for f in filas) / len(filas)
        base = f"media de los últimos {len(filas)} mes(es) cerrado(s)"
    elif hoy.day >= 5:
        gastos_mes = float(db.scalar(
            "SELECT COALESCE(SUM(importe), 0) FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' "
            "AND SUBSTR(fecha, 1, 7) = ?", (uid, mes_actual), 0))
        if gastos_mes > 0:
            media = gastos_mes / hoy.day * ultimo_dia(hoy.year, hoy.month)
            base = "estimación con el mes en curso"
    if not media or media <= 0:
        return {"meses": None, "saldo": saldo, "media_gasto": None, "base": None}
    meses = round(saldo / media, 1) if saldo > 0 else 0.0
    return {"meses": meses, "saldo": saldo, "media_gasto": round(media, 2), "base": base}


# ---------------------------------------------------------------------------
# Recurrentes: piloto automático
# ---------------------------------------------------------------------------
PREFIJO_RECURRENTE = "🔄 "


def dia_efectivo(dia_mes: int, anio: int, mes: int) -> int:
    """El día 31 en un mes de 30 días se cobra el 30."""
    return min(int(dia_mes), ultimo_dia(anio, mes))


def procesar_recurrentes(db, uid: int, hoy: Optional[datetime.date] = None) -> int:
    """Inserta los cargos/abonos fijos del mes que ya tocan y no se han procesado.

    Es idempotente y seguro con peticiones simultáneas: cada recurrente se
    «reclama» con un UPDATE condicional dentro de una transacción; solo quien
    consigue actualizar la fila inserta el movimiento.
    """
    hoy = hoy or datetime.date.today()
    mes = hoy.strftime("%Y-%m")
    pendientes = db.query(
        "SELECT id, concepto, categoria, importe, tipo, dia_mes FROM recurrentes "
        "WHERE usuario_id = ? AND activo = 1 AND (ultimo_mes_procesado IS NULL OR ultimo_mes_procesado <> ?)",
        (uid, mes))
    creados = 0
    for r in pendientes:
        dia = dia_efectivo(r["dia_mes"], hoy.year, hoy.month)
        if dia > hoy.day:
            continue
        with db.transaction():
            reclamado = db.execute(
                "UPDATE recurrentes SET ultimo_mes_procesado = ? "
                "WHERE id = ? AND usuario_id = ? AND (ultimo_mes_procesado IS NULL OR ultimo_mes_procesado <> ?)",
                (mes, r["id"], uid, mes))
            if reclamado == 1:
                db.execute(
                    "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (uid, datetime.date(hoy.year, hoy.month, dia).isoformat(),
                     PREFIJO_RECURRENTE + r["concepto"], r["categoria"], float(r["importe"]), r["tipo"]))
                creados += 1
    return creados


def recurrentes_futuros(db, uid: int, hoy: Optional[datetime.date] = None) -> Dict[str, float]:
    """Cargos y abonos fijos que aún faltan por llegar este mes."""
    hoy = hoy or datetime.date.today()
    mes = hoy.strftime("%Y-%m")
    gastos = ingresos = 0.0
    for r in db.query(
            "SELECT importe, tipo, dia_mes FROM recurrentes WHERE usuario_id = ? AND activo = 1 "
            "AND (ultimo_mes_procesado IS NULL OR ultimo_mes_procesado <> ?)", (uid, mes)):
        if dia_efectivo(r["dia_mes"], hoy.year, hoy.month) > hoy.day:
            if r["tipo"] == "gasto":
                gastos += float(r["importe"])
            else:
                ingresos += float(r["importe"])
    return {"gastos": round(gastos, 2), "ingresos": round(ingresos, 2)}


def listar_recurrentes(db, uid: int, hoy: Optional[datetime.date] = None) -> List[Dict[str, Any]]:
    hoy = hoy or datetime.date.today()
    mes = hoy.strftime("%Y-%m")
    filas = db.query(
        "SELECT id, concepto, categoria, importe, tipo, dia_mes, ultimo_mes_procesado, activo "
        "FROM recurrentes WHERE usuario_id = ? ORDER BY dia_mes, id", (uid,))
    for f in filas:
        f["pendiente_este_mes"] = bool(f["activo"]) and f["ultimo_mes_procesado"] != mes
    return filas


# ---------------------------------------------------------------------------
# Proyección a fin de mes
# ---------------------------------------------------------------------------
def proyeccion_fin_de_mes(db, uid: int, hoy: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Saldo estimado a final de mes.

    ``saldo_proyectado = saldo_actual - gasto_diario * días_restantes
    - recurrentes_gasto_pendientes + recurrentes_ingreso_pendientes``
    donde ``gasto_diario = gastos_del_mes / día_actual``.
    """
    hoy = hoy or datetime.date.today()
    mes = hoy.strftime("%Y-%m")
    gastos_mes = float(db.scalar(
        "SELECT COALESCE(SUM(importe), 0) FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' "
        "AND SUBSTR(fecha, 1, 7) = ?", (uid, mes), 0))
    saldo_actual = saldo_total(db, uid, hoy.isoformat())
    ultimo = ultimo_dia(hoy.year, hoy.month)
    restantes = ultimo - hoy.day
    velocidad = gastos_mes / hoy.day
    fijos = recurrentes_futuros(db, uid, hoy)
    saldo_proyectado = saldo_actual - velocidad * restantes - fijos["gastos"] + fijos["ingresos"]
    gastos_proyectados = gastos_mes + velocidad * restantes + fijos["gastos"]
    return {
        "saldo_actual": round(saldo_actual, 2),
        "saldo_proyectado": round(saldo_proyectado, 2),
        "gasto_diario": round(velocidad, 2),
        "gastos_proyectados": round(gastos_proyectados, 2),
        "dias_restantes": restantes,
        "fecha_fin": datetime.date(hoy.year, hoy.month, ultimo).isoformat(),
        "fijos": fijos,
    }


def curva_proyeccion(serie: Dict[str, List[Any]], proy: Dict[str, Any]) -> Dict[str, List[Any]]:
    """Etiquetas y datos para dibujar la proyección como línea discontinua.

    Devuelve las mismas etiquetas que la serie más la fecha de fin de mes, con
    ``None`` salvo en el último punto real y en el punto proyectado.
    """
    etiquetas = list(serie["labels"])
    if proy["fecha_fin"] in etiquetas:
        return {"labels": etiquetas, "data": [None] * len(etiquetas)}
    datos: List[Any] = [None] * len(etiquetas)
    if datos:
        datos[-1] = serie["data"][-1]
    etiquetas.append(proy["fecha_fin"])
    datos.append(proy["saldo_proyectado"])
    return {"labels": etiquetas, "data": datos}


# ---------------------------------------------------------------------------
# Metas
# ---------------------------------------------------------------------------
def progreso_metas(db, uid: int, ahorro_acumulado: Optional[float] = None) -> List[Dict[str, Any]]:
    """Metas con su % de avance respecto al ahorro histórico acumulado."""
    if ahorro_acumulado is None:
        ahorro_acumulado = saldo_total(db, uid)
    filas = db.query(
        "SELECT id, nombre, cantidad_objetivo FROM metas WHERE usuario_id = ? ORDER BY id DESC", (uid,))
    for f in filas:
        objetivo = float(f["cantidad_objetivo"])
        avance = max(ahorro_acumulado, 0.0)
        f["porcentaje"] = round(min(avance / objetivo * 100, 100.0), 1) if objetivo > 0 else 0.0
        f["completada"] = objetivo > 0 and avance >= objetivo
        f["restante"] = round(max(objetivo - avance, 0.0), 2)
    return filas


# ---------------------------------------------------------------------------
# Wrapped mensual
# ---------------------------------------------------------------------------
def _mes_anterior(hoy: datetime.date, atras: int = 1) -> str:
    anio, mes = hoy.year, hoy.month - atras
    while mes < 1:
        mes += 12
        anio -= 1
    return f"{anio:04d}-{mes:02d}"


def wrapped_mes_anterior(db, uid: int, hoy: Optional[datetime.date] = None) -> Optional[Dict[str, Any]]:
    """Resumen del mes anterior; ``None`` si ese mes no tuvo movimientos."""
    hoy = hoy or datetime.date.today()
    mes = _mes_anterior(hoy, 1)
    previo = _mes_anterior(hoy, 2)

    def tot(m: str) -> Dict[str, float]:
        f = db.query(
            "SELECT COALESCE(SUM(CASE WHEN tipo = 'ingreso' THEN importe ELSE 0 END), 0) AS i, "
            "COALESCE(SUM(CASE WHEN tipo = 'gasto' THEN importe ELSE 0 END), 0) AS g, COUNT(*) AS n "
            "FROM movimientos WHERE usuario_id = ? AND SUBSTR(fecha, 1, 7) = ?", (uid, m), one=True)
        return {"ingresos": float(f["i"]), "gastos": float(f["g"]), "n": int(f["n"])}

    actual = tot(mes)
    if actual["n"] == 0:
        return None
    anterior = tot(previo)
    ahorro = round(actual["ingresos"] - actual["gastos"], 2)
    top = db.query(
        "SELECT categoria, SUM(importe) AS total FROM movimientos "
        "WHERE usuario_id = ? AND tipo = 'gasto' AND SUBSTR(fecha, 1, 7) = ? "
        "GROUP BY categoria ORDER BY total DESC LIMIT 3", (uid, mes))
    mayor = db.query(
        "SELECT concepto, importe, fecha FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' "
        "AND SUBSTR(fecha, 1, 7) = ? ORDER BY importe DESC, id DESC LIMIT 1", (uid, mes), one=True)
    variacion = None
    if anterior["gastos"] > 0:
        variacion = round((actual["gastos"] - anterior["gastos"]) / anterior["gastos"] * 100, 1)
    return {
        "mes": mes,
        "mes_nombre": nombre_mes(mes),
        "ingresos": round(actual["ingresos"], 2),
        "gastos": round(actual["gastos"], 2),
        "ahorro": ahorro,
        "ratio_ahorro": round(ahorro / actual["ingresos"] * 100, 1) if actual["ingresos"] > 0 else 0.0,
        "movimientos": actual["n"],
        "top_categorias": [{"categoria": t["categoria"], "total": round(float(t["total"]), 2)} for t in top],
        "mayor_gasto": ({"concepto": mayor["concepto"], "importe": round(float(mayor["importe"]), 2),
                         "fecha": mayor["fecha"]} if mayor else None),
        "variacion_gastos": variacion,
    }


# ---------------------------------------------------------------------------
# Analítica
# ---------------------------------------------------------------------------
UMBRAL_AGRUPACION_MENSUAL_DIAS = 60


def analitica(db, uid: int, inicio: str, fin: str, dias: int) -> Dict[str, Any]:
    """Series de ingresos/gastos/ahorro y desglose por categoría.

    Si el rango es mayor de 60 días se agrupa por mes (``YYYY-MM``); si no, por día.
    """
    mensual = dias > UMBRAL_AGRUPACION_MENSUAL_DIAS
    expresion = "SUBSTR(fecha, 1, 7)" if mensual else "fecha"      # constante: nunca entrada del usuario
    filas = db.query(
        f"SELECT {expresion} AS etiqueta, "
        "COALESCE(SUM(CASE WHEN tipo = 'ingreso' THEN importe ELSE 0 END), 0) AS ingresos, "
        "COALESCE(SUM(CASE WHEN tipo = 'gasto' THEN importe ELSE 0 END), 0) AS gastos "
        f"FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? "
        f"GROUP BY {expresion} ORDER BY etiqueta", (uid, inicio, fin))
    etiquetas = [f["etiqueta"] for f in filas]
    ingresos = [round(float(f["ingresos"]), 2) for f in filas]
    gastos = [round(float(f["gastos"]), 2) for f in filas]
    ahorro = [round(i - g, 2) for i, g in zip(ingresos, gastos)]

    cats = db.query(
        "SELECT categoria, SUM(importe) AS total FROM movimientos "
        "WHERE usuario_id = ? AND tipo = 'gasto' AND fecha >= ? AND fecha <= ? "
        "GROUP BY categoria ORDER BY total DESC", (uid, inicio, fin))
    labels_cat = [c["categoria"] for c in cats]
    valores_cat = [round(float(c["total"]), 2) for c in cats]
    if len(labels_cat) > 10:                                     # legibilidad del donut
        resto = round(sum(valores_cat[9:]), 2)
        labels_cat, valores_cat = labels_cat[:9] + ["Otras categorías"], valores_cat[:9] + [resto]
    return {
        "agrupacion": "mes" if mensual else "dia",
        "chart_ahorro_mes": {"labels": etiquetas, "data": ahorro},
        "chart_ingresos_mes": {"labels": etiquetas, "data": ingresos},
        "chart_gastos_mes": {"labels": etiquetas, "data": gastos},
        "chart_gastos_categoria": {"labels": labels_cat, "data": valores_cat},
    }
