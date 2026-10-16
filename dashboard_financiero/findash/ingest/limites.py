"""Límites de seguridad de la ingesta y error de dominio."""

MAX_BYTES = 10 * 1024 * 1024          # tamaño máximo de un archivo subido
MAX_LINEAS = 20_000                   # líneas de texto analizables
MAX_MOVIMIENTOS = 5_000               # movimientos por importación
MAX_PAGINAS_PDF = 80
MAX_XLSX_DESCOMPRIMIDO = 100 * 1024 * 1024   # protección contra zip bombs
MAX_XLSX_ENTRADAS = 2_000
MAX_RATIO_COMPRESION = 200            # >200x en un fichero grande = sospechoso
VENTANA_IDEMPOTENCIA_SEGUNDOS = 600   # un mismo token 'clave' no se procesa dos veces


class ImportacionError(Exception):
    """Error con mensaje apto para mostrar tal cual al usuario."""
