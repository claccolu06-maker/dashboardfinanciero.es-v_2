"""Panel principal: dashboard, analítica, histórico, Wrapped y modo demo."""
from __future__ import annotations

import datetime
import re

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..db import get_db
from ..security import login_required, quiere_json
from ..services import demo, finance

bp = Blueprint("panel", __name__)

_RE_MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _uid() -> int:
    return int(session["usuario_id"])


@bp.get("/panel")
@login_required
def dashboard():
    db, uid = get_db(), _uid()
    hoy = datetime.date.today()

    # Piloto automático: los cargos/abonos fijos que ya tocan se registran solos.
    nuevos_recurrentes = finance.procesar_recurrentes(db, uid, hoy)
    if nuevos_recurrentes:
        flash(f"Se han registrado {nuevos_recurrentes} movimiento(s) recurrente(s) de este mes.", "info")

    periodo = finance.resolver_periodo(request.args, session, hoy)
    if periodo["error"]:
        flash(periodo["error"], "warning")

    total_movimientos = finance.contar_movimientos(db, uid)
    es_nuevo_usuario = total_movimientos == 0           # «Modo Despegue»
    tot = finance.totales(db, uid, periodo["inicio"], periodo["fin"])
    saldo = finance.saldo_total(db, uid)
    serie = finance.serie_saldo(db, uid, periodo["inicio"], periodo["fin"])
    proy = finance.proyeccion_fin_de_mes(db, uid, hoy)
    curva = (finance.curva_proyeccion(serie, proy)
             if periodo["fin"] == hoy.isoformat() and serie["labels"] else None)
    runway = finance.calcular_runway(db, uid, hoy)
    metas = finance.progreso_metas(db, uid, saldo)

    # Wrapped del mes anterior (se muestra hasta que el usuario lo cierre).
    wrapped = finance.wrapped_mes_anterior(db, uid, hoy)
    visto = db.scalar("SELECT wrapped_visto FROM usuarios WHERE id = ?", (uid,))
    mostrar_wrapped = bool(wrapped) and visto != wrapped["mes"]
    ahorro_mes_pasado = wrapped["ahorro"] if wrapped else 0.0

    pendientes = [r for r in finance.listar_recurrentes(db, uid, hoy) if r["pendiente_este_mes"]]

    return render_template(
        "dashboard.html",
        periodo=periodo["periodo"], rango=periodo,
        nombre=session.get("nombre"),
        es_nuevo_usuario=es_nuevo_usuario,
        tiene_demo=demo.tiene_demo(db, uid) if not es_nuevo_usuario else False,
        totales=tot, saldo=saldo,
        ratio_ahorro=tot["ratio_ahorro"],
        movimientos=finance.ultimos_movimientos(db, uid, periodo["inicio"], periodo["fin"], 10),
        alertas_presupuesto=finance.alertas_presupuesto(db, uid, hoy.strftime("%Y-%m")),
        runway=runway,
        proyeccion=proy,
        metas=metas, meta_activa=metas[0] if metas else None,
        mostrar_wrapped=mostrar_wrapped, wrapped=wrapped, ahorro_mes_pasado=ahorro_mes_pasado,
        recurrentes_pendientes=pendientes[:5],
        chart_saldo_tiempo=serie,
        chart_proyeccion=curva,
        hoy=hoy.isoformat(),
    )


@bp.post("/panel/cerrar_wrapped")
@login_required
def cerrar_wrapped():
    """Marca el resumen mensual como visto para no volver a mostrarlo."""
    db, uid = get_db(), _uid()
    mes = (request.form.get("mes") or (request.get_json(silent=True) or {}).get("mes") or "").strip()
    if not _RE_MES.match(mes) or mes >= datetime.date.today().strftime("%Y-%m"):
        wrapped = finance.wrapped_mes_anterior(db, uid)
        mes = wrapped["mes"] if wrapped else ""
    if mes:
        db.execute("UPDATE usuarios SET wrapped_visto = ? WHERE id = ?", (mes, uid))
    if quiere_json():
        return jsonify({"ok": True})
    return redirect(url_for("panel.dashboard"))


@bp.post("/panel/cargar_demo")
@login_required
def cargar_demo():
    n = demo.cargar_demo(get_db(), _uid())
    if n:
        flash(f"Hemos cargado {n} movimientos de ejemplo para que veas FinDash en acción. "
              "Puedes borrarlos cuando quieras.", "success")
    else:
        flash("Ya tienes movimientos registrados; no se han añadido datos de ejemplo.", "warning")
    return redirect(url_for("panel.dashboard"))


@bp.post("/panel/borrar_demo")
@login_required
def borrar_demo():
    n = demo.borrar_demo(get_db(), _uid())
    flash(f"Datos de ejemplo eliminados ({n}).", "success")
    return redirect(url_for("panel.dashboard"))


@bp.get("/panel/analytics")
@login_required
def analytics():
    db, uid = get_db(), _uid()
    hoy = datetime.date.today()
    periodo = finance.resolver_periodo(request.args, session, hoy)
    if periodo["error"]:
        flash(periodo["error"], "warning")
    tot = finance.totales(db, uid, periodo["inicio"], periodo["fin"])
    datos = finance.analitica(db, uid, periodo["inicio"], periodo["fin"], periodo["dias"])
    runway = finance.calcular_runway(db, uid, hoy)
    total_gasto = sum(datos["chart_gastos_categoria"]["data"]) or 0.0
    ranking = [
        {"categoria": c, "total": v, "porcentaje": round(v / total_gasto * 100, 1) if total_gasto else 0.0}
        for c, v in zip(datos["chart_gastos_categoria"]["labels"], datos["chart_gastos_categoria"]["data"])
    ]
    return render_template(
        "analytics.html",
        rango=periodo["periodo"], periodo=periodo["periodo"], intervalo=periodo,
        totales=tot, runway=runway, ranking=ranking,
        agrupacion=datos["agrupacion"],
        chart_ahorro_mes=datos["chart_ahorro_mes"],
        chart_ingresos_mes=datos["chart_ingresos_mes"],
        chart_gastos_mes=datos["chart_gastos_mes"],
        chart_gastos_categoria=datos["chart_gastos_categoria"],
        hoy=hoy.isoformat(),
    )


@bp.get("/panel/historico")
@login_required
def historico():
    db, uid = get_db(), _uid()
    filas = db.query(
        "SELECT SUBSTR(fecha, 1, 7) AS mes, "
        "COALESCE(SUM(CASE WHEN tipo = 'ingreso' THEN importe ELSE 0 END), 0) AS ingresos, "
        "COALESCE(SUM(CASE WHEN tipo = 'gasto' THEN importe ELSE 0 END), 0) AS gastos "
        "FROM movimientos WHERE usuario_id = ? GROUP BY SUBSTR(fecha, 1, 7) ORDER BY mes DESC", (uid,))
    for f in filas:
        f["ingresos"], f["gastos"] = float(f["ingresos"]), float(f["gastos"])
        f["ahorro"] = round(f["ingresos"] - f["gastos"], 2)
        f["ratio"] = round(f["ahorro"] / f["ingresos"] * 100, 1) if f["ingresos"] > 0 else 0.0
        f["nombre"] = finance.nombre_mes(f["mes"])
    cronologico = list(reversed(filas))
    return render_template(
        "historico.html", resumen_mensual=filas,
        grafico_ahorro_mes={"labels": [f["mes"] for f in cronologico],
                            "data": [f["ahorro"] for f in cronologico]} if filas else None,
    )
