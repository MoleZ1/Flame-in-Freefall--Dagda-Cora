"""
Motor de afinidad relacional (deterministico, sin LLM).

Para cada investigacion PSI calcula cinco componentes y explica "en que sentido" es afin:
  1. variables   : fraccion de las variables del usuario que la investigacion tambien mide
  2. rangos      : cuanto se solapan los rangos (en espacio phi, con margen de una sigma)
  3. contexto    : coincidencia de combustible (o familia), gravedad y tipo de fenomeno
  4. puntos      : fraccion de filas del usuario con al menos un experimento cercano en TODAS
                   las variables compartidas (compuerta conjunta, |z| <= Z_MAX)
  5. texto       : similitud lexica (TF-IDF) con la descripcion y los reportes de la investigacion
La afinidad global es un promedio ponderado de los componentes disponibles. Los pesos y las
tolerancias son PROVISIONALES (no calibrados con juicios expertos) y se devuelven en la respuesta.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .ingesta import FAMILIA, detectar_fenomenos

PESOS = {"puntos": 0.35, "rangos": 0.25, "contexto": 0.20, "variables": 0.10, "texto": 0.10}
Z_MAX = 2.5
MIN_COMPARTIDAS = 2
GRAV_MAPA = {"microgravity_iss": "microgravity", "simulated_microgravity": "microgravity", "normal_gravity_lab": "normal_gravity"}
NOMBRE_FEN = {"droplet": "combustion de gotas", "counterflow": "llamas en contraflujo", "spherical": "llamas esfericas",
              "cool_flame": "llamas frias (cool flames)", "extinction": "extincion", "premixed": "llamas premezcladas"}


# variables que delatan la configuracion experimental
VAR_FEN = {"initial_droplet_diameter": "droplet", "droplet_diameter": "droplet", "burning_rate_constant": "droplet",
           "cool_flame_extinction_droplet_diameter": "droplet", "hot_flame_extinction_droplet_diameter": "droplet",
           "strain_rate": "counterflow", "cool_flame_extinction_strain_rate": "counterflow", "hot_flame_extinction_strain_rate": "counterflow",
           "burner_fuel_mole_fraction": "spherical", "burner_oxygen_mole_fraction": "spherical", "fuel_mass_flow_rate": "spherical",
           "oxygen_mass_flow_rate": "spherical", "equivalent_fuel_burn_rate": "spherical"}
GEOMETRIAS = {"droplet", "counterflow", "spherical"}
FACTOR_CONFIG_DISTINTA = 0.6


def nivel(a):
    if a is None:
        return "sin datos comparables"
    return "alta" if a >= 0.6 else "media" if a >= 0.35 else "baja" if a >= 0.15 else "nula"


class Afinidad:
    def __init__(self, base):
        self.b = base
        self.idx = {v: j for j, v in enumerate(base.continuas)}
        # texto por investigacion (descripcion + fragmentos de reportes) y por fragmento
        self.inv_ids = list(base.inv)
        docs_inv = []
        for i in self.inv_ids:
            extra = " ".join(c["text"] for c in base.chunks if i in (c.get("investigation_ids") or []))
            docs_inv.append(base.texto_investigacion(i) + " " + extra)
        self.tfidf = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), min_df=1, stop_words="english")
        self.M_inv = self.tfidf.fit_transform(docs_inv)
        self.M_chk = self.tfidf.transform([c["text"] for c in base.chunks]) if base.chunks else None
        self.fen_inv = {i: set(detectar_fenomenos(base.texto_investigacion(i))) |
                        {t for f in base.perfil_inv[i]["fenomenos"] for t in detectar_fenomenos(f.replace("_", " "))}
                        for i in self.inv_ids}

    # ------------------------------------------------------------------------------------
    def perfil_usuario(self, entradas: list[dict], consulta: dict | None) -> dict:
        filas, comb, grav, fen, texto = [], set(), set(), set(), []
        for e in entradas:
            for t in e["tablas"]:
                for f in t["filas"]:
                    filas.append({"fuente": f'{e["archivo"]}:{t["tabla"]}', "valores": f})
            comb |= set(e["categoricos"]["combustibles"]); grav |= set(e["categoricos"]["gravedad"]); fen |= set(e["fenomenos"])
            texto.append(e["texto"] + " " + " ".join(a["columna"] for a in e["asignaciones"]))
            if e.get("cantidades_en_texto") and not any(t["filas"] for t in e["tablas"]):
                filas.append({"fuente": f'{e["archivo"]}:texto', "valores": {k: v["valor"] for k, v in e["cantidades_en_texto"].items()}})
        if consulta:
            if consulta["condiciones"]:
                filas.append({"fuente": "banner", "valores": {k: v["valor"] for k, v in consulta["condiciones"].items()}})
            comb |= set(consulta["combustibles"]); grav |= set(consulta["gravedad"]); fen |= set(consulta["fenomenos"])
            texto.append(consulta.get("texto", ""))
        vars_ = sorted({v for f in filas for v in f["valores"] if v in self.idx})
        fen |= {VAR_FEN[v] for v in vars_ if v in VAR_FEN}
        rangos = {}
        for v in vars_:
            xs = [f["valores"][v] for f in filas if v in f["valores"]]
            var = self.b.variables[v]
            ph = [var.a_phi(x) for x in xs]
            rangos[v] = {"n": len(xs), "min": min(xs), "max": max(xs), "min_phi": min(ph), "max_phi": max(ph), "unidad": var.canonical_unit}
        return {"filas": filas, "variables": vars_, "rangos": rangos, "combustibles": sorted(comb), "gravedad": sorted(grav),
                "fenomenos": sorted(fen), "texto": " ".join(texto).strip()}

    # ------------------------------------------------------------------------------------
    def _rangos(self, U, P, compartidas):
        detalle, fr = {}, []
        for v in compartidas:
            u, p, s = U["rangos"][v], P["rangos"][v], self.b.variables[v].sigma
            lo, hi = p["min_phi"] - s, p["max_phi"] + s
            if u["max_phi"] - u["min_phi"] < 1e-12:
                x = u["min_phi"]
                f = 1.0 if lo <= x <= hi else 0.0
                dist = 0.0 if f else (min(abs(x - lo), abs(x - hi)) / s)
            else:
                f = max(0.0, min(u["max_phi"], hi) - max(u["min_phi"], lo)) / (u["max_phi"] - u["min_phi"])
                dist = 0.0 if f > 0 else min(abs(u["min_phi"] - hi), abs(u["max_phi"] - lo)) / s
            estado = "dentro" if f >= 0.999 else "parcial" if f > 0 else "fuera"
            detalle[v] = {"solape": round(f, 3), "estado": estado, "distancia_sigmas": round(dist, 2),
                          "usuario": [u["min"], u["max"]], "base": [p["min"], p["max"]], "unidad": p["unidad"]}
            fr.append(f)
        return (float(np.mean(fr)) if fr else None), detalle

    def _contexto(self, U, inv_id):
        P = self.b.perfil_inv[inv_id]
        partes, sentido = [], []
        if U["combustibles"]:
            mismos = set(U["combustibles"]) & set(P["combustibles"])
            fam_u = {FAMILIA.get(c) for c in U["combustibles"]} - {None}
            fam_p = set(P["familias"])
            if mismos:
                partes.append(1.0); sentido.append(f"mismo combustible: {', '.join(sorted(mismos))}")
            elif fam_u & fam_p:
                partes.append(0.5); sentido.append(f"misma familia de combustible ({', '.join(sorted(fam_u & fam_p))}), distinto compuesto")
            else:
                partes.append(0.0); sentido.append(f"combustible distinto (base: {', '.join(P['combustibles'])})")
        if U["gravedad"]:
            g_p = {GRAV_MAPA.get(g, g) for g in P["gravedad"]}
            if set(U["gravedad"]) & g_p:
                partes.append(1.0); sentido.append(f"mismo entorno de gravedad ({', '.join(sorted(set(U['gravedad']) & g_p))})")
            else:
                partes.append(0.0); sentido.append(f"entorno de gravedad distinto (base: {', '.join(sorted(g_p))})")
        if U["fenomenos"]:
            comun = set(U["fenomenos"]) & self.fen_inv[inv_id]
            partes.append(len(comun) / len(set(U["fenomenos"])))
            if comun:
                sentido.append("fenomeno en comun: " + ", ".join(NOMBRE_FEN.get(f, f) for f in sorted(comun)))
            else:
                sentido.append("fenomeno distinto: " + ", ".join(NOMBRE_FEN.get(f, f) for f in sorted(self.fen_inv[inv_id])))
        return (float(np.mean(partes)) if partes else None), sentido

    def _puntos(self, U, inv_id, compartidas, top=5):
        if len(compartidas) < MIN_COMPARTIDAS:
            return None, [], 0
        filas_inv = np.where(self.b.inv_de == inv_id)[0]
        Xi = self.b.X[filas_inv]
        con_evidencia, n_eval, mejores = 0, 0, {}
        for f in U["filas"]:
            vs = [v for v in compartidas if v in f["valores"]]
            if len(vs) < MIN_COMPARTIDAS:
                continue
            n_eval += 1
            js = [self.idx[v] for v in vs]
            q = np.array([self.b.variables[v].a_phi(f["valores"][v]) for v in vs])
            sub = Xi[:, js]
            presentes = ~np.isnan(sub)
            k = presentes.sum(1)
            z = np.where(presentes, (sub - q) / self.b.sig[js], 0.0)
            zmax = np.abs(z).max(1)
            ok = (k >= MIN_COMPARTIDAS) & (zmax <= Z_MAX)
            if ok.any():
                con_evidencia += 1
            w = np.where(ok, np.exp(-0.5 * (z ** 2).sum(1) / np.maximum(k, 1)), 0.0)
            for r in np.argsort(-w)[:top]:
                if w[r] <= 0:
                    break
                e = self.b.exps[filas_inv[r]]
                if e["id"] not in mejores or mejores[e["id"]]["similitud"] < w[r]:
                    mejores[e["id"]] = {"experimento": e["id"], "similitud": round(float(w[r]), 3), "fila_usuario": f["fuente"],
                                        "diferencias_sigmas": {vs[t]: round(float(z[r, t]), 2) for t in range(len(vs)) if presentes[r, t]},
                                        "combustible": e["combustible"], "dataset": e["dataset"], "archivo": e["archivo"], "fila": e["fila"],
                                        "gravedad": e["gravedad"], "resultados": e["resultados"]}
        if n_eval == 0:
            return None, [], 0
        lista = sorted(mejores.values(), key=lambda m: -m["similitud"])[:top]
        return con_evidencia / n_eval, lista, n_eval

    # ------------------------------------------------------------------------------------
    def relacionar(self, entradas: list[dict], consulta: dict | None) -> dict:
        U = self.perfil_usuario(entradas, consulta)
        cos_inv = None
        n_palabras = len(U["texto"].split())
        if n_palabras >= 5:
            qv = self.tfidf.transform([U["texto"]])
            cos_inv = cosine_similarity(qv, self.M_inv)[0]
        resultados = []
        for k, inv_id in enumerate(self.inv_ids):
            P = self.b.perfil_inv[inv_id]
            compartidas = [v for v in U["variables"] if v in P["rangos"]]
            comp = {}
            comp["variables"] = (len(compartidas) / len(U["variables"])) if U["variables"] else None
            comp["rangos"], det_rangos = self._rangos(U, P, compartidas)
            comp["contexto"], sentido = self._contexto(U, inv_id)
            comp["puntos"], vecinos, n_eval = self._puntos(U, inv_id, compartidas)
            if U["variables"] and not compartidas:
                comp["rangos"], comp["puntos"] = 0.0, 0.0          # hay variables del usuario y ninguna en comun
            comp["texto"] = min(1.0, float(cos_inv[k]) / 0.25) if cos_inv is not None and n_palabras >= 30 else None
            disp = {c: v for c, v in comp.items() if v is not None}
            A = sum(PESOS[c] * v for c, v in disp.items()) / sum(PESOS[c] for c in disp) if disp else None
            geo_u, geo_i = set(U["fenomenos"]) & GEOMETRIAS, self.fen_inv[inv_id] & GEOMETRIAS
            config_distinta = bool(geo_u and geo_i and not (geo_u & geo_i))
            if A is not None and config_distinta:
                A *= FACTOR_CONFIG_DISTINTA
                sentido.append("configuracion distinta (" + ", ".join(NOMBRE_FEN[g] for g in sorted(geo_u)) + " frente a "
                               + ", ".join(NOMBRE_FEN[g] for g in sorted(geo_i)) + "): la comparacion es solo de condiciones")
            # explicacion
            if compartidas:
                sentido.insert(0, "variables compartidas: " + ", ".join(self.b.variables[v].label for v in compartidas))
            faltan = [self.b.variables[v].label for v in U["variables"] if v not in P["rangos"]]
            if faltan:
                sentido.append("la investigacion no mide: " + ", ".join(faltan))
            fuera = [self.b.variables[v].label for v, d in det_rangos.items() if d["estado"] == "fuera"]
            transfer = ("extrapolacion en " + ", ".join(fuera)) if fuera else ("interpolacion en las variables compartidas" if det_rangos else "no evaluable")
            resultados.append({"investigacion": inv_id, "titulo": self.b.inv[inv_id].get("title"), "acronimo": self.b.inv[inv_id].get("acronym"),
                               "afinidad": None if A is None else round(A, 3), "nivel": nivel(A),
                               "componentes": {c: (None if v is None else round(v, 3)) for c, v in comp.items()},
                               "sentido": sentido, "rangos": det_rangos, "transferibilidad": transfer,
                               "filas_evaluadas": n_eval, "vecinos": vecinos, "configuracion_distinta": config_distinta,
                               "similitud_texto_coseno": None if cos_inv is None else round(float(cos_inv[k]), 3)})
        resultados.sort(key=lambda r: -(r["afinidad"] or 0))
        afines = [r for r in resultados if (r["afinidad"] or 0) >= 0.15]
        return {"perfil_usuario": {k: v for k, v in U.items() if k not in ("filas", "texto")} | {"n_filas": len(U["filas"]), "n_palabras_texto": n_palabras},
                "investigaciones": resultados,
                "datasets": self._datasets(U, afines),
                "hallazgos": self._hallazgos(U, afines),
                "fragmentos": self._fragmentos(U["texto"]) if n_palabras >= 15 else [],
                "relaciones_entre_investigaciones": self._cruces([r["investigacion"] for r in afines]),
                "advertencias": self._advertencias(entradas, U, resultados),
                "parametros": {"pesos": PESOS, "z_max": Z_MAX, "factor_configuracion_distinta": FACTOR_CONFIG_DISTINTA, "min_variables_compartidas": MIN_COMPARTIDAS,
                               "tolerancias_provisionales": sorted(v for v in U["variables"] if self.b.variables[v].sigma_provisional)}}

    def _datasets(self, U, afines):
        out = []
        for r in afines:
            for ds_id, P in self.b.perfil_ds.items():
                if self.b.datasets[ds_id]["investigation_id"] != r["investigacion"]:
                    continue
                comp = [v for v in U["variables"] if v in P["rangos"]]
                s, _ = self._rangos(U, P, comp)
                n_vec = sum(1 for v in r["vecinos"] if v["dataset"] == ds_id)
                if comp or n_vec:
                    out.append({"dataset": ds_id, "investigacion": r["investigacion"], "titulo": self.b.datasets[ds_id].get("title"),
                                "variables_compartidas": comp, "solape_rangos": None if s is None else round(s, 3),
                                "vecinos_en_top": n_vec, "n_experimentos": P["n_experimentos"]})
        out.sort(key=lambda d: (-d["vecinos_en_top"], -(d["solape_rangos"] or 0), -len(d["variables_compartidas"])))
        return out[:8]

    def _hallazgos(self, U, afines):
        ids_afines = {r["investigacion"] for r in afines}
        vecinos = {v["experimento"] for r in afines for v in r["vecinos"]}
        out = []
        for h in self.b.hallazgos:
            if h["investigation_id"] not in ids_afines:
                continue
            motivo = []
            fuels = set((h.get("scope") or {}).get("fuels") or [])
            if fuels & set(U["combustibles"]):
                motivo.append("mismo combustible")
            if set(h.get("related_experiment_ids") or []) & vecinos:
                motivo.append("involucra experimentos cercanos a tus datos")
            if not motivo and not U["combustibles"]:
                motivo.append("investigacion afin")
            if motivo:
                ev = (h.get("evidence") or [{}])[0]
                out.append({"hallazgo": h["_id"], "investigacion": h["investigation_id"], "enunciado": h["statement"],
                            "tipo": h.get("statement_type"), "motivo": motivo, "cita": ev.get("quote", "")[:300],
                            "documento": ev.get("document_id"), "pagina": ev.get("page")})
        return out[:6]

    def _fragmentos(self, texto, top=3):
        if self.M_chk is None:
            return []
        s = cosine_similarity(self.tfidf.transform([texto]), self.M_chk)[0]
        out = []
        for i in np.argsort(-s)[:top]:
            if s[i] < 0.15:
                break
            c = self.b.chunks[i]
            out.append({"fragmento": c["_id"], "documento": c.get("parent_document_id"), "titulo": c.get("title"),
                        "paginas": [c.get("page_start"), c.get("page_end")], "similitud": round(float(s[i]), 3), "extracto": c["text"][:280]})
        return out

    def _cruces(self, ids):
        out = []
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                A, B = self.b.perfil_inv[ids[a]], self.b.perfil_inv[ids[b]]
                comb = sorted(set(A["combustibles"]) & set(B["combustibles"]))
                vs = sorted(set(A["rangos"]) & set(B["rangos"]))
                grav = sorted({GRAV_MAPA.get(g, g) for g in A["gravedad"]} & {GRAV_MAPA.get(g, g) for g in B["gravedad"]})
                fen = sorted(self.fen_inv[ids[a]] & self.fen_inv[ids[b]])
                out.append({"entre": [ids[a], ids[b]], "combustibles_comunes": comb,
                            "variables_comunes": [self.b.variables[v].label for v in vs],
                            "gravedad_comun": grav, "fenomenos_comunes": [NOMBRE_FEN.get(f, f) for f in fen]})
        return out

    def _advertencias(self, entradas, U, resultados):
        adv = []
        for e in entradas:
            sin = [a["columna"] for a in e["asignaciones"] if a["estado"] == "sin_asignar"]
            sug = [f'{a["columna"]} -> {a["variable"]}' for a in e["asignaciones"] if a["estado"] == "sugerida"]
            unk = [f'{a["columna"]} ({a.get("unidad")})' for a in e["asignaciones"] if a["estado"] == "unidad_desconocida"]
            fuera = [a["columna"] for a in e["asignaciones"] if a.get("fuera_de_rango_plausible")]
            inf = [a["columna"] for a in e["asignaciones"] if a.get("fuente_unidad") == "inferida"]
            if sin: adv.append(f'{e["archivo"]}: columnas sin asignar: {", ".join(map(str, sin[:12]))}')
            if sug: adv.append(f'{e["archivo"]}: asignaciones sugeridas por parecido (confirmar): {", ".join(sug)}')
            if unk: adv.append(f'{e["archivo"]}: unidad no reconocida: {", ".join(unk)}')
            if fuera: adv.append(f'{e["archivo"]}: valores fuera del rango plausible en: {", ".join(fuera)}')
            if inf: adv.append(f'{e["archivo"]}: unidad inferida por los valores (no venia en el encabezado): {", ".join(inf)}')
            if e["tipo"] == "pdf" and e.get("cantidades_en_texto"):
                adv.append(f'{e["archivo"]}: cantidades tomadas del texto del PDF; verificar en el documento')
        if not U["variables"] and not U["combustibles"] and len(U["texto"].split()) < 30:
            adv.append("No se reconocio ninguna variable, combustible ni texto suficiente: no hay base para relacionar.")
        prov = [self.b.variables[v].label for v in U["variables"] if self.b.variables[v].sigma_provisional]
        if prov:
            adv.append("Tolerancias provisionales (pendientes de experto) para: " + ", ".join(prov))
        adv.append("Similitud no es transferibilidad: un experimento parecido no garantiza el mismo resultado en tu caso.")
        return adv
