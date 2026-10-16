"""Seguridad transversal: CSRF, autenticación, límites de intentos, cabeceras y errores.

La protección CSRF es **central**: un único ``before_request`` valida el token
en *todas* las peticiones que modifican estado (POST/PUT/PATCH/DELETE), de modo
que ninguna ruta nueva puede olvidarse de protegerse. El token viaja en el campo
``csrf_token`` de los formularios o en la cabecera ``X-CSRFToken`` de las
llamadas ``fetch``.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from functools import wraps
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from flask import (
    Flask, current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)
from werkzeug.exceptions import HTTPException

log = logging.getLogger("findash.security")

METODOS_SEGUROS = {"GET", "HEAD", "OPTIONS"}


# ---------------------------------------------------------------------------
# Utilidades de petición
# ---------------------------------------------------------------------------
def quiere_json() -> bool:
    if request.is_json:
        return True
    mejor = request.accept_mimetypes.best_match(["application/json", "text/html"])
    if mejor != "application/json":
        return False
    return request.accept_mimetypes["application/json"] > request.accept_mimetypes["text/html"]


def ip_cliente() -> str:
    return request.remote_addr or "desconocida"


def es_ruta_interna(destino: Optional[str]) -> bool:
    """Solo rutas relativas del propio sitio (evita redirecciones abiertas)."""
    if not destino:
        return False
    partes = urlparse(destino)
    return (not partes.scheme and not partes.netloc and destino.startswith("/")
            and not destino.startswith("//") and "\\" not in destino)


def redirigir_atras(endpoint_por_defecto: str, **valores: Any):
    """Vuelve a la página anterior solo si es del mismo sitio; si no, al endpoint dado."""
    referer = request.referrer
    if referer:
        partes = urlparse(referer)
        if partes.netloc == request.host:
            ruta = partes.path + (f"?{partes.query}" if partes.query else "")
            if es_ruta_interna(ruta):
                return redirect(ruta)
    return redirect(url_for(endpoint_por_defecto, **valores))


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def generate_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = session["csrf_token"] = secrets.token_hex(32)
    return token


def validate_csrf(token: Optional[str]) -> bool:
    esperado = session.get("csrf_token")
    if not token or not esperado:
        return False
    return hmac.compare_digest(str(token), str(esperado))


def _token_de_la_peticion() -> Optional[str]:
    return (request.headers.get("X-CSRFToken")
            or request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token"))


def _proteger_csrf():
    if request.method in METODOS_SEGUROS or request.endpoint in (None, "static"):
        return None
    if validate_csrf(_token_de_la_peticion()):
        return None
    log.warning("CSRF rechazado: %s %s desde %s", request.method, request.path, ip_cliente())
    mensaje = "Tu sesión de seguridad ha caducado. Recarga la página e inténtalo de nuevo."
    if quiere_json() or request.headers.get("X-CSRFToken"):
        return jsonify({"ok": False, "error": mensaje, "csrf": True}), 400
    flash(mensaje, "danger")
    destino = "auth.login" if "usuario_id" not in session else "panel.dashboard"
    return redirect(url_for(destino))


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------
def login_required(vista: Callable) -> Callable:
    @wraps(vista)
    def envoltorio(*args: Any, **kwargs: Any):
        if "usuario_id" not in session:
            if quiere_json():
                return jsonify({"ok": False, "error": "Tu sesión ha caducado. Vuelve a iniciar sesión."}), 401
            return redirect(url_for("auth.login"))
        return vista(*args, **kwargs)
    return envoltorio


def iniciar_sesion(usuario: dict) -> None:
    """Abre sesión limpiando la anterior y cargando las preferencias guardadas."""
    session.clear()
    session.permanent = True
    session["usuario_id"] = usuario["id"]
    session["nombre"] = usuario["nombre"]
    session["periodo"] = "mes"
    session["tema"] = usuario.get("tema") or "dark"
    session["privacidad"] = 1 if usuario.get("privacidad") else 0
    session["animaciones"] = 1 if usuario.get("animaciones") else 0
    generate_csrf_token()


# ---------------------------------------------------------------------------
# Limitación de intentos (en base de datos: válida con varios procesos)
# ---------------------------------------------------------------------------
def bloqueado_segundos(db: Any, clave: str, maximo: int, ventana: int) -> int:
    """Segundos que faltan para poder reintentar (0 si no hay bloqueo)."""
    fila = db.query("SELECT fallos, ultimo_fallo FROM login_intentos WHERE clave = ?", (clave,), one=True)
    if not fila:
        return 0
    restante = ventana - (int(time.time()) - int(fila["ultimo_fallo"]))
    return restante if fila["fallos"] >= maximo and restante > 0 else 0


def registrar_fallo(db: Any, clave: str, ventana: int) -> None:
    ahora = int(time.time())
    with db.transaction():
        fila = db.query("SELECT fallos, ultimo_fallo FROM login_intentos WHERE clave = ?", (clave,), one=True)
        fallos = 1 if (not fila or ahora - int(fila["ultimo_fallo"]) >= ventana) else int(fila["fallos"]) + 1
        db.execute(
            "INSERT INTO login_intentos (clave, fallos, ultimo_fallo) VALUES (?, ?, ?) "
            "ON CONFLICT (clave) DO UPDATE SET fallos = excluded.fallos, ultimo_fallo = excluded.ultimo_fallo",
            (clave, fallos, ahora),
        )
        if secrets.randbelow(50) == 0:    # limpieza oportunista
            db.execute("DELETE FROM login_intentos WHERE ultimo_fallo < ?", (ahora - 86400,))


def limpiar_fallos(db: Any, clave: str) -> None:
    db.execute("DELETE FROM login_intentos WHERE clave = ?", (clave,))


# ---------------------------------------------------------------------------
# Cabeceras de seguridad
# ---------------------------------------------------------------------------
def _cabeceras(respuesta):
    h = respuesta.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Datos financieros privados: que ni el navegador ni un proxy los guarden
    # (evita ver páginas privadas con el botón "atrás" tras cerrar sesión).
    if "usuario_id" in session and request.endpoint != "static" and \
            respuesta.mimetype in ("text/html", "application/json", "text/csv"):
        h["Cache-Control"] = "no-store, private"
    return respuesta


# ---------------------------------------------------------------------------
# Manejo de errores
# ---------------------------------------------------------------------------
_TITULOS = {
    400: "Petición no válida", 403: "Acceso denegado", 404: "Página no encontrada",
    405: "Método no permitido", 413: "Archivo demasiado grande", 429: "Demasiadas peticiones",
    500: "Algo ha ido mal",
}
_MENSAJES = {
    400: "No hemos podido entender la petición.",
    403: "No tienes permiso para ver esto.",
    404: "La página que buscas no existe o se ha movido.",
    405: "Esa acción no está permitida en esta dirección.",
    413: "El archivo supera el tamaño máximo permitido (10 MB).",
    429: "Has hecho demasiados intentos. Espera un momento antes de volver a intentarlo.",
    500: "Ha ocurrido un error inesperado. Ya lo hemos registrado; inténtalo de nuevo en unos instantes.",
}


def _manejar_error(error: Exception):
    if not isinstance(error, HTTPException) and current_app.debug:
        raise error                      # en desarrollo, que salte el depurador
    codigo = error.code if isinstance(error, HTTPException) and error.code else 500
    if codigo >= 500:
        log.exception("Error no controlado en %s %s", request.method, request.path)
    mensaje = _MENSAJES.get(codigo, "Se ha producido un error.")
    es_api = request.path.startswith(("/panel/importar", "/panel/editar_movimiento")) and request.method != "GET"
    if quiere_json() or es_api:
        return jsonify({"ok": False, "error": mensaje}), codigo
    try:
        return render_template("errors/error.html", codigo=codigo,
                               titulo=_TITULOS.get(codigo, "Error"), mensaje=mensaje), codigo
    except Exception:  # pragma: no cover - plantilla rota: texto plano
        return mensaje, codigo


def init_app(app: Flask) -> None:
    app.before_request(_proteger_csrf)
    app.after_request(_cabeceras)
    app.jinja_env.globals["csrf_token"] = generate_csrf_token
    for codigo in (400, 403, 404, 405, 413, 429):
        app.register_error_handler(codigo, _manejar_error)
    app.register_error_handler(Exception, _manejar_error)
