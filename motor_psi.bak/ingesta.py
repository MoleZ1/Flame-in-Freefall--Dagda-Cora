"""
Ingesta de lo que sube el usuario: CSV, XLSX y PDF, mas el texto del banner de busqueda.

Todo es deterministico y auditable:
  - columnas -> variables del registro por sinonimos (exacto) o por parecido (sugerido, requiere confirmar);
  - unidades leidas del encabezado, p. ej. "P (atm)", "mHC (mg/s)", "do [mm]";
  - combustibles, entorno de gravedad y tipo de fenomeno detectados por palabras clave;
  - en PDF: texto por pagina y tablas; las cantidades del texto se marcan como candidatas.
Ningun valor se inventa: lo que no se reconoce queda listado como "sin asignar".
"""
from __future__ import annotations

import difflib
import io
import math
import re
import unicodedata

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------------------
# Unidades: factor a la unidad canonica del registro
# ----------------------------------------------------------------------------------------
UNIDADES = {
    "Pa": {"pa": 1, "kpa": 1e3, "mpa": 1e6, "bar": 1e5, "mbar": 100, "atm": 101325, "psi": 6894.757, "torr": 133.322, "mmhg": 133.322},
    "1": {"1": 1, "-": 1, "frac": 1, "fraction": 1, "%": 0.01, "mol%": 0.01, "mol.%": 0.01, "vol%": 0.01, "ppm": 1e-6},
    "kg/s": {"kg/s": 1, "g/s": 1e-3, "mg/s": 1e-6, "ug/s": 1e-9},
    "m": {"m": 1, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "µm": 1e-6, "micron": 1e-6},
    "s": {"s": 1, "ms": 1e-3, "min": 60},
    "1/s": {"1/s": 1, "s-1": 1, "s^-1": 1, "hz": 1},
    "m^2/s": {"m^2/s": 1, "m2/s": 1, "mm^2/s": 1e-6, "mm2/s": 1e-6, "cm^2/s": 1e-4, "cm2/s": 1e-4},
    "m^2": {"m^2": 1, "m2": 1, "mm^2": 1e-6, "mm2": 1e-6},
    "W": {"w": 1, "mw": 1e-3, "kw": 1e3},
}

COMBUSTIBLES = {  # sinonimo normalizado -> nombre canonico de la base
    "n-heptane": "n-heptane", "heptane": "n-heptane", "nheptane": "n-heptane", "heptano": "n-heptane", "n-heptano": "n-heptane",
    "n-dodecane": "n-dodecane", "dodecane": "n-dodecane", "dodecano": "n-dodecane", "n-dodecano": "n-dodecane",
    "n-decane": "n-decane", "decane": "n-decane", "decano": "n-decane",
    "n-octane": "n-octane", "octane": "n-octane", "octano": "n-octane",
    "n-tetradecane": "n-tetradecane", "tetradecane": "n-tetradecane", "tetradecano": "n-tetradecane",
    "n-butane": "n-butane", "butane": "n-butane", "butano": "n-butane", "n-butano": "n-butane",
    "propane": "propane", "propano": "propane", "ethane": "ethane", "etano": "ethane",
    "dimethyl ether": "dimethyl_ether", "dimethyl_ether": "dimethyl_ether", "dme": "dimethyl_ether", "dimetil eter": "dimethyl_ether",
    "methane": "methane", "metano": "methane", "ethylene": "ethylene", "etileno": "ethylene", "pmma": "PMMA",
}
FAMILIA = {"n-heptane": "n-alkane", "n-dodecane": "n-alkane", "n-decane": "n-alkane", "n-octane": "n-alkane",
           "n-tetradecane": "n-alkane", "n-butane": "n-alkane", "propane": "n-alkane", "ethane": "n-alkane",
           "methane": "n-alkane", "dimethyl_ether": "ether", "ethylene": "alkene", "PMMA": "polymer"}

