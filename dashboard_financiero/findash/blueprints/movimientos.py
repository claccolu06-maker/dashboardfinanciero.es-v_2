"""Alta, edición, borrado, duplicado y exportación de movimientos."""
from __future__ import annotations

import csv
import datetime
import io
from typing import Any, Dict, List

from flask import (
    Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)

from ..categorias import (
    CATEGORIAS_GASTO, CATEGORIAS_INGRESO, aprender_regla, cargar_reglas,
    categorizar_movimiento, clave_comercio, recategorizar_similares,
)
from ..db import get_db
from ..security import login_required, quiere_json, redirigir_atras
from ..validators import MAX_CATEGORIA, MAX_CONCEPTO, ValidacionError, limpiar_texto, validar_movimiento

bp = Blueprint("movimientos", __name__)

POR_PAGINA = 25
_CARACTERES_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _uid() -> int:
    return int(session["usuario_id"])


def _categorias_para_formularios(db) -> Dict[str, List[str]]:
    """Catálogo estándar + categorías propias (incluidas las antiguas que el usuario ya use)."""
    usadas = [f["categoria"] for f in db.query(
        "SELECT DISTINCT categoria FROM movimientos WHERE usuario_id = ?", (_uid(),))]
    estandar = set(CATEGORIAS_GASTO) | set(CATEGORIAS_INGRESO)
    propias = sorted(c for c in usadas if c and c not in estandar)
    return {
        "gasto": list(CATEGORIAS_GASTO),
        "ingreso": list(CATEGORIAS_INGRESO),
        "propias": propias,
    }


# ---------------------------------------------------------------------------
# Rutas antiguas (marcadores y enlaces guardados)
# ---------------------------------------------------------------------------
@bp.get("/añadir")
@bp.get("/anadir")
@login_required
def redireccion_antigua_anadir():
    return redirect(url_for("movimientos.anadir"))


@bp.get("/transactions")
@login_required
def redireccion_antigua_transactions():
    return redirect(url_for("movimientos.transactions"))


# ---------------------------------------------------------------------------
# Alta
# ---------------------------------------------------------------------------
@bp.route("/panel/anadir", methods=["GET", "POST"])
@login_required
def anadir():
    db, uid = get_db(), _uid()
    if request.method == "POST":
        try:
            fecha, concepto, categoria, importe, tipo = validar_movimiento(request.form)
        except ValidacionError as exc:
            flash(str(exc), "danger")
            return render_template("anadir.html", categorias=_categorias_para_formularios(db),
                                   valores=request.form,
                                   hoy=datetime.date.today().isoformat()), 400
        if not categoria:                         # «Automática»: heurística + reglas aprendidas
            categoria = categorizar_movimiento(concepto, tipo, cargar_reglas(db, uid, tipo))
        db.insert(
            "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, fecha.isoformat(), concepto, categoria, importe, tipo),
        )
        flash(f"Transacción registrada en «{categoria}».", "success")
        return redirect(url_for("panel.dashboard"))
    return render_template("anadir.html", categorias=_categorias_para_formularios(db), valores={},
                           hoy=datetime.date.today().isoformat())


