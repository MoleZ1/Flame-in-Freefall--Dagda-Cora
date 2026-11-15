"""
Carga de la base PSI (semilla JSONL o MongoDB) y construccion de perfiles.

Perfil = para cada investigacion y cada dataset: variables presentes, rangos (unidad canonica),
combustibles, fenomenos, entorno de gravedad. Ademas, una matriz de experimentos en espacio phi
para buscar vecinos.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

SEED_DIR = os.environ.get("SEED_DIR", "schemas")   # solo si no hay MONGO_URI
COLECCIONES = ["investigations", "experiments", "variables", "measurements_metadata", "documents", "scientific_findings"]


def _leer_jsonl(ruta):
    with open(ruta, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def cargar_colecciones() -> dict[str, list[dict]]:
    uri = os.environ.get("MONGO_URI")
    if uri:
        from pymongo import MongoClient  # pip install pymongo
        db = MongoClient(uri)[os.environ.get("MONGO_DB", "psi_science")]
        return {c: list(db[c].find()) for c in COLECCIONES}
    return {c: _leer_jsonl(os.path.join(SEED_DIR, f"{c}.jsonl")) if os.path.exists(os.path.join(SEED_DIR, f"{c}.jsonl")) else []
            for c in COLECCIONES}


def sigma_provisional(v: dict) -> tuple[float, bool]:
    """El registro tiene sigma = null (pendiente de experto). Se usa una tolerancia provisional y se marca."""
    if v.get("sigma") is not None:
        return float(v["sigma"]), False
    if v.get("phi") == "log":
        return 0.35, True                    # factor ~1.4
    if v.get("canonical_unit") == "K":
        return 100.0, True
    if v.get("canonical_unit") == "1":
        return 0.03, True
    pr = v.get("plausible_range") or {}
    if "min" in pr and "max" in pr and pr["max"] > pr["min"]:
        return 0.1 * (pr["max"] - pr["min"]), True
    return 1.0, True


@dataclass
class Variable:
    id: str
    label: str
    role: str
    value_type: str
    canonical_unit: str | None
    dimension: str | None
    phi: str | None
    synonyms: list[str]
    sigma: float
    sigma_provisional: bool
    rango: dict | None

    def a_phi(self, x: float) -> float:
        if self.phi == "log":
            return math.log(max(x, 1e-12))
        return x


def _primero_reportado(lista, var):
    """Si una variable aparece varias veces (p. ej. before_test / after_test) se usa la primera reportada."""
    for c in lista:
        if c.get("variable") == var and c.get("status") == "reported":
            return c
    return None


@dataclass
class Base:
    col: dict
    variables: dict[str, Variable] = field(default_factory=dict)
    exps: list[dict] = field(default_factory=list)

    def __post_init__(self):
        for v in self.col["variables"]:
            s, prov = sigma_provisional(v)
            self.variables[v["_id"]] = Variable(v["_id"], v.get("label") or v["_id"], v.get("role", ""), v.get("value_type", ""),
                                                v.get("canonical_unit"), v.get("dimension"), v.get("phi"),
                                                list(v.get("synonyms") or []), s, prov, v.get("plausible_range"))
        self.continuas = [k for k, v in self.variables.items() if v.value_type == "continuous" and v.canonical_unit]
        self.inv = {i["investigation_id"]: i for i in self.col["investigations"]}
        self.datasets = {d["dataset_id"]: d for d in self.col["measurements_metadata"]}
        self.hallazgos = self.col["scientific_findings"]
        self.chunks = [d for d in self.col["documents"] if d.get("record_type") == "chunk" and d.get("text")]
        self._construir_matriz()
        self._construir_perfiles()

    # ---------------------------------------------------------------------------------
    def _construir_matriz(self):
        idx = {v: j for j, v in enumerate(self.continuas)}
        X = np.full((len(self.col["experiments"]), len(self.continuas)), np.nan)
        for i, e in enumerate(self.col["experiments"]):
            registros = (e.get("conditions") or []) + (e.get("measurements") or []) + (e.get("outcomes") or [])
            for v in self.continuas:
                c = _primero_reportado(registros, v)
                if c is None:
                    continue
                val = (c.get("norm") or {}).get("value")
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    X[i, idx[v]] = self.variables[v].a_phi(float(val))
            grav = _primero_reportado(e.get("conditions") or [], "gravity_environment")
            fuel = e.get("fuel") or {}
            prov = e.get("provenance") or {}
            self.exps.append({
                "id": e["_id"], "inv": e["investigation_id"], "fenomeno": e.get("phenomenon"),
                "configuracion": e.get("configuration") or {}, "combustible": fuel.get("canonical"),
                "familia": fuel.get("family"), "clase": fuel.get("class"),
                "gravedad": (grav or {}).get("norm", {}).get("value") if grav else None,
                "origen": e.get("data_origin"), "tipo": e.get("record_type"),
                "dataset": prov.get("dataset_id"), "archivo": prov.get("file"), "fila": prov.get("row"),
                "resultados": {o["variable"]: (o.get("norm") or {}).get("value") for o in (e.get("outcomes") or []) if o.get("status") == "reported"},
            })
        self.X = X
        self.sig = np.array([self.variables[v].sigma for v in self.continuas])
        self.inv_de = np.array([e["inv"] for e in self.exps])

    def _perfil(self, filas: np.ndarray) -> dict:
        sub = self.X[filas]
        rangos = {}
        for j, v in enumerate(self.continuas):
            col = sub[:, j]
            col = col[~np.isnan(col)]
            if len(col):
                var = self.variables[v]
                inv = (lambda z: math.exp(z)) if var.phi == "log" else (lambda z: z)
                rangos[v] = {"n": int(len(col)), "min_phi": float(col.min()), "max_phi": float(col.max()),
                             "min": inv(float(col.min())), "max": inv(float(col.max())), "unidad": var.canonical_unit}
        ex = [self.exps[i] for i in np.where(filas)[0]]
        return {"n_experimentos": int(filas.sum()), "rangos": rangos,
                "combustibles": dict(Counter(e["combustible"] for e in ex if e["combustible"])),
                "familias": dict(Counter(e["familia"] for e in ex if e["familia"])),
                "fenomenos": dict(Counter(e["fenomeno"] for e in ex if e["fenomeno"])),
                "gravedad": dict(Counter(e["gravedad"] for e in ex if e["gravedad"])),
                "configuraciones": dict(Counter(f'{e["configuracion"].get("geometry")}/{e["configuracion"].get("flame_type")}' for e in ex)),
                "origen": dict(Counter(e["origen"] for e in ex))}

    def _construir_perfiles(self):
        self.perfil_inv = {i: self._perfil(self.inv_de == i) for i in self.inv}
        ds = np.array([e["dataset"] for e in self.exps])
        self.perfil_ds = {d: self._perfil(ds == d) for d in self.datasets if (ds == d).any()}

    # ---------------------------------------------------------------------------------
    def texto_investigacion(self, inv_id: str) -> str:
        i = self.inv[inv_id]
        partes = [i.get("title") or "", i.get("acronym") or "", i.get("sub_research_area") or "", i.get("research_area") or "",
                  i.get("hypothesis") or "", i.get("objective") or "", i.get("approach") or "", " ".join(i.get("design_descriptors") or [])]
        p = self.perfil_inv[inv_id]
        partes += list(p["combustibles"]) + list(p["fenomenos"]) + list(p["gravedad"])
        partes += [self.variables[v].label for v in p["rangos"]]
        return " ".join(partes)

    def ids_validos(self) -> set[str]:
        s = set(self.inv) | {e["id"] for e in self.exps} | set(self.datasets) | {h["_id"] for h in self.hallazgos}
        s |= {c["_id"] for c in self.chunks}
        return s


_BASE = None


def obtener_base() -> Base:
    global _BASE
    if _BASE is None:
        _BASE = Base(cargar_colecciones())
    return _BASE
