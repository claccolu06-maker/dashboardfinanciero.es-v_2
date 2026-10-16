"""Configuración: perfil, presupuestos, metas, recurrentes y preferencias."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from ..categorias import CATEGORIAS_GASTO, CATEGORIAS_INGRESO, ICONOS, categorizar_movimiento, cargar_reglas
from ..db import get_db
from ..security import login_required
from ..services import finance
from ..validators import (
    MAX_CATEGORIA, MAX_CONCEPTO, MAX_NOMBRE, ValidacionError, limpiar_texto, validar_dia_mes, validar_fecha,
    validar_importe, validar_tipo, validar_tema,
)

bp = Blueprint("ajustes", __name__)

_PREFIJO_PRESUPUESTO = "presu_"


def _uid() -> int:
    return int(session["usuario_id"])


def _volver(pestana: str = ""):
    destino = url_for("ajustes.settings")
    return redirect(f"{destino}#{pestana}" if pestana else destino)


# ---------------------------------------------------------------------------
# Página y acciones generales (una sola URL con «accion», como antes)
# ---------------------------------------------------------------------------
@bp.route("/panel/settings", methods=["GET", "POST"])
@login_required
def settings():
    db, uid = get_db(), _uid()
    if request.method == "POST":
        accion = request.form.get("accion", "")
        manejador = _ACCIONES.get(accion)
        if manejador is None:
            flash("Acción no reconocida.", "danger")
            return _volver()
        try:
            pestana = manejador(db, uid)
        except ValidacionError as exc:
            flash(str(exc), "danger")
            pestana = _PESTANA_DE_ACCION.get(accion, "")
        return _volver(pestana)

    usuario = db.query("SELECT email FROM usuarios WHERE id = ?", (uid,), one=True)
    usadas = [f["categoria"] for f in db.query(
        "SELECT DISTINCT categoria FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto'", (uid,))]
    propias = sorted(c for c in usadas if c and c not in CATEGORIAS_GASTO)
    presupuestos = {f["categoria"]: float(f["importe_mensual"]) for f in db.query(
        "SELECT categoria, importe_mensual FROM presupuestos WHERE usuario_id = ?", (uid,))}
    # Un presupuesto de una categoría que ya no se usa tampoco debe desaparecer del formulario.
    propias += sorted(c for c in presupuestos if c not in CATEGORIAS_GASTO and c not in propias)
    return render_template(
        "settings.html",
        nombre=session.get("nombre"), email_real=usuario["email"] if usuario else "",
        categorias_presupuesto=list(CATEGORIAS_GASTO) + propias,
        presupuestos=presupuestos, iconos=ICONOS,
        metas=finance.progreso_metas(db, uid),
        recurrentes=finance.listar_recurrentes(db, uid),
        categorias_gasto=list(CATEGORIAS_GASTO) + propias,
        categorias_ingreso=list(CATEGORIAS_INGRESO),
    )


def _guardar_presupuestos(db, uid: int) -> str:
    nuevos: Dict[str, float] = {}
    for clave, valor in request.form.items():
        if not clave.startswith(_PREFIJO_PRESUPUESTO) or not valor.strip():
            continue
        categoria = limpiar_texto(clave[len(_PREFIJO_PRESUPUESTO):], MAX_CATEGORIA)
        if not categoria:
            continue
        try:
            nuevos[categoria] = validar_importe(valor)
        except ValidacionError:
            raise ValidacionError(f"El límite de «{categoria}» no es un importe válido.") from None
    with db.transaction():                        # todo o nada: nunca se pierden los límites a medias
        db.execute("DELETE FROM presupuestos WHERE usuario_id = ?", (uid,))
        for categoria, importe in nuevos.items():
            db.execute("INSERT INTO presupuestos (usuario_id, categoria, importe_mensual) VALUES (?, ?, ?)",
                       (uid, categoria, importe))
    flash("Presupuestos actualizados.", "success")
    return "presupuestos"


def _nueva_meta(db, uid: int) -> str:
    nombre = limpiar_texto(request.form.get("meta_nombre"), 60, True, "El nombre del objetivo")
    objetivo = validar_importe(request.form.get("meta_objetivo"))
    fecha_objetivo = None
    if request.form.get("meta_fecha", "").strip():
        fecha_objetivo = validar_fecha(request.form["meta_fecha"], permitir_futuro=True).isoformat()
    db.insert("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo, cantidad_actual, fecha_objetivo) "
              "VALUES (?, ?, ?, 0, ?)", (uid, nombre, objetivo, fecha_objetivo))
    flash("Objetivo creado con éxito.", "success")
    return "metas"


def _borrar_meta(db, uid: int) -> str:
    try:
        meta_id = int(request.form.get("meta_id", ""))
    except ValueError:
        raise ValidacionError("Meta no válida.") from None
    db.execute("DELETE FROM metas WHERE id = ? AND usuario_id = ?", (meta_id, uid))
    flash("Meta eliminada.", "success")
    return "metas"


def _guardar_perfil(db, uid: int) -> str:
    nombre = limpiar_texto(request.form.get("nombrevisible"), MAX_NOMBRE, True, "El nombre")
    if len(nombre) < 2:
        raise ValidacionError("El nombre debe tener al menos 2 caracteres.")
    db.execute("UPDATE usuarios SET nombre = ? WHERE id = ?", (nombre, uid))
    session["nombre"] = nombre
    flash("Perfil actualizado.", "success")
    return "perfil"


# ---------------------------------------------------------------------------
# Recurrentes (suscripciones, alquiler, nómina...)
# ---------------------------------------------------------------------------
def _datos_recurrente(db, uid: int) -> Dict[str, Any]:
    tipo = validar_tipo(request.form.get("tipo"))
    concepto = limpiar_texto(request.form.get("concepto"), MAX_CONCEPTO, True, "El concepto")
    importe = validar_importe(request.form.get("importe"))
    dia = validar_dia_mes(request.form.get("dia_mes"))
    categoria = limpiar_texto(request.form.get("categoria"), MAX_CATEGORIA)
    if not categoria:
        categoria = categorizar_movimiento(concepto, tipo, cargar_reglas(db, uid, tipo))
    return {"tipo": tipo, "concepto": concepto, "importe": importe, "dia_mes": dia, "categoria": categoria}


def _crear_recurrente(db, uid: int) -> str:
    d = _datos_recurrente(db, uid)
    db.insert(
        "INSERT INTO recurrentes (usuario_id, concepto, categoria, importe, tipo, dia_mes, activo) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (uid, d["concepto"], d["categoria"], d["importe"], d["tipo"], d["dia_mes"]))
    mensaje = "Movimiento recurrente creado."
    hoy = datetime.date.today()
    if request.form.get("aplicar_este_mes") and finance.procesar_recurrentes(db, uid, hoy):
        mensaje += " Ya se ha registrado el de este mes."
    flash(mensaje, "success")
    return "recurrentes"


def _id_recurrente() -> int:
    try:
        return int(request.form.get("recurrente_id", ""))
    except ValueError:
        raise ValidacionError("Movimiento recurrente no válido.") from None


def _editar_recurrente(db, uid: int) -> str:
    rid, d = _id_recurrente(), _datos_recurrente(db, uid)
    actualizados = db.execute(
        "UPDATE recurrentes SET concepto = ?, categoria = ?, importe = ?, tipo = ?, dia_mes = ? "
        "WHERE id = ? AND usuario_id = ?",
        (d["concepto"], d["categoria"], d["importe"], d["tipo"], d["dia_mes"], rid, uid))
    flash("Recurrente actualizado." if actualizados else "No se ha encontrado ese recurrente.",
          "success" if actualizados else "warning")
    return "recurrentes"


def _alternar_recurrente(db, uid: int) -> str:
    rid = _id_recurrente()
    db.execute("UPDATE recurrentes SET activo = CASE WHEN activo = 1 THEN 0 ELSE 1 END "
               "WHERE id = ? AND usuario_id = ?", (rid, uid))
    flash("Estado del recurrente actualizado.", "success")
    return "recurrentes"


def _borrar_recurrente(db, uid: int) -> str:
    rid = _id_recurrente()
    db.execute("DELETE FROM recurrentes WHERE id = ? AND usuario_id = ?", (rid, uid))
    flash("Recurrente eliminado. Los movimientos ya registrados se conservan.", "success")
    return "recurrentes"


_ACCIONES = {
    "guardar_presupuestos": _guardar_presupuestos,
    "nueva_meta": _nueva_meta,
    "borrar_meta": _borrar_meta,
    "guardar_perfil": _guardar_perfil,
    "crear_recurrente": _crear_recurrente,
    "editar_recurrente": _editar_recurrente,
    "alternar_recurrente": _alternar_recurrente,
    "borrar_recurrente": _borrar_recurrente,
}
_PESTANA_DE_ACCION = {
    "guardar_presupuestos": "presupuestos", "nueva_meta": "metas", "borrar_meta": "metas",
    "guardar_perfil": "perfil", "crear_recurrente": "recurrentes", "editar_recurrente": "recurrentes",
    "alternar_recurrente": "recurrentes", "borrar_recurrente": "recurrentes",
}


# ---------------------------------------------------------------------------
# Preferencias (tema, privacidad, animaciones): fuente de verdad = base de datos
# ---------------------------------------------------------------------------
@bp.post("/panel/settings/preferencias")
@login_required
def guardar_preferencias():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"ok": False, "error": "JSON no válido."}), 400
    tema = validar_tema(datos.get("tema"))
    privacidad = 1 if datos.get("privacidad") else 0
    animaciones = 1 if datos.get("animaciones") else 0
    get_db().execute("UPDATE usuarios SET tema = ?, privacidad = ?, animaciones = ? WHERE id = ?",
                     (tema, privacidad, animaciones, _uid()))
    session["tema"], session["privacidad"], session["animaciones"] = tema, privacidad, animaciones
    return jsonify({"ok": True, "status": "success", "tema": tema})
