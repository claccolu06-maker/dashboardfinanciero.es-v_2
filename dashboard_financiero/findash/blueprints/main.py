"""Rutas públicas: portada, PWA y comprobación de salud."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, redirect, render_template, session, url_for

from ..db import get_db

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    if "usuario_id" in session:
        return redirect(url_for("panel.dashboard"))
    return render_template("landing_publica.html")


@bp.get("/favicon.ico")
def favicon():
    return "", 204


@bp.get("/sw.js")
def service_worker():
    """El service worker se sirve desde la raíz para poder controlar todo el sitio."""
    respuesta = current_app.send_static_file("sw.js")
    respuesta.headers["Content-Type"] = "application/javascript; charset=utf-8"
    respuesta.headers["Service-Worker-Allowed"] = "/"
    respuesta.headers["Cache-Control"] = "no-cache"
    return respuesta


@bp.get("/healthz")
def healthz():
    """Para el health check de Render / UptimeRobot. No expone datos."""
    try:
        get_db().scalar("SELECT 1")
        return jsonify({"status": "ok", "db": get_db().dialect})
    except Exception:  # pragma: no cover
        current_app.logger.exception("healthz: la base de datos no responde")
        return jsonify({"status": "error"}), 503