# ---------------------------------------------------------------------------
# Listado
# ---------------------------------------------------------------------------
@bp.get("/panel/transactions")
@login_required
def transactions():
    db, uid = get_db(), _uid()
    tipo = request.args.get("tipo") if request.args.get("tipo") in ("ingreso", "gasto") else ""
    categoria = limpiar_texto(request.args.get("categoria"), MAX_CATEGORIA)
    condiciones, params = ["usuario_id = ?"], [uid]
    if tipo:
        condiciones.append("tipo = ?")
        params.append(tipo)
    if categoria:
        condiciones.append("categoria = ?")
        params.append(categoria)
    donde = " AND ".join(condiciones)           # solo fragmentos literales: los valores van como parámetros

    total = int(db.scalar(f"SELECT COUNT(*) FROM movimientos WHERE {donde}", params, 0))
    total_paginas = max(1, (total + POR_PAGINA - 1) // POR_PAGINA)
    pagina = min(max(request.args.get("page", 1, type=int) or 1, 1), total_paginas)
    movimientos = db.query(
        f"SELECT id, fecha, concepto, categoria, importe, tipo FROM movimientos WHERE {donde} "
        "ORDER BY fecha DESC, id DESC LIMIT ? OFFSET ?",
        [*params, POR_PAGINA, (pagina - 1) * POR_PAGINA])
    return render_template(
        "transactions.html", movimientos=movimientos, page=pagina, total_pages=total_paginas, total=total,
        filtro_tipo=tipo, filtro_categoria=categoria,
        categorias=_categorias_para_formularios(db),
        hay_movimientos=bool(total) or bool(db.scalar("SELECT COUNT(*) FROM movimientos WHERE usuario_id = ?", (uid,), 0)),
        hoy=datetime.date.today().isoformat(),
    )


# ---------------------------------------------------------------------------
# Edición (con aprendizaje de categorías)
# ---------------------------------------------------------------------------
def _datos_peticion() -> Any:
    return request.get_json(silent=True) if request.is_json else request.form


@bp.post("/panel/editar_movimiento")
@login_required
def editar_movimiento():
    """Edita un movimiento. Si el usuario corrige la categoría, FinDash aprende.

    Campos: ``id``, ``fecha``, ``concepto``, ``categoria``, ``importe``, ``tipo`` y,
    opcionalmente, ``aplicar_similares`` para recategorizar también los movimientos
    pasados del mismo comercio.
    """
    db, uid = get_db(), _uid()
    datos = _datos_peticion() or {}
    try:
        movimiento_id = int(str(datos.get("id", "")).strip())
    except ValueError:
        return _error_edicion("Movimiento no válido.", 400)
    actual = db.query("SELECT id, concepto, categoria, tipo FROM movimientos WHERE id = ? AND usuario_id = ?",
                      (movimiento_id, uid), one=True)
    if actual is None:                       # inexistente o de otro usuario: misma respuesta
        return _error_edicion("Movimiento no encontrado.", 404)
    try:
        fecha, concepto, categoria, importe, tipo = validar_movimiento(datos)
    except ValidacionError as exc:
        return _error_edicion(str(exc), 400)
    if not categoria:
        categoria = categorizar_movimiento(concepto, tipo, cargar_reglas(db, uid, tipo))

    aplicar = str(datos.get("aplicar_similares", "")).lower() in ("1", "true", "on", "si", "sí")
    recategorizados, clave = 0, ""
    with db.transaction():
        db.execute(
            "UPDATE movimientos SET fecha = ?, concepto = ?, categoria = ?, importe = ?, tipo = ? "
            "WHERE id = ? AND usuario_id = ?",
            (fecha.isoformat(), concepto, categoria, importe, tipo, movimiento_id, uid))
        if categoria != actual["categoria"] or tipo != actual["tipo"]:
            clave = aprender_regla(db, uid, tipo, concepto, categoria)
            if aplicar and clave:
                recategorizados = recategorizar_similares(db, uid, tipo, clave, categoria, excluir_id=movimiento_id)

    mensaje = "Movimiento actualizado."
    if clave:
        mensaje += f" Recordaré que «{clave}» es «{categoria}»."
    if recategorizados:
        mensaje += f" Además he corregido {recategorizados} movimiento(s) similar(es)."
    if quiere_json():
        return jsonify({
            "ok": True, "mensaje": mensaje, "recategorizados": recategorizados,
            "regla": {"clave": clave, "categoria": categoria} if clave else None,
            "movimiento": {"id": movimiento_id, "fecha": fecha.isoformat(), "concepto": concepto,
                           "categoria": categoria, "importe": importe, "tipo": tipo},
        })
    flash(mensaje, "success")
    return redirigir_atras("movimientos.transactions")


def _error_edicion(mensaje: str, codigo: int):
    if quiere_json():
        return jsonify({"ok": False, "error": mensaje}), codigo
    flash(mensaje, "danger")
    return redirigir_atras("movimientos.transactions")


@bp.get("/panel/sugerir_categoria")
@login_required
def sugerir_categoria():
    """Categoría que FinDash asignaría a un concepto (para el formulario de alta)."""
    db, uid = get_db(), _uid()
    tipo = "ingreso" if request.args.get("tipo") == "ingreso" else "gasto"
    concepto = limpiar_texto(request.args.get("concepto"), MAX_CONCEPTO)
    if not concepto:
        return jsonify({"categoria": ""})
    return jsonify({"categoria": categorizar_movimiento(concepto, tipo, cargar_reglas(db, uid, tipo)),
                    "comercio": clave_comercio(concepto)})


# ---------------------------------------------------------------------------
# Borrar y duplicar
# ---------------------------------------------------------------------------
@bp.post("/panel/borrar/<int:movimiento_id>")
@login_required
def borrar(movimiento_id: int):
    borrados = get_db().execute("DELETE FROM movimientos WHERE id = ? AND usuario_id = ?",
                                (movimiento_id, _uid()))
    flash("Transacción eliminada." if borrados else "Esa transacción ya no existe.",
          "success" if borrados else "warning")
    return redirigir_atras("movimientos.transactions")


@bp.post("/panel/duplicar/<int:movimiento_id>")
@login_required
def duplicar(movimiento_id: int):
    db, uid = get_db(), _uid()
    mov = db.query("SELECT concepto, categoria, importe, tipo FROM movimientos WHERE id = ? AND usuario_id = ?",
                   (movimiento_id, uid), one=True)
    if mov is None:
        flash("Esa transacción ya no existe.", "warning")
    else:
        concepto = f"{mov['concepto']} (Copia)"
        db.insert(
            "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, datetime.date.today().isoformat(), concepto[:MAX_CONCEPTO], mov["categoria"],
             float(mov["importe"]), mov["tipo"]))
        flash("Registro duplicado con éxito.", "success")
    return redirect(url_for("movimientos.transactions"))


# ---------------------------------------------------------------------------
# Exportación
# ---------------------------------------------------------------------------
def _seguro_para_hoja_de_calculo(texto: Any) -> str:
    """Neutraliza la «inyección de fórmulas» (=, +, -, @) al abrir el CSV en Excel."""
    texto = str(texto if texto is not None else "")
    return "'" + texto if texto.startswith(_CARACTERES_FORMULA) else texto


@bp.get("/exportar_csv")
@login_required
def exportar_csv():
    movimientos = get_db().query(
        "SELECT fecha, tipo, concepto, categoria, importe FROM movimientos "
        "WHERE usuario_id = ? ORDER BY fecha DESC, id DESC", (_uid(),))
    if not movimientos:
        flash("No hay movimientos para exportar.", "warning")
        return redirigir_atras("movimientos.transactions")

    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";", lineterminator="\n")
    escritor.writerow(["Fecha", "Tipo", "Concepto", "Categoria", "Importe (EUR)"])
    for fila in movimientos:
        escritor.writerow([
            fila["fecha"], str(fila["tipo"]).upper(),
            _seguro_para_hoja_de_calculo(fila["concepto"]), _seguro_para_hoja_de_calculo(fila["categoria"]),
            f"{float(fila['importe']):.2f}".replace(".", ","),
        ])
    nombre = f"findash_{datetime.date.today():%Y%m%d}.csv"
    return Response(
        salida.getvalue().encode("utf-8-sig"), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