GRAVEDAD = [(r"\b(iss|international space station|estaci[oó]n espacial|orbit|microgravit|micro-g|microgravedad|μg|ug\b|0 ?g\b)", "microgravity"),
            (r"\b(drop tower|torre de ca[ií]da|parabolic|parab[oó]lic)", "microgravity"),
            (r"\b(normal gravity|1 ?g\b|gravedad normal|ground|laboratorio en tierra|lab test)", "normal_gravity")]
FENOMENOS = {
    "droplet": ["droplet", "gota", "gotas", "d0", "d^2", "d2-law", "burning rate"],
    "counterflow": ["counterflow", "contraflujo", "strain rate", "tasa de deformaci", "opposed jet"],
    "spherical": ["spherical", "esfer", "porous burner", "quemador poroso", "inverse flame", "llama inversa", "normal flame"],
    "cool_flame": ["cool flame", "llama fr", "llamas fr", "low temperature", "baja temperatura", "two-stage", "three-stage"],
    "extinction": ["extinction", "extinci", "extinguish", "self-extinguish", "flameout"],
    "premixed": ["premixed", "premezcl"],
}


def norm_txt(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    return re.sub(r"\s+", " ", s)


_UNIDADES_CONOCIDAS = {u for t in UNIDADES.values() for u in t} | {c.lower() for c in UNIDADES} | {"k", "c", "degc", "°c"}


def _es_unidad(s: str) -> bool:
    return norm_txt(s).replace(" ", "").replace("²", "^2") in _UNIDADES_CONOCIDAS


def separar_unidad(encabezado: str) -> tuple[str, str | None]:
    """'mHC (mg/s)' -> ('mhc', 'mg/s');  'do [mm]' -> ('do', 'mm');  'XO2 %' -> ('xo2', '%').
    'XO2 (ambient)' no se separa: lo que va entre parentesis solo es unidad si es una unidad conocida."""
    h = norm_txt(encabezado)
    m = re.search(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$", h)
    if m and _es_unidad(m.group(1)):
        return h[: m.start()].strip(), m.group(1).strip()
    m = re.search(r"\s(%|mol%|ppm)$", h)
    if m:
        return h[: m.start()].strip(), m.group(1)
    return h, None


def factor_unidad(unidad: str | None, canonica: str | None) -> float | None:
    if canonica is None:
        return None
    if unidad is None:
        return None
    u = norm_txt(unidad).replace(" ", "").replace("²", "^2").replace("·", "")
    tabla = UNIDADES.get(canonica, {})
    if u in tabla:
        return float(tabla[u])
    if u == norm_txt(canonica):
        return 1.0
    return None


# ----------------------------------------------------------------------------------------
# Asignacion de columnas
# ----------------------------------------------------------------------------------------
def clave(s: str) -> str:
    """'X_O2' == 'XO2' == 'x-o2': solo letras y numeros."""
    return re.sub(r"[^a-z0-9%]", "", norm_txt(s))


def indice_sinonimos(base):
    exactos, todos = {}, {}
    for v in base.variables.values():
        for s in [v.id, v.label] + v.synonyms:
            nombre, unidad = separar_unidad(s)
            for k in (norm_txt(s), nombre, clave(s), clave(nombre)):
                if k:
                    exactos.setdefault(k, (v.id, unidad))
            todos[nombre] = v.id
    return exactos, todos


def asignar_columna(base, encabezado: str, valores: pd.Series, indice):
    exactos, todos = indice
    nombre, unidad = separar_unidad(encabezado)
    origen = "exacto"
    for k in (norm_txt(encabezado), nombre, clave(encabezado), clave(nombre)):
        if k in exactos:
            vid, unidad_sin = exactos[k]
            break
    else:
        cerca = difflib.get_close_matches(nombre, list(todos), n=1, cutoff=0.86)
        if not cerca:
            return {"columna": encabezado, "estado": "sin_asignar"}
        vid, unidad_sin, origen = todos[cerca[0]], None, "sugerido"
    var = base.variables[vid]
    if var.value_type != "continuous" or not var.canonical_unit:
        return {"columna": encabezado, "variable": vid, "estado": "categorica", "origen": origen}
    unidad = unidad or unidad_sin
    fuente_unidad = "encabezado" if unidad else None
    num = pd.to_numeric(valores, errors="coerce").dropna()
    if unidad is None and var.canonical_unit == "1" and len(num):
        unidad, fuente_unidad = ("%", "inferida") if num.max() > 1.0 else ("1", "inferida")
    f = factor_unidad(unidad, var.canonical_unit)
    if f is None:
        return {"columna": encabezado, "variable": vid, "estado": "unidad_desconocida", "unidad": unidad, "origen": origen}
    vals = (num * f).to_numpy(dtype=float)
    pr = var.rango or {}
    fuera = int(((vals < pr.get("min", -np.inf)) | (vals > pr.get("max", np.inf))).sum()) if pr else 0
    return {"columna": encabezado, "variable": vid, "estado": "asignada" if origen == "exacto" else "sugerida",
            "origen": origen, "unidad": unidad, "fuente_unidad": fuente_unidad, "factor": f, "n": int(len(vals)),
            "fuera_de_rango_plausible": fuera}


# ----------------------------------------------------------------------------------------
# Lectura de archivos
# ----------------------------------------------------------------------------------------
def _mejor_encabezado(df_crudo: pd.DataFrame, base, indice) -> pd.DataFrame:
    """En hojas de Excel el encabezado no siempre esta en la primera fila: se prueba hasta la fila 6."""
    mejor, mejor_n = None, -1
    for h in range(min(6, len(df_crudo))):
        cols = [str(c) for c in df_crudo.iloc[h].tolist()]
        n = sum(1 for c in cols if c and c != "nan" and (norm_txt(c) in indice[0] or separar_unidad(c)[0] in indice[0] or clave(separar_unidad(c)[0]) in indice[0]))
        if n > mejor_n:
            mejor, mejor_n = h, n
    df = df_crudo.iloc[mejor + 1:].copy()
    df.columns = [str(c) if str(c) != "nan" else f"col_{i}" for i, c in enumerate(df_crudo.iloc[mejor].tolist())]
    return df.dropna(how="all")


def leer_archivo(nombre: str, contenido: bytes, base) -> dict:
    ext = nombre.lower().rsplit(".", 1)[-1]
    indice = indice_sinonimos(base)
    tablas, texto, paginas = [], "", 0
    if ext == "csv":
        df = pd.read_csv(io.BytesIO(contenido), sep=None, engine="python")
        tablas.append(("csv", df))
    elif ext in ("xlsx", "xls"):
        hojas = pd.read_excel(io.BytesIO(contenido), sheet_name=None, header=None)
        for hoja, crudo in hojas.items():
            if crudo.dropna(how="all").empty:
                continue
            tablas.append((f"hoja:{hoja}", _mejor_encabezado(crudo, base, indice)))
    elif ext == "pdf":
        import pdfplumber
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            paginas = len(pdf.pages)
            partes = []
            for i, p in enumerate(pdf.pages[:60]):           # limite para PDFs muy largos
                partes.append(f"[p.{i + 1}] " + (p.extract_text() or ""))
                for t in p.extract_tables() or []:
                    if len(t) >= 2 and t[0]:
                        tablas.append((f"pdf_tabla:p{i + 1}", pd.DataFrame(t[1:], columns=[str(c) for c in t[0]])))
            texto = "\n".join(partes)
    else:
        raise ValueError(f"formato no soportado: .{ext} (use csv, xlsx o pdf)")

    asignaciones, filas_por_tabla, categoricos = [], [], {"combustibles": set(), "gravedad": set()}
    for origen_tabla, df in tablas:
        filas = [dict() for _ in range(len(df))]
        for col in df.columns:
            a = asignar_columna(base, col, df[col], indice)
            a["tabla"] = origen_tabla
            asignaciones.append(a)
            if a["estado"] in ("asignada", "sugerida"):
                num = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float) * a["factor"]
                for k, x in enumerate(num):
                    if not math.isnan(x):
                        filas[k][a["variable"]] = x
            elif a["estado"] == "categorica" or a.get("variable") is None:
                for celda in df[col].dropna().astype(str).unique()[:200]:
                    categoricos["combustibles"] |= detectar_combustibles(celda)
        filas_por_tabla.append({"tabla": origen_tabla, "filas": [f for f in filas if f]})
    texto_total = texto + " " + " ".join(str(c) for _, df in tablas for c in df.columns)
    categoricos["combustibles"] |= detectar_combustibles(texto_total)
    categoricos["gravedad"] |= detectar_gravedad(texto_total)
    return {"archivo": nombre, "tipo": ext, "paginas": paginas, "asignaciones": asignaciones,
            "tablas": filas_por_tabla, "texto": texto, "categoricos": {k: sorted(v) for k, v in categoricos.items()},
            "fenomenos": detectar_fenomenos(texto_total),
            "cantidades_en_texto": interpretar_texto(texto, base)["condiciones"] if texto else {}}


def detectar_combustibles(texto: str) -> set[str]:
    t = norm_txt(texto)
    encontrados = set()
    for sin, can in COMBUSTIBLES.items():
        if re.search(r"(?<![a-z])" + re.escape(sin) + r"(?![a-z])", t):
            encontrados.add(can)
    return encontrados


def detectar_gravedad(texto: str) -> set[str]:
    t = norm_txt(texto)
    return {g for patron, g in GRAVEDAD if re.search(patron, t)}


def detectar_fenomenos(texto: str) -> list[str]:
    t = norm_txt(texto)
    return sorted(f for f, claves in FENOMENOS.items() if any(c in t for c in claves))


# ----------------------------------------------------------------------------------------
# Texto libre del banner: "gota de n-heptano de 0.5 mm a 1 atm con 21% O2 en microgravedad"
# ----------------------------------------------------------------------------------------
_NUM = r"(\d+(?:[.,]\d+)?(?:e-?\d+)?)"
REGLAS_TEXTO = [  # (patron de unidad, palabras cercanas, variable)
    (r"(pa|kpa|mpa|bar|mbar|atm|psi|torr)", None, "pressure"),
    (r"(%|mol%|ppm)", r"(o2|ox[ií]geno|oxygen|x_?o2)", "ambient_oxygen_mole_fraction"),
    (r"(mm|um|µm|cm)", r"(gota|droplet|d0|do\b|di[aá]metro|diameter)", "initial_droplet_diameter"),
    (r"(mg/s|g/s|kg/s)", r"(fuel|combustible|hc|flujo|flow)", "fuel_mass_flow_rate"),
    (r"(1/s|s-1|s\^-1)", r"(strain|deformaci|a\b)", "strain_rate"),
    (r"(k)\b", r"(tad|adiab)", "adiabatic_flame_temperature"),
]


def interpretar_texto(texto: str, base) -> dict:
    t = norm_txt(texto).replace(",", ".")
    condiciones = {}
    for unidad_pat, cerca, vid in REGLAS_TEXTO:
        for m in re.finditer(_NUM + r"\s*" + unidad_pat + r"(?![a-z])", t):
            ventana = t[max(0, m.start() - 40): m.end() + 25]
            if cerca and not re.search(cerca, ventana):
                continue
            var = base.variables.get(vid)
            f = factor_unidad(m.group(2), var.canonical_unit) if var else None
            if f is None:
                continue
            valor = float(m.group(1)) * f
            condiciones.setdefault(vid, {"valor": valor, "texto": m.group(0)})
            break
    m = re.search(r"(x_?o2|o2)\s*[=:]\s*" + _NUM, t)
    if m and "ambient_oxygen_mole_fraction" not in condiciones:
        v = float(m.group(2))
        condiciones["ambient_oxygen_mole_fraction"] = {"valor": v / 100 if v > 1 else v, "texto": m.group(0)}
    return {"condiciones": condiciones, "combustibles": sorted(detectar_combustibles(t)),
            "gravedad": sorted(detectar_gravedad(t)), "fenomenos": detectar_fenomenos(t)}
