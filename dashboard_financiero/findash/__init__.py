"""FinDash: panel de control financiero personal (Flask).

Uso típico::

    from findash import create_app
    app = create_app()

``create_app`` admite un diccionario de ajustes que se superpone a la
configuración leída del entorno (útil en tests)::

    app = create_app({"TESTING": True, "DATABASE_PATH": "/tmp/prueba.db"})
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import Flask

from . import db, security
from .config import construir_config
from .services.finance import format_currency

__all__ = ["create_app"]


def create_app(overrides: Optional[Dict[str, Any]] = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False,
                static_folder="../static", template_folder="../templates")
    app.config.from_mapping(construir_config(overrides))

    logging.basicConfig(level=logging.INFO if not app.config["TESTING"] else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if app.config["TRUST_PROXY"] > 0:
        # Render / PythonAnywhere: la IP real y el esquema https llegan en cabeceras.
        from werkzeug.middleware.proxy_fix import ProxyFix
        n = app.config["TRUST_PROXY"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)  # type: ignore[assignment]

    db.init_app(app)
    security.init_app(app)
    app.jinja_env.filters["format_currency"] = format_currency
    app.jinja_env.globals["format_currency"] = format_currency

    from .blueprints import ajustes, auth, importar, main, movimientos, panel
    for modulo in (main, auth, panel, movimientos, importar, ajustes):
        app.register_blueprint(modulo.bp)

    @app.context_processor
    def _contexto():
        from flask import session
        return {
            "tema_servidor": session.get("tema", "dark") if session.get("usuario_id") else "dark",
            "privacidad": bool(session.get("privacidad")),
            "animaciones": bool(session.get("animaciones")),
        }

    return app
