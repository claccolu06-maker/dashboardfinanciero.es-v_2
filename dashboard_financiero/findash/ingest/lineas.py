"""Estrategias 2 y 3: movimientos repartidos en varias líneas y fallback difuso.

* ``_estrategia_lineas``: máquina de estados para operaciones que ocupan
  varias líneas consecutivas. Dos modos:

  - **modo A**: la fecha abre la operación; concepto e importes van detrás.
  - **modo C**: el concepto va primero; la fecha y los importes le siguen.

* ``_estrategia_difusa``: último recurso para PDFs desestructurados; aplana el
  texto, ancla fechas e importes por proximidad y extrae los conceptos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .modelos import Movimiento, ResultadoParcial
from .numeros import (
    ImporteTok, buscar_fechas, buscar_importes, detectar_orden_fecha, limpiar_concepto,
)
from .tokens import (
    Linea, _consistencia, es_linea_resumen, es_ruido, tipo_por_palabras, tokenizar_linea,
)


@dataclass
class _Bloque:
    fecha: "object"
    textos: List[str] = field(default_factory=list)
    importes: List[ImporteTok] = field(default_factory=list)

    def agregar(self, linea: Linea) -> None:
        if linea.texto:
            self.textos.append(linea.texto)
        self.importes.extend(linea.importes)


def _orden_global(lineas: Sequence[str]) -> str:
    candidatos: List[str] = []
    for l in lineas:
        candidatos.extend(re.findall(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}", l))
    return detectar_orden_fecha(candidatos)


def _bloques_a_resultado(bloques: Sequence[_Bloque], estrategia: str, confianza: float,
                         descartadas_extra: int = 0) -> Optional[ResultadoParcial]:
    avisos: List[str] = []
    filas = []   # (fecha, concepto, valor, signo, saldo)
    descartadas = descartadas_extra
    exceso = 0
    for b in bloques:
        concepto = limpiar_concepto(" ".join(b.textos))
        if not b.importes or not re.search(r"[A-Za-zÀ-ÿ]", concepto):
            descartadas += 1
            continue
        if len(b.importes) == 1:
            imp, saldo = b.importes[0], None
        else:
            imp, saldo = b.importes[0], b.importes[1]
            if len(b.importes) > 2:
                exceso += 1
                descartadas += 1       # dudosa: cuenta en contra de la calidad
        saldo_val = None
        if saldo is not None:
            saldo_val = -saldo.valor if saldo.signo == -1 else saldo.valor
        filas.append([b.fecha, concepto, imp.valor, imp.signo, saldo_val])
    if not filas:
        return None
    if exceso:
        avisos.append(f"{exceso} operación(es) tenían más de dos importes; se usaron los dos primeros (importe y saldo).")

    saldo_verificado = False
    if any(f[3] is None for f in filas):
        deducidos, ratio, _o = _consistencia([f[2] for f in filas], [f[4] for f in filas])
        if ratio >= 0.8:
            saldo_verificado = True
            for f, d in zip(filas, deducidos):
                if f[3] is None and d is not None:
                    f[3] = d

    movimientos: List[Movimiento] = []
    adivinados = 0
    for fecha, concepto, valor, signo, saldo in filas:
        adivinado = False
        if signo is None:
            tipo = tipo_por_palabras(concepto) or "gasto"
            adivinado = True
            adivinados += 1
        else:
            tipo = "ingreso" if signo > 0 else "gasto"
        movimientos.append(Movimiento(fecha, concepto, valor, tipo, saldo, tipo_adivinado=adivinado))
    if adivinados:
        avisos.append(
            f"En {adivinados} movimiento(s) el archivo no indica si es ingreso o gasto y los saldos no permiten "
            "deducirlo: se han clasificado por su concepto. Revisa la columna «Tipo» antes de importar "
            "(también puedes corregirlos después con el lápiz).")
    return ResultadoParcial(
        movimientos=movimientos, estrategia=estrategia, descartadas=descartadas, avisos=avisos,
        confianza_estructura=confianza, saldo_verificado=saldo_verificado,
        candidatas=len(movimientos) + descartadas,
    )


def _es_descartable(linea: Linea) -> bool:
    return es_ruido(linea.original) or es_linea_resumen(linea.texto, bool(linea.fechas))


def _modo_a(lineas: Sequence[Linea]) -> List[_Bloque]:
    """La fecha abre la operación."""
    bloques: List[_Bloque] = []
    actual: Optional[_Bloque] = None
    for L in lineas:
        if _es_descartable(L):
            continue
        if L.fechas:
            if actual is not None and not actual.importes and not actual.textos and L.solo_fecha:
                continue                      # segunda fecha (fecha valor)
            actual = _Bloque(L.fechas[0].fecha)
            bloques.append(actual)
            actual.agregar(L)
        elif actual is not None:
            actual.agregar(L)
    return bloques


def _modo_c(lineas: Sequence[Linea]) -> List[_Bloque]:
    """El concepto precede a la fecha y a los importes."""
    bloques: List[_Bloque] = []
    pendiente: List[str] = []
    actual: Optional[_Bloque] = None
    for L in lineas:
        if _es_descartable(L):
            continue
        if L.fechas:
            if actual is not None and not actual.importes and L.solo_fecha:
                continue                      # fecha valor tras la fecha de operación
            actual = _Bloque(L.fechas[0].fecha, textos=pendiente[-3:] + ([L.texto] if L.texto else []),
                             importes=list(L.importes))
            pendiente = []
            bloques.append(actual)
            if len(actual.importes) >= 2:
                actual = None
        elif actual is not None and L.importes:
            actual.agregar(L)
            if len(actual.importes) >= 2:
                actual = None
        elif actual is not None:
            # texto suelto con la operación a medias
            if actual.importes:
                actual = None
                pendiente = [L.texto]
            else:
                actual.textos.append(L.texto)
        else:
            if L.texto:
                pendiente.append(L.texto)
            if L.importes and bloques:
                pass                          # importes huérfanos: se ignoran
    return bloques


def _estrategia_lineas(lineas: Sequence[str]) -> List[ResultadoParcial]:
    """Devuelve los resultados de los modos A y C (el motor elige el mejor)."""
    orden = _orden_global(lineas)
    tokens = [tokenizar_linea(i, l, orden) for i, l in enumerate(lineas)]
    resultados: List[ResultadoParcial] = []
    for nombre, funcion in (("lineas-A", _modo_a), ("lineas-C", _modo_c)):
        res = _bloques_a_resultado(funcion(tokens), nombre, 0.7)
        if res is not None:
            resultados.append(res)
    return resultados


# ---------------------------------------------------------------------------
# Estrategia 3: difusa
# ---------------------------------------------------------------------------
def _estrategia_difusa(lineas: Sequence[str]) -> Optional[ResultadoParcial]:
    plano = re.sub(r"\s+", " ", " ".join(l for l in lineas if not es_ruido(l))).strip()
    if not plano:
        return None
    orden = _orden_global(lineas)
    fechas = buscar_fechas(plano, orden)
    # Dos fechas pegadas (operación + valor) son una sola operación.
    depuradas = []
    for f in fechas:
        if depuradas and re.fullmatch(r"[\s\-–/|]*", plano[depuradas[-1].fin:f.inicio]):
            continue
        depuradas.append(f)
    fechas = depuradas
    if not fechas:
        return None
    # Los importes se buscan con las fechas "tapadas".
    piezas = list(plano)
    for f in fechas:
        for i in range(f.inicio, f.fin):
            piezas[i] = " "
    importes = buscar_importes("".join(piezas))
    if not importes:
        return None

    def texto_entre(a: int, b: int) -> str:
        huecos = sorted([(f.inicio, f.fin) for f in fechas if f.inicio >= a and f.fin <= b] +
                        [(i.inicio, i.fin) for i in importes if i.inicio >= a and i.fin <= b])
        trozos, cursor = [], a
        for x, y in huecos:
            if x > cursor:
                trozos.append(plano[cursor:x])
            cursor = max(cursor, y)
        trozos.append(plano[cursor:b])
        resto = re.sub(r"(?i)(?<![A-Za-z])(?:€|eur(?:os)?)(?![A-Za-z])", " ", " ".join(trozos))
        return limpiar_concepto(resto)

    regiones = []
    for i, f in enumerate(fechas):
        fin = fechas[i + 1].inicio if i + 1 < len(fechas) else len(plano)
        mios = [im for im in importes if f.fin <= im.inicio < fin]
        antes = fechas[i - 1].fin if i else 0
        regiones.append((f, f.fin, fin, antes, mios))

    # ¿El concepto va antes o después de la fecha? Votación sobre las regiones.
    texto_antes = texto_despues = 0
    for f, ini, fin, antes, mios in regiones:
        if mios:
            a = texto_entre(ini, mios[0].inicio)
            b = texto_entre(mios[-1].fin, fin)
            texto_antes += bool(re.search(r"[A-Za-zÀ-ÿ]{3}", a))
            texto_despues += bool(re.search(r"[A-Za-zÀ-ÿ]{3}", b))
    concepto_primero = texto_despues > texto_antes

    bloques: List[_Bloque] = []
    for k, (f, ini, fin, antes, mios) in enumerate(regiones):
        if not mios:
            continue
        mios = mios[:2]
        if concepto_primero:
            previo_fin = f.inicio
            previo_ini = regiones[k - 1][2] if k else 0
            # el concepto de esta operación es el texto que sigue a los importes de la anterior
            if k:
                prev_mios = regiones[k - 1][4]
                previo_ini = prev_mios[-1].fin if prev_mios else regiones[k - 1][1]
            else:
                previo_ini = max(0, f.inicio - 90)
            concepto = texto_entre(previo_ini, previo_fin)
        else:
            concepto = limpiar_concepto(texto_entre(ini, mios[0].inicio) + " " + texto_entre(mios[-1].fin, fin))
        b = _Bloque(f.fecha, textos=[concepto] if concepto else [], importes=list(mios))
        bloques.append(b)
    res = _bloques_a_resultado(bloques, "difusa", 0.5)
    if res is not None:
        res.avisos.insert(0, "Se ha usado el análisis difuso (texto desestructurado): revisa los resultados.")
    return res
