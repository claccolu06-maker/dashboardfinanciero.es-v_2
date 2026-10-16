"""Catálogo de categorías y auto-categorizador heurístico.

``categorizar_movimiento`` normaliza el concepto a ASCII/minúsculas y lo compara
con un diccionario de palabras clave por categoría. Antes de eso consulta las
*reglas aprendidas* del usuario (``reglas_categoria``): si corrigió a mano la
categoría de un comercio, esa decisión manda en futuras importaciones.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .ingest.numeros import a_ascii_minusculas

# ---------------------------------------------------------------------------
# Catálogo estándar
# ---------------------------------------------------------------------------
CATEGORIAS_INGRESO: List[str] = ["Nómina y Salario", "Transferencias", "Otros Ingresos"]

CATEGORIAS_GASTO: List[str] = [
    "Alimentación",
    "Restaurantes y Comida",
    "Ocio y Tiempo Libre",
    "Suscripciones Digitales",
    "Servicios del Hogar",
    "Vivienda",
    "Transporte Público",
    "Vehículo y Gasolina",
    "Compras y Ropa",
    "Salud y Bienestar",
    "Viajes y Vacaciones",
    "Educación",
    "Gastos Generales",
]

CATEGORIA_GASTO_DEFECTO = "Gastos Generales"
CATEGORIA_INGRESO_DEFECTO = "Otros Ingresos"

# Categorías de versiones anteriores de la app. Se conservan tal cual en los
# movimientos existentes; aparecen como "personalizadas" en ajustes/formularios.
CATEGORIAS_LEGACY: List[str] = ["Transporte", "Ocio", "Hogar", "Salud", "Servicios", "Otros"]

ICONOS: Dict[str, str] = {
    "Alimentación": "ph-shopping-cart",
    "Restaurantes y Comida": "ph-fork-knife",
    "Ocio y Tiempo Libre": "ph-game-controller",
    "Suscripciones Digitales": "ph-television-simple",
    "Servicios del Hogar": "ph-lightbulb",
    "Vivienda": "ph-house",
    "Transporte Público": "ph-train",
    "Vehículo y Gasolina": "ph-gas-pump",
    "Compras y Ropa": "ph-shopping-bag",
    "Salud y Bienestar": "ph-first-aid",
    "Viajes y Vacaciones": "ph-airplane-tilt",
    "Educación": "ph-books",
    "Gastos Generales": "ph-package",
    "Nómina y Salario": "ph-briefcase",
    "Transferencias": "ph-arrows-left-right",
    "Otros Ingresos": "ph-coins",
}


def todas_las_categorias(usadas: Iterable[str] = ()) -> List[str]:
    """Catálogo de gasto + categorías propias que el usuario ya haya utilizado."""
    resultado = list(CATEGORIAS_GASTO)
    for cat in usadas:
        if cat and cat not in resultado and cat not in CATEGORIAS_INGRESO:
            resultado.append(cat)
    return resultado


# ---------------------------------------------------------------------------
# Diccionario heurístico (palabras ya normalizadas: ASCII y minúsculas).
# Un '*' al final significa "empieza por" (restaurante*, farmacia*...).
# El ORDEN importa: de lo más específico a lo más genérico.
# ---------------------------------------------------------------------------
_REGLAS_GASTO: List[Tuple[str, Sequence[str]]] = [
    ("Suscripciones Digitales", [
        "netflix", "spotify", "hbo", "hbo max", "max com", "disney", "disney plus", "prime video",
        "amazon prime", "youtube premium", "youtube music", "apple com bill", "itunes",
        "icloud", "google one", "google storage", "dazn", "filmin", "movistar plus",
        "adobe", "microsoft 365", "office 365", "chatgpt", "openai", "anthropic", "claude ai",
        "notion", "dropbox", "canva", "github", "twitch", "patreon", "crunchyroll", "audible",
        "kindle unlimited", "paramount", "apple tv", "playstation plus", "ps plus",
        "xbox game pass", "game pass", "nintendo online", "suscripcion*", "subscription*",
        "tidal", "deezer", "skyshowtime", "rakuten tv", "duolingo", "linkedin premium",
        "nordvpn", "expressvpn", "1password", "lastpass", "proton",
    ]),
    ("Restaurantes y Comida", [
        "glovo", "uber eats", "ubereats", "just eat", "justeat", "deliveroo", "mcdonald*",
        "mc donald*", "burger king", "kfc", "telepizza", "domino*", "pizzeria*", "pizza*",
        "restaurante*", "restaurant*", "cafeteria*", "cafe", "starbucks", "taberna*",
        "tapas", "sushi", "kebab", "foodoo", "100 montaditos", "montaditos", "vips", "foster*",
        "la tagliatella", "tagliatella", "bar", "brasserie", "hamburgueseria*", "heladeria*",
        "pasteleria*", "bocateria*", "cerveceria*", "asador*", "chiringuito*", "comida",
        "five guys", "taco bell", "wok", "ramen", "poke", "dunkin*", "tim hortons",
        "rodilla", "panaria", "paradis", "lateral", "ginos", "tgb", "udon",
    ]),
    ("Alimentación", [
        "mercadona", "carrefour", "lidl", "aldi", "supermercados dia", "dia supermercados",
        "dia retail", "dia sa", "alcampo", "eroski", "consum", "hipercor", "bonpreu", "bon preu",
        "caprabo", "condis", "ahorramas", "gadis", "froiz", "spar", "supercor", "family cash",
        "e leclerc", "leclerc", "mas y mas", "covirán", "coviran", "fruteria*", "carniceria*",
        "panaderia*", "pescaderia*", "charcuteria*", "supermercado*", "hipermercado*",
        "ultramarinos", "alimentacion", "bm supermercados", "plaza de dia", "makro",
        "costco", "el arbol", "masymas", "sorli", "veritas", "naturhouse",
    ]),
    ("Transporte Público", [
        "metro", "renfe", "emt", "tmb", "bus", "autobus*", "tranvia*", "cercanias", "alsa",
        "blablacar", "uber", "cabify", "bolt", "taxi*", "freenow", "free now", "ouigo",
        "iryo", "avlo", "ave", "billete*", "abono transporte", "consorcio transportes",
        "bicimad", "bicing", "tussam", "auvasa", "metrovalencia", "interurbano*",
        "tarjeta transporte", "tren", "trenes", "lime", "cercanias renfe", "monbus",
        "avanza", "ctm", "tram", "ferrocarril*", "fgc", "euskotren", "metro bilbao",
    ]),
    ("Vehículo y Gasolina", [
        "repsol", "cepsa", "moeve", "bp", "shell", "galp", "gasolinera*", "estacion de servicio",
        "carburante*", "gasoil", "diesel", "peaje*", "autopista*", "parking*", "aparcamiento*",
        "itv", "taller*", "norauto", "midas", "feu vert", "seguro coche", "seguro auto",
        "linea directa", "dgt", "multa*", "neumatico*", "lavadero*", "tesla", "supercharger",
        "ionity", "zity", "emov", "cooltra", "car2go", "plenergy", "petroprix", "ballenoil",
        "automocion", "recambios",
    ]),
    ("Vivienda", [
        "alquiler", "hipoteca", "comunidad de propietarios", "comunidad propietarios",
        "comunidad vecinos", "ibi", "arrendamiento*", "inmobiliaria*", "fianza", "gastos comunidad",
        "administrador de fincas", "cuota hipoteca", "prestamo hipotecario", "seguro hogar",
        "seguro del hogar", "habitaclia", "idealista", "tasa basura", "tasa de basuras",
    ]),
    ("Servicios del Hogar", [
        "endesa", "iberdrola", "naturgy", "repsol luz", "holaluz", "octopus energy", "totalenergies",
        "electricidad", "gas natural", "canal de isabel", "aguas de", "emasesa",
        "aigues", "movistar", "vodafone", "orange", "digi", "yoigo", "masmovil", "pepephone",
        "lowi", "o2", "jazztel", "telefonica", "fibra", "internet", "telefono", "wifi",
        "recibo luz", "recibo agua", "recibo gas", "ferreteria*", "leroy merlin", "bricomart", "brico depot", "bricodepot", "limpieza",
        "tintoreria*", "cerrajeria*", "fontaner*", "electricista*", "factura luz",
        "factura gas", "factura agua", "suministro*", "gas butano", "butano", "som energia",
    ]),
    ("Compras y Ropa", [
        "zara", "h m", "pull bear", "bershka", "stradivarius", "mango", "primark",
        "massimo dutti", "uniqlo", "shein", "el corte ingles", "corte ingles", "amazon",
        "amzn", "aliexpress", "temu", "ebay", "wallapop", "ikea", "decathlon", "media markt",
        "mediamarkt", "pccomponentes", "fnac", "apple store", "apple com", "worten", "miravia",
        "zalando", "asos", "lefties", "springfield", "sfera", "tendam", "cortefiel", "deichmann",
        "foot locker", "jd sports", "sprinter", "nike", "adidas", "maisons du monde",
        "conforama", "druni", "sephora", "perfumeria*", "primor", "juguetes*",
        "toys r us", "libreria*", "papeleria*", "regalo*", "pandora", "parfois", "kiabi",
        "vinted", "etsy", "lidl online", "tedi", "ropa", "zapateria*", "calzados*",
    ]),
    ("Salud y Bienestar", [
        "farmacia*", "parafarmacia*", "clinica*", "dentista*", "dental*", "hospital*", "medic*",
        "centro medico", "fisioterap*", "psicolog*", "optica*", "general optica", "multiopticas",
        "alain afflelou", "afflelou", "veterinari*", "gimnasio*", "gym", "basic fit", "basicfit",
        "vivagym", "mcfit", "altafit", "holmes place", "sportlife", "yoga", "pilates", "sanitas",
        "adeslas", "asisa", "dkv", "mapfre salud", "peluqueria*", "barberia*", "estetica*",
        "spa", "quiron", "hm hospitales", "vithas", "ribera salud", "analisis clinicos",
    ]),
    ("Viajes y Vacaciones", [
        "booking", "airbnb", "vueling", "ryanair", "iberia", "air europa", "easyjet", "volotea",
        "binter", "lufthansa", "klm", "air france", "tap air", "norwegian", "wizz", "hotel*",
        "hostal*", "apartamento turistico", "expedia", "skyscanner", "kayak", "trivago",
        "edreams", "rumbo", "logitravel", "viajes el corte ingles", "halcon viajes",
        "tripadvisor", "civitatis", "getyourguide", "hertz", "avis", "europcar", "sixt",
        "goldcar", "rent a car", "camping*", "paradores", "melia", "nh hotel", "iberostar",
        "barcelo", "marriott", "hilton", "agencia viajes", "vacaciones", "aeropuerto*",
        "aena", "aerolinea*", "vuelo*", "crucero*", "hostel*",
    ]),
    ("Educación", [
        "universidad*", "matricula*", "academia*", "colegio*", "guarderia*", "escuela*",
        "curso*", "udemy", "coursera", "domestika", "master", "formacion", "idiomas",
        "open english", "cambridge", "uned", "libros texto", "tasas universitarias",
    ]),
    ("Ocio y Tiempo Libre", [
        "cine*", "cinesa", "yelmo", "kinepolis", "teatro*", "concierto*", "entradas", "ticketmaster",
        "ticketea", "eventbrite", "steam", "steam games", "playstation store", "nintendo eshop",
        "xbox", "epic games", "blizzard", "riot games", "discoteca*", "club", "pub", "copas",
        "ocio", "bolera*", "parque atracciones", "port aventura", "portaventura", "zoo",
        "museo*", "loteria*", "apuestas", "bet365", "codere", "sportium", "casino*", "kiosko*",
        "estanco*", "tabaco*", "tabacalera", "fiesta*", "karaoke", "escape room", "futbol",
        "deporte*", "padel", "tenis", "golf", "ocio nocturno", "gamestop",
    ]),
]

_REGLAS_INGRESO: List[Tuple[str, Sequence[str]]] = [
    ("Nómina y Salario", [
        "nomina*", "salario*", "sueldo*", "haberes", "paga extra", "paga", "retribucion*",
        "payroll", "salary", "pension*", "prestacion*", "finiquito*", "dieta*", "honorarios",
    ]),
    ("Transferencias", [
        "transferencia*", "transf", "transfer*", "bizum", "traspaso*", "ingreso transferencia",
        "trf", "paypal", "revolut", "wise",
    ]),
]


def normalizar(texto: str) -> str:
    """ASCII, minúsculas y solo [a-z0-9] separados por un espacio."""
    t = a_ascii_minusculas(texto or "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _compilar(reglas: Sequence[Tuple[str, Sequence[str]]]) -> List[Tuple[str, "re.Pattern[str]"]]:
    compiladas = []
    for categoria, palabras in reglas:
        partes = []
        # Las más largas primero para que "uber eats" gane a "uber".
        for palabra in sorted(set(palabras), key=len, reverse=True):
            prefijo = palabra.endswith("*")
            nucleo = normalizar(palabra.rstrip("*"))
            if not nucleo:
                continue
            patron = re.escape(nucleo)
            partes.append(rf"(?<![a-z0-9]){patron}" + ("" if prefijo else r"(?![a-z0-9])"))
        compiladas.append((categoria, re.compile("|".join(partes))))
    return compiladas


_GASTO_RE = _compilar(_REGLAS_GASTO)
_INGRESO_RE = _compilar(_REGLAS_INGRESO)


# ---------------------------------------------------------------------------
# Aprendizaje por usuario
# ---------------------------------------------------------------------------
_RUIDO_CLAVE = {
    "compra", "pago", "tarjeta", "tarj", "movil", "recibo", "adeudo", "en", "de", "del", "la",
    "el", "los", "las", "con", "sa", "sl", "slu", "s", "a", "l", "es", "com", "www", "eur",
    "ref", "referencia", "operacion", "trans", "cargo", "abono", "domiciliacion", "tpv",
    "comercio", "pos", "visa", "mastercard", "debito", "credito", "nº", "no", "num", "y",
    "por", "para", "al", "su", "mi", "compra tarjeta", "pagos",
}
_AMBIGUOS = {"amazon", "paypal", "apple", "google", "bizum", "transferencia", "transf", "traspaso",
             "microsoft", "ebay", "uber", "cabify", "el", "movistar", "orange", "bp"}


def clave_comercio(concepto: str) -> str:
    """Huella estable del comercio dentro de un concepto bancario.

    ``"COMPRA TARJ. 5540 MERCADONA VALENCIA 03"`` -> ``"mercadona"``.
    Para marcas ambiguas (Amazon, PayPal...) se usan dos palabras
    (``"amazon prime"``) para no generalizar de más.
    """
    palabras = [
        w for w in normalizar(concepto).split()
        if not any(ch.isdigit() for ch in w) and w not in _RUIDO_CLAVE and len(w) > 1
    ]
    if not palabras:
        return ""
    primera = palabras[0]
    if primera in _AMBIGUOS and len(palabras) > 1:
        return f"{primera} {palabras[1]}"
    return primera if len(primera) >= 4 or len(palabras) == 1 else " ".join(palabras[:2])


def _coincide_clave(clave: str, texto_normalizado: str) -> bool:
    return bool(clave) and f" {clave} " in f" {texto_normalizado} "


def coincide_regla(clave: str, concepto: str) -> bool:
    """¿El concepto pertenece al comercio identificado por ``clave``?"""
    return _coincide_clave(clave, normalizar(concepto))


def categorizar_movimiento(concepto: str, tipo: str,
                           reglas_usuario: Optional[Mapping[str, str]] = None) -> str:
    """Devuelve la categoría de un movimiento.

    ``reglas_usuario`` mapea ``clave_comercio -> categoría`` (las aprendidas de
    las correcciones manuales del usuario para este ``tipo``). Se prueban de la
    más específica (clave más larga) a la más genérica.
    """
    texto = normalizar(concepto)
    if reglas_usuario:
        for clave in sorted(reglas_usuario, key=len, reverse=True):
            if _coincide_clave(clave, texto):
                return reglas_usuario[clave]
    if tipo == "ingreso":
        for categoria, patron in _INGRESO_RE:
            if patron.search(texto):
                return categoria
        return CATEGORIA_INGRESO_DEFECTO
    for categoria, patron in _GASTO_RE:
        if patron.search(texto):
            return categoria
    return CATEGORIA_GASTO_DEFECTO


def cargar_reglas(db, usuario_id: int, tipo: Optional[str] = None) -> Dict[str, str]:
    """Reglas aprendidas del usuario como ``{clave: categoria}``."""
    if tipo:
        filas = db.query("SELECT patron, categoria FROM reglas_categoria WHERE usuario_id = ? AND tipo = ?",
                         (usuario_id, tipo))
    else:
        filas = db.query("SELECT patron, categoria FROM reglas_categoria WHERE usuario_id = ?", (usuario_id,))
    return {f["patron"]: f["categoria"] for f in filas}


def aprender_regla(db, usuario_id: int, tipo: str, concepto: str, categoria: str) -> str:
    """Guarda (o actualiza) la regla ``comercio -> categoría``. Devuelve la clave."""
    clave = clave_comercio(concepto)
    if not clave:
        return ""
    db.execute(
        "INSERT INTO reglas_categoria (usuario_id, tipo, patron, categoria) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (usuario_id, tipo, patron) DO UPDATE SET categoria = excluded.categoria",
        (usuario_id, tipo, clave, categoria),
    )
    return clave


def recategorizar_similares(db, usuario_id: int, tipo: str, clave: str, categoria: str,
                            excluir_id: Optional[int] = None) -> int:
    """Aplica ``categoria`` a los movimientos pasados del mismo comercio."""
    if not clave:
        return 0
    filas = db.query(
        "SELECT id, concepto FROM movimientos WHERE usuario_id = ? AND tipo = ? AND categoria <> ?",
        (usuario_id, tipo, categoria),
    )
    ids = [f["id"] for f in filas
           if f["id"] != excluir_id and coincide_regla(clave, f["concepto"])]
    total = 0
    with db.transaction():
        for i in range(0, len(ids), 400):
            trozo = ids[i:i + 400]
            marcadores = ", ".join("?" for _ in trozo)
            total += db.execute(
                f"UPDATE movimientos SET categoria = ? WHERE usuario_id = ? AND id IN ({marcadores})",
                [categoria, usuario_id, *trozo],
            )
    return total
