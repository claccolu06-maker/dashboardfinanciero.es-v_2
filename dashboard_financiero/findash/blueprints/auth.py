"""Registro, inicio y cierre de sesión (correo/contraseña y Google)."""
from __future__ import annotations

import datetime
import hashlib
import secrets
import string
import time
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import security
from ..db import es_error_integridad, get_db
from ..validators import ValidacionError, limpiar_texto, validar_email, validar_password

bp = Blueprint("auth", __name__)

# Hash ficticio para igualar el tiempo de respuesta cuando el correo no existe.
_HASH_FICTICIO = generate_password_hash("no-existe-" + secrets.token_hex(8))
_ISSUERS_GOOGLE = {"accounts.google.com", "https://accounts.google.com"}


def _clave(prefijo: str, valor: str) -> str:
    return f"{prefijo}:{hashlib.sha256(valor.encode('utf-8')).hexdigest()[:32]}"


def _minutos(segundos: int) -> int:
    return max(1, (segundos + 59) // 60)


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------
@bp.route("/registro", methods=["GET", "POST"])
def registro():
    if "usuario_id" in session:
        return redirect(url_for("panel.dashboard"))
    if request.method == "GET":
        return render_template("registro.html")

    db, cfg = get_db(), current_app.config
    clave_ip = _clave("registro", security.ip_cliente())
    espera = security.bloqueado_segundos(db, clave_ip, cfg["REGISTRO_MAX_POR_IP"], cfg["REGISTRO_VENTANA_SEGUNDOS"])
    if espera:
        flash(f"Demasiados registros desde tu conexión. Inténtalo de nuevo en {_minutos(espera)} min.", "danger")
        return render_template("registro.html"), 429

    # Campo trampa para bots: una persona nunca lo rellena.
    if request.form.get("sitio_web", "").strip():
        security.registrar_fallo(db, clave_ip, cfg["REGISTRO_VENTANA_SEGUNDOS"])
        flash("Cuenta creada con éxito. Ya puedes iniciar sesión.", "success")
        return redirect(url_for("auth.login"))

    try:
        nombre = limpiar_texto(request.form.get("nombre"), 50, True, "El nombre")
        email = validar_email(request.form.get("email"))
        password = validar_password(request.form.get("password"))
        if request.form.get("password2") is not None and request.form.get("password2") != password:
            raise ValidacionError("Las contraseñas no coinciden.")
    except ValidacionError as exc:
        flash(str(exc), "danger")
        return render_template("registro.html", nombre=request.form.get("nombre", ""),
                               email=request.form.get("email", "")), 400

    security.registrar_fallo(db, clave_ip, cfg["REGISTRO_VENTANA_SEGUNDOS"])   # cuenta cada intento
    if db.query("SELECT id FROM usuarios WHERE email = ?", (email,), one=True):
        flash("Este correo electrónico ya está registrado.", "danger")
        return render_template("registro.html", nombre=nombre), 409
    try:
        db.insert(
            "INSERT INTO usuarios (nombre, email, password, creado_en) VALUES (?, ?, ?, ?)",
            (nombre, email, generate_password_hash(password), datetime.datetime.now().isoformat(timespec="seconds")),
        )
    except Exception as exc:                   # dos registros simultáneos con el mismo correo
        if not es_error_integridad(exc):
            raise
        flash("Este correo electrónico ya está registrado.", "danger")
        return render_template("registro.html", nombre=nombre), 409
    flash("Cuenta creada con éxito. Ya puedes iniciar sesión.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def _destino_tras_login() -> str:
    destino = request.args.get("next") or request.form.get("next")
    return destino if security.es_ruta_interna(destino) else url_for("panel.dashboard")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if "usuario_id" in session:
        return redirect(url_for("panel.dashboard"))
    siguiente = request.args.get("next") if security.es_ruta_interna(request.args.get("next")) else ""
    if request.method == "GET":
        return render_template("login.html", siguiente=siguiente,
                               google_activo=True)

    db, cfg = get_db(), current_app.config
    email = (request.form.get("email") or "").strip().lower()[:254]
    password = request.form.get("password") or ""
    clave_cuenta = _clave("login", f"{security.ip_cliente()}|{email}")
    clave_ip = _clave("login-ip", security.ip_cliente())
    ventana = cfg["LOGIN_VENTANA_SEGUNDOS"]

    espera = max(
        security.bloqueado_segundos(db, clave_cuenta, cfg["LOGIN_MAX_FALLOS"], ventana),
        security.bloqueado_segundos(db, clave_ip, cfg["LOGIN_MAX_FALLOS"] * 6, ventana),
    )
    if espera:
        flash(f"Demasiados intentos fallidos. Inténtalo de nuevo en {_minutos(espera)} min.", "danger")
        return render_template("login.html", siguiente=siguiente, google_activo=True), 429

    usuario = db.query("SELECT * FROM usuarios WHERE email = ?", (email,), one=True) if email else None
    hash_guardado = usuario["password"] if usuario else _HASH_FICTICIO
    correcto = check_password_hash(hash_guardado, password) and usuario is not None
    if not correcto:
        security.registrar_fallo(db, clave_cuenta, ventana)
        security.registrar_fallo(db, clave_ip, ventana)
        flash("Las credenciales introducidas no son válidas.", "danger")
        return render_template("login.html", siguiente=siguiente, google_activo=True), 401

    security.limpiar_fallos(db, clave_cuenta)
    security.iniciar_sesion(usuario)
    return redirect(_destino_tras_login())


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
def _validar_token_google(token: str) -> Dict[str, Any]:
    """Verifica el ``id_token`` con Google y comprueba que es para NUESTRA app.

    Sin comprobar ``aud`` cualquier token de Google emitido para *otra*
    aplicación valdría para entrar aquí. Devuelve los datos del usuario o lanza
    ``ValueError`` con un motivo (solo para el registro, nunca para el usuario).
    """
    cfg = current_app.config
    if len(token) > 4096:
        raise ValueError("token demasiado largo")
    try:
        r = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": token}, timeout=6)
    except requests.RequestException as exc:
        raise ConnectionError(str(exc)) from exc
    if r.status_code != 200:
        raise ValueError(f"Google rechazó el token ({r.status_code})")
    info = r.json()

    aud = str(info.get("aud", ""))
    if cfg["GOOGLE_CLIENT_ID"]:
        if aud != cfg["GOOGLE_CLIENT_ID"]:
            raise ValueError(f"aud inesperado: {aud}")
    elif cfg["GOOGLE_AUD_PREFIX"]:
        if not aud.startswith(cfg["GOOGLE_AUD_PREFIX"]):
            raise ValueError(f"aud inesperado: {aud}")
    else:
        raise ValueError("Ni GOOGLE_CLIENT_ID ni GOOGLE_AUD_PREFIX configurados")
    if info.get("iss") not in _ISSUERS_GOOGLE:
        raise ValueError(f"iss inesperado: {info.get('iss')}")
    try:
        if int(info.get("exp", 0)) <= int(time.time()):
            raise ValueError("token caducado")
    except (TypeError, ValueError):
        raise ValueError("exp no válido") from None
    if str(info.get("email_verified")).lower() != "true":
        raise ValueError("correo sin verificar")
    return {"email": validar_email(info.get("email")),
            "nombre": limpiar_texto(info.get("name"), 50) or str(info.get("email")).split("@")[0]}


@bp.post("/login_google")
def login_google():
    db, cfg = get_db(), current_app.config
    clave_ip = _clave("google", security.ip_cliente())
    ventana = cfg["LOGIN_VENTANA_SEGUNDOS"]
    if security.bloqueado_segundos(db, clave_ip, cfg["LOGIN_MAX_FALLOS"] * 4, ventana):
        flash("Demasiados intentos. Espera unos minutos.", "danger")
        return redirect(url_for("auth.login"))

    token = (request.form.get("id_token") or "").strip()
    if not token:
        flash("No se recibió información válida de Google.", "danger")
        return redirect(url_for("auth.login"))
    try:
        datos = _validar_token_google(token)
    except ConnectionError:
        current_app.logger.warning("Google tokeninfo no disponible", exc_info=True)
        flash("No hemos podido contactar con Google. Inténtalo de nuevo en unos minutos.", "danger")
        return redirect(url_for("auth.login"))
    except (ValueError, ValidacionError) as exc:
        current_app.logger.warning("Login con Google rechazado: %s", exc)
        security.registrar_fallo(db, clave_ip, ventana)
        flash("No hemos podido validar tu cuenta de Google.", "danger")
        return redirect(url_for("auth.login"))

    usuario = db.query("SELECT * FROM usuarios WHERE email = ?", (datos["email"],), one=True)
    if usuario is None:
        aleatoria = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        try:
            db.insert(
                "INSERT INTO usuarios (nombre, email, password, creado_en) VALUES (?, ?, ?, ?)",
                (datos["nombre"], datos["email"], generate_password_hash(aleatoria),
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
        except Exception as exc:
            if not es_error_integridad(exc):
                raise
        usuario = db.query("SELECT * FROM usuarios WHERE email = ?", (datos["email"],), one=True)
    security.iniciar_sesion(usuario)
    flash("Has iniciado sesión correctamente.", "success")
    return redirect(url_for("panel.dashboard"))


# ---------------------------------------------------------------------------
# Logout (POST: un enlace malicioso no puede cerrar la sesión de nadie)
# ---------------------------------------------------------------------------
@bp.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "GET":
        return redirect(url_for("panel.dashboard" if "usuario_id" in session else "auth.login"))
    session.clear()
    flash("Sesión cerrada correctamente.", "success")
    return redirect(url_for("auth.login"))
