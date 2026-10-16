"""Importación de extractos: archivo (CSV/XLSX/PDF) o texto pegado.

Tres rutas con el mismo comportamiento:

* ``POST /panel/importar``        JSON ``{nombre, contenido (base64) | texto, dry_run, clave}``
* ``POST /panel/importar_csv``    multipart con el campo ``archivo``
* ``POST /panel/importar_texto``  formulario o JSON con ``texto``

Todas aceptan ``dry_run`` (vista previa: no escribe nada) y ``clave``, un
identificador que genera el navegador para cada importación. Si llega dos veces
la misma ``clave`` dentro de 600 s (doble clic, reintento de red) se devuelve el
resultado anterior en lugar de volver a procesar.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Any, Callable, Dict, List

from flask import Blueprint, current_app, flash, jsonify, redirect, request, session, url_for

from .. import security
from ..categorias import cargar_reglas
from ..db import get_db
from ..ingest import (
    ImportacionError, MAX_BYTES, VENTANA_IDEMPOTENCIA_SEGUNDOS, analizar_archivo,
    reconstruir_movimientos_detallado,
)
from ..ingest.importacion import categorizar_lote, procesar_importacion
from ..ingest.modelos import ResultadoParseo
from ..security import login_required

bp = Blueprint("importar", __name__)

_RE_CLAVE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MUESTRA_MAXIMA = 200
_MAX_CLAVES_EN_SESION = 6
_LIMITE_PETICIONES = (30, 60)          # 30 importaciones por minuto y usuario


def _uid() -> int:
    return int(session["usuario_id"])


def _verdadero(valor: Any) -> bool:
    return str(valor or "").strip().lower() in ("1", "true", "on", "si", "sí", "yes")


# ---------------------------------------------------------------------------
# Idempotencia (en la propia cookie de sesión: sirve con varios procesos)
# ---------------------------------------------------------------------------
def _claves_vigentes() -> Dict[str, Dict[str, Any]]:
    ahora = time.time()
    vigentes = {k: v for k, v in (session.get("imp_claves") or {}).items()
                if isinstance(v, dict) and ahora - float(v.get("ts", 0)) < VENTANA_IDEMPOTENCIA_SEGUNDOS}
    return vigentes


def _recordar_clave(clave: str, resumen: Dict[str, Any]) -> None:
    vigentes = _claves_vigentes()
    vigentes[clave] = {"ts": time.time(), **resumen}
    recientes = sorted(vigentes.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:_MAX_CLAVES_EN_SESION]
    session["imp_claves"] = dict(recientes)


# ---------------------------------------------------------------------------
# Núcleo común
# ---------------------------------------------------------------------------
def _error(mensaje: str, codigo: int = 400):
    if _quiere_json_importacion():
        return jsonify({"ok": False, "error": mensaje}), codigo
    flash(mensaje, "danger")
    return redirect(url_for("movimientos.transactions"))


def _quiere_json_importacion() -> bool:
    return request.is_json or security.quiere_json() or request.headers.get("X-Requested-With") == "fetch"


def _ejecutar(analizar: Callable[[], ResultadoParseo], dry_run: bool, clave: str):
    db, uid = get_db(), _uid()

    limite, ventana = _LIMITE_PETICIONES
    clave_rl = f"importar:{uid}"
    if security.bloqueado_segundos(db, clave_rl, limite, ventana):
        return _error("Estás importando demasiado rápido. Espera un minuto.", 429)
    security.registrar_fallo(db, clave_rl, ventana)

    if clave and not dry_run:
        previa = _claves_vigentes().get(clave)
        if previa:
            return _responder({"ok": True, "repetido": True, "dry_run": False,
                               **{k: v for k, v in previa.items() if k != "ts"}})

    try:
        parseo = analizar()
    except ImportacionError as exc:
        return _error(str(exc), 422)

    categorizar_lote(parseo.movimientos, cargar_reglas(db, uid, "gasto"), cargar_reglas(db, uid, "ingreso"))
    resultado = procesar_importacion(db, uid, parseo.movimientos, dry_run=dry_run)

    nuevos_ids = {id(m) for m in resultado["nuevos"]}
    resumen: Dict[str, Any] = {
        "insertados": resultado["insertados"],
        "duplicados": resultado["duplicados"],
        "descartadas": parseo.descartadas,
        "detectados": len(parseo.movimientos),
        "estrategia": parseo.estrategia,
        "avisos": parseo.avisos,
        "fecha_min": resultado["fecha_min"],
        "fecha_max": resultado["fecha_max"],
    }
    payload: Dict[str, Any] = {"ok": True, "repetido": False, "dry_run": dry_run, **resumen}

    if dry_run:
        nuevos = resultado["nuevos"]
        payload["nuevos"] = len(nuevos)
        payload["total_ingresos"] = round(sum(m.importe for m in nuevos if m.tipo == "ingreso"), 2)
        payload["total_gastos"] = round(sum(m.importe for m in nuevos if m.tipo == "gasto"), 2)
        payload["tipos_adivinados"] = sum(1 for m in parseo.movimientos if m.tipo_adivinado)
        payload["muestra"] = [
            {**m.como_dict(), "duplicado": id(m) not in nuevos_ids, "tipo_adivinado": m.tipo_adivinado}
            for m in parseo.movimientos[:MUESTRA_MAXIMA]
        ]
        payload["muestra_truncada"] = len(parseo.movimientos) > MUESTRA_MAXIMA
    else:
        if clave:
            _recordar_clave(clave, resumen)
        current_app.logger.info("Importación usuario=%s insertados=%s duplicados=%s estrategia=%s",
                                uid, resumen["insertados"], resumen["duplicados"], resumen["estrategia"])
    return _responder(payload)


def _responder(payload: Dict[str, Any]):
    if _quiere_json_importacion():
        return jsonify(payload)
    if payload.get("dry_run"):
        flash(f"Vista previa: {payload.get('nuevos', 0)} nuevos y {payload['duplicados']} ya existentes.", "info")
    else:
        flash(_mensaje_resumen(payload), "success" if payload["insertados"] else "warning")
    return redirect(url_for("movimientos.transactions"))


def _mensaje_resumen(p: Dict[str, Any]) -> str:
    partes: List[str] = [f"Importados {p['insertados']} movimientos"]
    if p["duplicados"]:
        partes.append(f"{p['duplicados']} ya existían")
    if p["descartadas"]:
        partes.append(f"{p['descartadas']} líneas descartadas")
    return ", ".join(partes) + "."


def _clave_peticion(datos: Any) -> str:
    clave = str(datos.get("clave") or "").strip()
    return clave if _RE_CLAVE.match(clave) else ""


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@bp.post("/panel/importar")
@login_required
def importar():
    """JSON: ``{"nombre": "extracto.pdf", "contenido": "<base64>", "dry_run": true}``
    o ``{"texto": "..."}``."""
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return _error("Envía un JSON con «contenido» (base64) o «texto».", 400)
    dry_run, clave = _verdadero(datos.get("dry_run")), _clave_peticion(datos)

    if datos.get("texto"):
        return _desde_texto(str(datos["texto"]), dry_run, clave)

    contenido_b64 = datos.get("contenido")
    if not contenido_b64 or not isinstance(contenido_b64, str):
        return _error("Falta el contenido del archivo.", 400)
    if len(contenido_b64) > MAX_BYTES * 4 // 3 + 16:
        return _error("El archivo supera el máximo permitido (10 MB).", 413)
    try:
        contenido = base64.b64decode(contenido_b64.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError):
        return _error("El contenido del archivo no es Base64 válido.", 400)
    return _desde_bytes(str(datos.get("nombre") or "extracto.csv"), contenido, dry_run, clave)


@bp.post("/panel/importar_csv")
@login_required
def importar_csv():
    """Formulario multipart con el campo ``archivo`` (CSV, XLSX o PDF)."""
    archivo = request.files.get("archivo")
    if archivo is None or not archivo.filename:
        return _error("Selecciona un archivo para importar.", 400)
    contenido = archivo.read(MAX_BYTES + 1)
    return _desde_bytes(archivo.filename, contenido, _verdadero(request.form.get("dry_run")),
                        _clave_peticion(request.form))


@bp.post("/panel/importar_texto")
@login_required
def importar_texto():
    """Texto copiado y pegado desde la banca online."""
    datos = request.get_json(silent=True) if request.is_json else request.form
    datos = datos or {}
    texto = str(datos.get("texto") or "")
    return _desde_texto(texto, _verdadero(datos.get("dry_run")), _clave_peticion(datos))


def _desde_bytes(nombre: str, contenido: bytes, dry_run: bool, clave: str):
    if not contenido:
        return _error("El archivo está vacío.", 400)
    if len(contenido) > MAX_BYTES:
        return _error("El archivo supera el máximo permitido (10 MB).", 413)
    return _ejecutar(lambda: analizar_archivo(nombre, contenido), dry_run, clave)


def _desde_texto(texto: str, dry_run: bool, clave: str):
    if not texto.strip():
        return _error("Pega primero el texto de tu extracto.", 400)
    if len(texto.encode("utf-8", "ignore")) > MAX_BYTES:
        return _error("El texto supera el máximo permitido (10 MB).", 413)
    return _ejecutar(lambda: reconstruir_movimientos_detallado(texto), dry_run, clave)
