"""Estrategia 1: datos tabulares (CSV, Excel, columnas pegadas del banco).

``_intentar_tabla`` detecta el delimitador (``\\t``, ``;``, ``|``, ``,``) y
``_inferir_tabla`` descubre, sin depender de que exista cabecera, qué columna es
la fecha, el concepto, el importe (o el par Debe/Haber) y el saldo. Cuando el
extracto no trae ``+``/``-`` deduce el signo comprobando la aritmética fila a
fila contra la columna de saldo (``_consistencia``).
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from .modelos import Movimiento, ResultadoParcial
from .numeros import (
    a_ascii_minusculas, detectar_decimal, detectar_orden_fecha, limpiar_concepto,
    parse_fecha, parse_numero,
)
from .tokens import _consistencia, tipo_por_palabras

DELIMITADORES = ["\t", ";", "|", ","]

# --- léxico de cabeceras ----------------------------------------------------
_KW_FECHA = {"fecha", "date", "dia", "fec"}
_KW_CONCEPTO = {
    "concepto", "descripcion", "detalle", "detalles", "concept", "description", "details",
    "memo", "payee", "beneficiario", "observaciones", "texto", "comercio", "movimiento",
    "narrative", "operacion",
}
_KW_REF = {"referencia", "ref", "reference", "num", "numero", "id", "iban", "cuenta",
           "codigo", "oficina", "n"}
_KW_IMPORTE = {"importe", "amount", "cantidad", "monto", "valor", "quantity", "total"}
_KW_SALDO = {"saldo", "balance", "disponible"}
_KW_DEBITO = {"debe", "cargo", "cargos", "debito", "debit", "retirada", "retiradas",
              "salida", "salidas", "gasto", "gastos", "withdrawal", "withdrawals", "pagos"}
_KW_CREDITO = {"haber", "abono", "abonos", "credito", "credit", "ingreso", "ingresos",
               "entrada", "entradas", "deposito", "depositos", "deposit", "income"}
_KW_TIPO = {"tipo", "clase", "signo", "dc", "type"}
_KW_CATEGORIA = {"categoria", "category"}

_TIPO_GASTO = {"cargo", "debito", "debit", "d", "debe", "gasto", "expense", "dr", "retirada",
               "pago", "salida", "g"}
_TIPO_INGRESO = {"abono", "credito", "credit", "c", "h", "haber", "ingreso", "income", "cr",
                 "deposito", "entrada", "i"}


def _tokens(celda: str) -> set:
    return set(re.findall(r"[a-z]+", a_ascii_minusculas(celda)))


def _rol_cabecera(celda: str) -> Optional[str]:
    t = _tokens(celda)
    if not t:
        return None
    if t & _KW_SALDO:
        return "saldo"
    if t & _KW_DEBITO:
        return "debito"
    if t & _KW_CREDITO:
        return "credito"
    if "valor" in t and (t & _KW_FECHA or "f" in t):
        return "fecha_valor"
    if t & _KW_FECHA:
        return "fecha"
    if t & _KW_CATEGORIA:
        return "categoria"
    if t & _KW_TIPO:
        return "tipo"
    if t & _KW_CONCEPTO:
        return "concepto"
    if t & _KW_IMPORTE:
        return "importe"
    if t & _KW_REF:
        return "ref"
    return None


# ---------------------------------------------------------------------------
# Delimitador y lectura
# ---------------------------------------------------------------------------
def _contar_campos(linea: str, delim: str) -> int:
    try:
        return len(next(csv.reader([linea], delimiter=delim, quotechar='"', skipinitialspace=True)))
    except (csv.Error, StopIteration):
        return linea.count(delim) + 1


def _detectar_delimitador(lineas: Sequence[str]) -> Optional[str]:
    muestra = [l for l in lineas if l.strip()][:400]
    if len(muestra) < 2:
        return None
    mejor: Optional[Tuple[float, int, str]] = None
    for prioridad, delim in enumerate(DELIMITADORES):
        if not any(delim in l for l in muestra):
            continue
        cuentas = Counter(_contar_campos(l, delim) for l in muestra)
        modal, veces = cuentas.most_common(1)[0]
        if modal < 3:
            continue
        fraccion = veces / len(muestra)
        if fraccion < 0.5:
            continue
        # La coma compite con los decimales europeos: exigimos más evidencia.
        if delim == "," and fraccion < 0.7:
            continue
        puntuacion = fraccion - 0.01 * prioridad
        if mejor is None or puntuacion > mejor[0]:
            mejor = (puntuacion, modal, delim)
    return mejor[2] if mejor else None


def _leer_filas(lineas: Sequence[str], delim: str) -> List[List[str]]:
    filas: List[List[str]] = []
    for linea in lineas:
        if not linea.strip():
            continue
        try:
            fila = next(csv.reader([linea], delimiter=delim, quotechar='"', skipinitialspace=True))
        except (csv.Error, StopIteration):
            fila = linea.split(delim)
        filas.append([c.strip() for c in fila])
    return filas


def _buscar_cabecera(filas: Sequence[Optional[List[str]]]) -> Tuple[Optional[int], Dict[int, str]]:
    for i, fila in enumerate(filas[:40]):
        if fila is None:
            continue
        roles = {j: r for j, c in enumerate(fila) if c and (r := _rol_cabecera(c))}
        con_numero = sum(1 for c in fila if c and parse_numero(c) is not None and re.search(r"\d", c))
        con_fecha = sum(1 for c in fila if c and parse_fecha(c))
        if len(roles) >= 2 and con_fecha == 0 and con_numero == 0:
            return i, roles
    return None, {}


# ---------------------------------------------------------------------------
# Inferencia de columnas
# ---------------------------------------------------------------------------
class _Perfil:
    __slots__ = ("j", "n", "llenado", "frac_fecha", "frac_num", "enteros_largos", "long_media",
                 "valores", "decimal", "es_tipo")

    def __init__(self, j: int) -> None:
        self.j = j


def _perfilar(datos: List[List[str]], j: int, orden: str) -> _Perfil:
    p = _Perfil(j)
    celdas = [f[j].strip() for f in datos]
    no_vacias = [c for c in celdas if c]
    p.n = len(no_vacias)
    p.llenado = p.n / len(datos) if datos else 0.0
    p.decimal = detectar_decimal(no_vacias)
    p.frac_fecha = (sum(1 for c in no_vacias if parse_fecha(c, orden)) / p.n) if p.n else 0.0
    p.valores = [parse_numero(c, p.decimal) if not parse_fecha(c, orden) else None for c in celdas]
    p.frac_num = (sum(1 for c, v in zip(celdas, p.valores) if c and v is not None) / p.n) if p.n else 0.0
    p.enteros_largos = bool(p.n) and all(re.fullmatch(r"\d{6,}", c) for c in no_vacias)
    p.long_media = (sum(len(c) for c in no_vacias) / p.n) if p.n else 0.0
    conjunto = {a_ascii_minusculas(c) for c in no_vacias}
    p.es_tipo = bool(p.n) and len(conjunto) <= 8 and (
        sum(1 for c in no_vacias if a_ascii_minusculas(c) in (_TIPO_GASTO | _TIPO_INGRESO)) / p.n >= 0.9)
    return p


def _exclusivas(a: _Perfil, b: _Perfil) -> float:
    """Fracción de filas donde exactamente una de las dos columnas trae valor."""
    pares = list(zip(a.valores, b.valores))
    if not pares:
        return 0.0
    exacto = sum(1 for x, y in pares if (x not in (None, 0)) != (y not in (None, 0)))
    return exacto / len(pares)


def _inferir_tabla(filas: Sequence[Sequence[str]]) -> Optional[ResultadoParcial]:
    filas = [list(f) for f in filas if any(c.strip() for c in f)]
    if len(filas) < 2:
        return None
    ncols = Counter(len(f) for f in filas).most_common(1)[0][0]
    if ncols < 3:
        return None
    normalizadas: List[Optional[List[str]]] = [
        (f + [""] * (ncols - len(f))) if len(f) <= ncols else None for f in filas]
    idx_cab, roles = _buscar_cabecera(normalizadas)
    inicio = idx_cab + 1 if idx_cab is not None else 0
    datos_todos = [f for f in normalizadas[inicio:] if f is not None]
    if not datos_todos:
        return None
    avisos: List[str] = []

    # Orden de fechas global
    orden = detectar_orden_fecha([c for f in datos_todos for c in f if re.match(r"\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d", c)])

    perfiles = [_perfilar(datos_todos, j, orden) for j in range(ncols)]

    # --- columna de fecha ---
    cand_fecha = [p for p in perfiles if p.frac_fecha >= 0.6 and p.llenado >= 0.6]
    if not cand_fecha:
        return None
    por_rol = [p for p in cand_fecha if roles.get(p.j) == "fecha"]
    jf = (por_rol or [p for p in cand_fecha if roles.get(p.j) != "fecha_valor"] or cand_fecha)[0].j

    # Solo filas con fecha válida son "candidatas" a movimiento.
    datos = [f for f in datos_todos if parse_fecha(f[jf], orden)]
    if not datos:
        return None
    perfiles = [_perfilar(datos, j, orden) for j in range(ncols)]
    perf = {p.j: p for p in perfiles}

    # --- columnas numéricas ---
    numericas = [
        p for p in perfiles
        if p.j != jf and p.frac_num >= 0.6 and p.n >= 1
        and not p.enteros_largos and roles.get(p.j) not in ("ref", "fecha_valor")
        and not (p.frac_fecha >= 0.6)
    ]

    jd = jc = ji = js = None   # debe, haber, importe único, saldo
    par_inferido = False
    for p in numericas:
        r = roles.get(p.j)
        if r == "debito" and jd is None:
            jd = p.j
        elif r == "credito" and jc is None:
            jc = p.j
        elif r == "saldo" and js is None:
            js = p.j
        elif r == "importe" and ji is None:
            ji = p.j

    resto = [p for p in numericas if p.j not in (jd, jc, js, ji)]

    if jd is None and jc is None and ji is None:
        # Sin roles útiles en cabecera: inferimos por contenido.
        pendientes = list(resto)
        # ¿Par Debe/Haber? Dos columnas poco pobladas y mutuamente excluyentes.
        mejor_par = None
        for a in range(len(pendientes)):
            for b in range(a + 1, len(pendientes)):
                pa, pb = pendientes[a], pendientes[b]
                if pa.llenado < 0.95 and pb.llenado < 0.95 and _exclusivas(pa, pb) >= 0.9:
                    mejor_par = (pa.j, pb.j)
                    break
            if mejor_par:
                break
        if mejor_par:
            jd, jc = mejor_par
            par_inferido = True
            pendientes = [p for p in pendientes if p.j not in mejor_par]
            if js is None and pendientes:
                js = max(pendientes, key=lambda p: p.llenado).j
        elif js is not None:
            cands = [p for p in pendientes]
            if cands:
                ji = cands[0].j
        elif len(pendientes) == 1:
            ji = pendientes[0].j
        elif len(pendientes) >= 2:
            # (importe, saldo): el par cuya aritmética cuadra
            mejor = (0.0, None, None)
            for pa in pendientes:
                for pb in pendientes:
                    if pa.j == pb.j:
                        continue
                    sub = [(abs(x), y) for x, y in zip(pa.valores, pb.valores) if x is not None and y is not None]
                    if len(sub) < 2:
                        continue
                    _, ratio, _o = _consistencia([s[0] for s in sub], [s[1] for s in sub])
                    if ratio > mejor[0]:
                        mejor = (ratio, pa.j, pb.j)
            if mejor[1] is not None and mejor[0] >= 0.8:
                ji, js = mejor[1], mejor[2]
            else:
                ji = pendientes[0].j
                avisos.append("Hay varias columnas numéricas y no se pudo identificar el saldo; se usa la primera como importe.")
    elif ji is None and (jd is None or jc is None):
        # Solo uno de debe/haber en cabecera: lo tratamos como importe único.
        ji = jd if jd is not None else jc
        jd = jc = None
    if js is None and (ji is not None or (jd is not None and jc is not None)):
        usados = {x for x in (jd, jc, ji) if x is not None}
        for p in resto:
            if p.j not in usados and roles.get(p.j) in (None, "saldo"):
                js = p.j
                break

    if jd is None and jc is None and ji is None:
        return None

    # --- columnas de tipo, concepto y categoría ---
    jt = next((p.j for p in perfiles if p.es_tipo and p.j not in (jf, jd, jc, ji, js)), None)
    usadas = {x for x in (jf, jd, jc, ji, js, jt) if x is not None}
    jcat = next((j for j, r in roles.items() if r == "categoria" and j not in usadas), None)
    if jcat is not None:
        usadas.add(jcat)
    texto_cols = [p for p in perfiles if p.j not in usadas and p.frac_num < 0.6
                  and p.frac_fecha < 0.6 and p.n > 0 and roles.get(p.j) not in ("ref", "fecha_valor")]
    conceptos = [p for p in texto_cols if roles.get(p.j) == "concepto"]
    if not conceptos:
        conceptos = sorted(texto_cols, key=lambda p: p.long_media, reverse=True)[:1]
        extra = [p for p in sorted(texto_cols, key=lambda p: p.long_media, reverse=True)[1:2] if p.long_media >= 12]
        conceptos += extra
    conceptos = conceptos[:2]
    if not conceptos:
        return None
    conceptos.sort(key=lambda p: p.j)

    # --- construcción de filas ---
    brutas = []   # [fecha, concepto, valor_abs, signo|None, saldo, categoria, fila_original]
    dec_imp = perf[ji].decimal if ji is not None else None
    for f in datos:
        fecha = parse_fecha(f[jf], orden)
        piezas = []
        for p in conceptos:
            t = limpiar_concepto(f[p.j])
            if t and t not in piezas:
                piezas.append(t)
        concepto = limpiar_concepto(" - ".join(piezas)) or "(sin concepto)"
        brutas.append([fecha, concepto, None, None, None, None, f])
    # Los perfiles guardan los valores numéricos en el mismo orden que ``datos``.
    for i, fila in enumerate(brutas):
        f = fila[6]
        if ji is not None:
            v = perf[ji].valores[i]
            if v is None:
                fila[2] = None
            else:
                fila[2] = abs(v)
                crudo = f[ji].strip()
                fila[3] = -1 if v < 0 else (1 if crudo.startswith("+") else None)
        else:
            vd = perf[jd].valores[i] if jd is not None else None
            vc = perf[jc].valores[i] if jc is not None else None
            if vd not in (None, 0):
                fila[2], fila[3] = abs(vd), -1
            elif vc not in (None, 0):
                fila[2], fila[3] = abs(vc), 1
        if js is not None:
            fila[4] = perf[js].valores[i]
        if jcat is not None:
            fila[5] = f[jcat].strip() or None
        if jt is not None and fila[3] is None:
            marca = a_ascii_minusculas(f[jt].strip())
            if marca in _TIPO_GASTO:
                fila[3] = -1
            elif marca in _TIPO_INGRESO:
                fila[3] = 1

    validas = [fila for fila in brutas if fila[2] is not None and fila[2] > 0]
    descartadas = len(brutas) - len(validas)
    if not validas:
        return None

    # --- debe/haber inferido por contenido: ¿está el orden invertido? ---
    saldo_verificado = False
    if par_inferido and js is not None:
        deducidos, _ratio, _o = _consistencia([f[2] for f in validas], [f[4] for f in validas])
        coinciden = sum(1 for f, d in zip(validas, deducidos) if d is not None and f[3] == d)
        opuestos = sum(1 for f, d in zip(validas, deducidos) if d is not None and f[3] == -d)
        if opuestos > coinciden:
            for fila in validas:
                if fila[3] is not None:
                    fila[3] = -fila[3]
            avisos.append("Se ha invertido el orden Debe/Haber porque los saldos lo indican.")

    # --- signos que faltan ---
    sin_signo = [fila for fila in validas if fila[3] is None]
    hay_negativos = any(fila[3] == -1 for fila in validas) and ji is not None
    if sin_signo:
        if hay_negativos:
            # Convención "gastos en negativo": lo que no lleva signo es un ingreso.
            for fila in sin_signo:
                fila[3] = 1
        elif js is not None:
            valores = [fila[2] for fila in validas]
            saldos = [fila[4] for fila in validas]
            deducidos, ratio, _orden = _consistencia(valores, saldos)
            if ratio >= 0.8:
                saldo_verificado = True
                for fila, d in zip(validas, deducidos):
                    if fila[3] is None and d is not None:
                        fila[3] = d
            else:
                avisos.append("La columna de saldo no cuadra con los importes; no se usa para deducir el signo.")
    elif js is not None and len(validas) > 1:
        valores = [fila[2] * fila[3] for fila in validas]
        saldos = [fila[4] for fila in validas]
        if all(s is not None for s in saldos):
            ok_a = sum(abs((saldos[i] - saldos[i - 1]) - valores[i]) <= 0.011 for i in range(1, len(valores)))
            ok_d = sum(abs((saldos[i] - saldos[i + 1]) - valores[i]) <= 0.011 for i in range(len(valores) - 1))
            saldo_verificado = max(ok_a, ok_d) / (len(valores) - 1) >= 0.8

    movimientos: List[Movimiento] = []
    adivinados = 0
    for fecha, concepto, valor, signo, saldo, categoria, _f in validas:
        adivinado = False
        if signo is None:
            tipo = tipo_por_palabras(concepto)
            if tipo is None:
                tipo = "gasto"
            adivinado = True
            adivinados += 1
        else:
            tipo = "ingreso" if signo > 0 else "gasto"
        m = Movimiento(fecha, concepto, valor, tipo, saldo, tipo_adivinado=adivinado)
        m.categoria = categoria
        movimientos.append(m)
    if adivinados:
        avisos.append(
            f"{adivinados} movimiento(s) no traían signo ni saldo utilizable: se han clasificado por su "
            "concepto (por defecto, gasto). Revísalos antes de confirmar.")

    return ResultadoParcial(
        movimientos=movimientos,
        estrategia="tabla",
        descartadas=descartadas,
        avisos=avisos,
        confianza_estructura=0.95 if idx_cab is not None else 0.85,
        saldo_verificado=saldo_verificado,
        candidatas=len(datos),
    )


def _intentar_tabla(lineas: Sequence[str]) -> Optional[ResultadoParcial]:
    """Punto de entrada: detecta delimitador, lee filas e infiere la estructura."""
    delim = _detectar_delimitador(lineas)
    if delim is None:
        return None
    return _inferir_tabla(_leer_filas(lineas, delim))
