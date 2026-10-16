"""Suite de pruebas de FinDash (unittest, sin dependencias externas).

    python -m unittest discover -s tests -t . -v

Para ejecutarla contra PostgreSQL define ``FINDASH_TEST_DATABASE_URL``
(p. ej. ``postgresql://usuario:clave@localhost:5432/findash_test``). **Se vacían
las tablas de esa base de datos**: usa una base solo para pruebas.
"""
import logging
import os
import sys

os.environ.setdefault("FINDASH_NO_DOTENV", "1")        # un .env local no debe alterar las pruebas
logging.getLogger("findash").setLevel(logging.ERROR)   # los avisos esperados no ensucian la salida

if os.environ.get("FINDASH_TEST_DATABASE_URL"):
    try:
        import psycopg2  # noqa: F401
    except ImportError:                      # entorno sin psycopg2: driver de pruebas sobre libpq
        from tests import fake_psycopg2
        fake_psycopg2.instalar()
