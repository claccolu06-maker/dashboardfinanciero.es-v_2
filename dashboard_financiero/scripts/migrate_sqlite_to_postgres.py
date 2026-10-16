#!/usr/bin/env python3
"""Migra una base SQLite de FinDash a PostgreSQL (Neon, Render Postgres...).

Uso:
    export DATABASE_URL="postgresql://usuario:clave@host/db?sslmode=require"
    python scripts/migrate_sqlite_to_postgres.py database.db --simulacro   # prueba, no escribe
    python scripts/migrate_sqlite_to_postgres.py database.db               # migra de verdad

También se puede pasar la URL con --url. El destino debe estar vacío (sin usuarios).
Antes de migrar PRODUCCIÓN haz una copia de seguridad del SQLite.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findash.config import normalizar_database_url  # noqa: E402
from findash.db import connect  # noqa: E402
from findash.migracion import MigracionError, migrar  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sqlite", help="ruta del database.db de origen")
    ap.add_argument("--url", default=os.getenv("DATABASE_URL"), help="URL de PostgreSQL (por defecto $DATABASE_URL)")
    ap.add_argument("--simulacro", action="store_true", help="hace todo y revierte al final; no escribe nada")
    args = ap.parse_args()

    url = normalizar_database_url(args.url)
    if not url:
        print("ERROR: define DATABASE_URL o pasa --url.", file=sys.stderr)
        return 2
    if not os.path.isfile(args.sqlite):
        print(f"ERROR: no existe {args.sqlite}", file=sys.stderr)
        return 2

    destino = connect({"DATABASE_URL": url})
    try:
        informe = migrar(args.sqlite, destino, simulacro=args.simulacro)
    except MigracionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        destino.close()
    print(informe.texto())
    if informe.simulacro:
        print("\nTodo correcto. Ejecuta de nuevo sin --simulacro para migrar de verdad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
