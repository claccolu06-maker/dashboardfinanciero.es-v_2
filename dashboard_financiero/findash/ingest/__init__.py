"""Motor de ingesta de extractos bancarios (CSV, Excel, PDF y texto pegado).

Puntos de entrada principales:

* :func:`reconstruir_movimientos_detallado` -- texto -> movimientos + diagnóstico.
* :func:`analizar_archivo`                  -- bytes de CSV/XLSX/PDF -> movimientos.
* :func:`procesar_importacion`              -- deduplica e inserta en la base de datos.

Nota: este paquete es una implementación **nueva** escrita a partir de la
descripción funcional del motor original (que no venía en el .zip). Si más
adelante recuperas tu parser original, puedes sustituir ``francotirador.py``,
``tabla.py`` y ``lineas.py`` manteniendo la firma ``f(lineas) -> ResultadoParcial``
y el resto de la aplicación seguirá funcionando.
"""
from .limites import (  # noqa: F401
    ImportacionError, MAX_BYTES, MAX_LINEAS, MAX_MOVIMIENTOS, MAX_PAGINAS_PDF,
    VENTANA_IDEMPOTENCIA_SEGUNDOS,
)
from .modelos import Movimiento, ResultadoParcial, ResultadoParseo  # noqa: F401
from .motor import (  # noqa: F401
    analizar_archivo, reconstruir_movimientos, reconstruir_movimientos_detallado,
)
