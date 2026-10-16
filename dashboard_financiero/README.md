# FinDash

Dashboard de finanzas personales en Flask (dashboardfinanciero.es). Funciona igual en
**SQLite** (local, PythonAnywhere) y en **PostgreSQL** (Neon, Render Postgres): si defines
`DATABASE_URL` usa PostgreSQL; si no, un `database.db` local.

## Estructura

```
app.py / wsgi.py          Puntos de entrada (gunicorn, flask run, PythonAnywhere)
findash/
  __init__.py             create_app(): configuración, seguridad, blueprints
  config.py               Todo sale de variables de entorno (+ .env opcional)
  db.py                   Capa unificada SQLite/PostgreSQL, esquema y migraciones ligeras
  security.py             CSRF, cabeceras, rate limiting, no-store, manejo de errores
  validators.py           Validación de entradas (fechas, importes, e-mail, contraseña)
  categorias.py           Categorizador + reglas aprendidas por usuario
  migracion.py            Copia SQLite -> otra base (la usa scripts/)
  ingest/                 Motor de importación (CSV, Excel, PDF, texto pegado)
  services/finance.py     Periodos, KPIs, runway, proyección, recurrentes, Wrapped, analítica
  services/demo.py        Datos de demostración (Modo Despegue)
  blueprints/             main · auth · panel · movimientos · importar · ajustes
templates/ static/        Jinja2, CSS, JS, PWA (sw.js, manifest, iconos)
scripts/                  migrate_sqlite_to_postgres.py
tests/                    180 pruebas (unittest), también contra PostgreSQL
```

## Arranque en local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # opcional; en desarrollo no hace falta nada
flask --app app run --debug     # http://127.0.0.1:5000
```

En desarrollo, si no defines `SECRET_KEY`, se genera una y se guarda en `instance/.secret_key`.
En producción **la aplicación se niega a arrancar sin `SECRET_KEY`**.

## Variables de entorno

| Variable | Descripción |
|---|---|
| `FINDASH_ENV` | `development` (por defecto) o `production` |
| `SECRET_KEY` | Obligatoria en producción (≥16 caracteres; `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `DATABASE_URL` | `postgresql://…` (acepta `postgres://`). Vacía = SQLite |
| `DATABASE_PATH` | Ruta del SQLite (por defecto `./database.db`) |
| `TRUST_PROXY` | Nº de proxies de confianza (Render = 1) para leer la IP real |
| `SESSION_COOKIE_SECURE` | `1`/`0`; por defecto `1` en producción |
| `GOOGLE_CLIENT_ID` / `GOOGLE_AUD_PREFIX` | Validación del token de Google (ver más abajo) |
| `LOGIN_MAX_FALLOS`, `LOGIN_VENTANA_SEGUNDOS`, `REGISTRO_MAX_POR_IP`, `REGISTRO_VENTANA_SEGUNDOS` | Anti fuerza bruta |

Un `.env` en la raíz se carga automáticamente sin pisar las variables ya definidas
(`FINDASH_NO_DOTENV=1` lo desactiva). Las claves vacías se tratan como «no definidas».

## Despliegue

### PythonAnywhere (SQLite)

1. Sube el proyecto (p. ej. `/home/USUARIO/findash`) y crea un virtualenv: `pip install -r requirements.txt`
   (si no vas a usar PostgreSQL puedes omitir `psycopg2-binary`).
2. Pestaña **Web** → archivo WSGI:
   ```python
   import os, sys
   ruta = "/home/USUARIO/findash"
   if ruta not in sys.path:
       sys.path.insert(0, ruta)
   os.environ["FINDASH_ENV"] = "production"
   os.environ["SECRET_KEY"] = "…tu clave…"
   from wsgi import application
   ```
   (alternativa: un `.env` en la raíz con `chmod 600`).
3. **Static files**: URL `/static/` → `/home/USUARIO/findash/static`.
4. Reload. La primera petición crea/actualiza las tablas; no se borra ningún dato.

> Las cuentas gratuitas de PythonAnywhere solo pueden salir a Internet hacia una lista de sitios;
> comprueba que el login con Google y, si usas Neon, tu host de base de datos son alcanzables.

### Render + Neon (PostgreSQL)

1. En Neon crea el proyecto y copia la cadena de conexión (mejor la del host `-pooler`).
2. En Render, **Web Service**: Build `pip install -r requirements.txt`, Start
   `gunicorn app:app --workers 2 --timeout 60`, Health check `/healthz`.
3. Variables: `FINDASH_ENV=production`, `SECRET_KEY`, `DATABASE_URL`, `TRUST_PROXY=1`.
4. Si ya tienes usuarios en SQLite, migra **antes** de abrir el tráfico (siguiente apartado).

## Migrar SQLite → PostgreSQL

```bash
cp database.db database.copia.db                      # copia de seguridad
export DATABASE_URL="postgresql://…?sslmode=require"
python scripts/migrate_sqlite_to_postgres.py database.copia.db --simulacro   # no escribe nada
python scripts/migrate_sqlite_to_postgres.py database.copia.db               # migra
```

Conserva los `id`, reajusta las secuencias, corre en **una sola transacción** y se niega a mezclar con
un destino que ya tenga usuarios. Lo que no puede copiar sin romper restricciones (filas huérfanas,
tipo ≠ ingreso/gasto…) lo omite **y lo cuenta** en el informe; los e-mails se normalizan a minúsculas
(el login siempre busca en minúsculas). Tablas ajenas a FinDash (p. ej. `locales`) no se migran.

## Tests

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .                      # SQLite
FINDASH_TEST_DATABASE_URL=postgresql://u:p@localhost/findash_test \
    python -m unittest discover -s tests -t .                  # PostgreSQL (¡vacía esas tablas!)
```

Cubren, entre otras cosas: CSRF en **todas** las rutas POST (recorren `app.url_map`), aislamiento entre
usuarios (IDOR), rate limiting, validaciones, runway, proyección, recurrentes idempotentes, Wrapped,
metas, aprendizaje de categorías, importación (vista previa, confirmación, duplicados, límites,
idempotencia) y la capa de base de datos en ambos motores.

## Seguridad (resumen)

* CSRF central en `before_request` (campo `csrf_token` o cabecera `X-CSRFToken`) para todo POST/PUT/PATCH/DELETE.
* Contraseñas con hash de Werkzeug, mínimo 8 caracteres con letras y números; login con límite de intentos
  por cuenta+IP (persistido en la base de datos, funciona con varios procesos).
* Login con Google: se valida `aud` (`GOOGLE_CLIENT_ID`, o el prefijo del proyecto Firebase), `iss`, `exp`
  y `email_verified`.
* Cierre de sesión por POST, cabeceras de seguridad, `Cache-Control: no-store` en páginas autenticadas,
  redirecciones `next` solo a rutas internas, protección frente a inyección de fórmulas en el CSV exportado.
* Todas las escrituras van en transacción con rollback automático; la conexión se cierra siempre al final
  de la petición.

## Motor de importación

`findash/ingest/` es una implementación **nueva** (estrategias «francotirador», tabla, líneas y difusa,
con selección por calidad, deduplicación y límites de tamaño). Si recuperas tu parser original, sustituye
`francotirador.py`, `tabla.py` y `lineas.py` manteniendo la firma `f(lineas) -> ResultadoParcial`
(`findash/ingest/modelos.py`) y el resto de la aplicación seguirá funcionando.
