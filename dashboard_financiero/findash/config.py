"""Configuración centralizada de FinDash.

Toda la configuración sale de variables de entorno, de modo que el mismo código
funciona en local (SQLite), en PythonAnywhere y en Render + Neon (PostgreSQL)
sin tocar ficheros.

Variables principales
---------------------
FINDASH_ENV            development (por defecto) | production
SECRET_KEY             clave de firma de sesiones. OBLIGATORIA en producción.
DATABASE_URL           postgresql://... (Neon/Render). Si no existe se usa SQLite.
DATABASE_PATH          ruta del fichero SQLite (por defecto ./database.db)
SESSION_COOKIE_SECURE  1/0 (por defecto 1 en producción)
TRUST_PROXY            nº de proxies de confianza delante de la app (Render = 1)
GOOGLE_CLIENT_ID       client ID OAuth esperado en el token de Google (opcional)
GOOGLE_AUD_PREFIX      prefijo aceptado del client ID (por defecto el del
                       proyecto Firebase que ya usa login.html)
"""
from __future__ import annotations

import datetime
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("findash")

BASE_DIR = Path(__file__).resolve().parent.parent

# Prefijo numérico del proyecto de Firebase/Google que ya está en login.html
# (messagingSenderId). Los client IDs web de ese proyecto empiezan por él.
_DEFAULT_GOOGLE_AUD_PREFIX = "566983233923-"


def cargar_dotenv(ruta: Optional[Path] = None) -> None:
    """Carga ``KEY=valor`` de un fichero ``.env`` sin pisar variables ya definidas.

    Implementación mínima (sin dependencias). Ignora comentarios y líneas vacías,
    admite ``export KEY=...`` y comillas simples o dobles. Para desactivarla:
    ``FINDASH_NO_DOTENV=1``.
    """
    if os.getenv("FINDASH_NO_DOTENV", "").strip() in {"1", "true", "yes"}:
        return
    fichero = ruta or (BASE_DIR / ".env")
    try:
        lineas = fichero.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for linea in lineas:
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        if linea.startswith("export "):
            linea = linea[len("export "):].lstrip()
        clave, _, valor = linea.partition("=")
        clave, valor = clave.strip(), valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        elif " #" in valor:
            valor = valor.split(" #", 1)[0].rstrip()
        if clave and valor != "" and clave not in os.environ:
            os.environ[clave] = valor           # valor vacío = «no definida»


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def normalizar_database_url(url: Optional[str]) -> Optional[str]:
    """Render/Heroku entregan ``postgres://``; psycopg2 prefiere ``postgresql://``."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url or None


def cargar_secret_key(instance_dir: Path, es_produccion: bool) -> str:
    """Devuelve la clave secreta sin dejarla nunca escrita en el código.

    * ``SECRET_KEY`` del entorno, si existe.
    * En producción su ausencia es un error (no se arranca con una clave débil).
    * En desarrollo se genera una clave aleatoria y se guarda en
      ``instance/.secret_key`` para que las sesiones sobrevivan a reinicios.
    """
    clave = os.getenv("SECRET_KEY", "").strip()
    if clave:
        if len(clave) < 16:
            raise RuntimeError("SECRET_KEY debe tener al menos 16 caracteres.")
        return clave
    if es_produccion:
        raise RuntimeError(
            "Falta SECRET_KEY. Genera una con: python -c \"import secrets; "
            "print(secrets.token_hex(32))\" y defínela como variable de entorno."
        )
    fichero = instance_dir / ".secret_key"
    try:
        if fichero.exists():
            guardada = fichero.read_text(encoding="utf-8").strip()
            if len(guardada) >= 32:
                return guardada
        instance_dir.mkdir(parents=True, exist_ok=True)
        nueva = secrets.token_hex(32)
        fichero.write_text(nueva, encoding="utf-8")
        try:
            os.chmod(fichero, 0o600)
        except OSError:
            pass
        log.warning("SECRET_KEY no definida: generada una clave local en %s", fichero)
        return nueva
    except OSError:
        log.warning("SECRET_KEY no definida y no se puede persistir: clave efímera.")
        return secrets.token_hex(32)


def construir_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Construye el diccionario de configuración de Flask."""
    cargar_dotenv()
    entorno = (os.getenv("FINDASH_ENV") or "development").strip().lower()
    es_produccion = entorno == "production"
    instance_dir = Path(os.getenv("FINDASH_INSTANCE_DIR", str(BASE_DIR / "instance")))

    cfg: Dict[str, Any] = {
        "ENV_NAME": entorno,
        "IS_PRODUCTION": es_produccion,
        "DEBUG": env_bool("FLASK_DEBUG", False) and not es_produccion,
        "INSTANCE_DIR": str(instance_dir),
        "DATABASE_URL": normalizar_database_url(os.getenv("DATABASE_URL")),
        "DATABASE_PATH": os.getenv("DATABASE_PATH") or str(BASE_DIR / "database.db"),
        "AUTO_INIT_DB": env_bool("FINDASH_AUTO_INIT_DB", True),
        # --- sesión / cookies ---
        "PERMANENT_SESSION_LIFETIME": datetime.timedelta(days=7),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": env_bool("SESSION_COOKIE_SECURE", es_produccion),
        # 10 MB de archivo -> ~13,4 MB en Base64; margen para el JSON.
        "MAX_CONTENT_LENGTH": 16 * 1024 * 1024,
        "TRUST_PROXY": env_int("TRUST_PROXY", 0),
        # --- Google ---
        "GOOGLE_CLIENT_ID": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        "GOOGLE_AUD_PREFIX": (os.getenv("GOOGLE_AUD_PREFIX") or _DEFAULT_GOOGLE_AUD_PREFIX).strip(),
        # --- anti fuerza bruta ---
        "LOGIN_MAX_FALLOS": env_int("LOGIN_MAX_FALLOS", 5),
        "LOGIN_VENTANA_SEGUNDOS": env_int("LOGIN_VENTANA_SEGUNDOS", 15 * 60),
        "REGISTRO_MAX_POR_IP": env_int("REGISTRO_MAX_POR_IP", 8),
        "REGISTRO_VENTANA_SEGUNDOS": env_int("REGISTRO_VENTANA_SEGUNDOS", 3600),
    }
    if overrides:
        cfg.update(overrides)
    if not cfg.get("SECRET_KEY"):
        cfg["SECRET_KEY"] = cargar_secret_key(Path(cfg["INSTANCE_DIR"]), cfg["IS_PRODUCTION"])
    return cfg
