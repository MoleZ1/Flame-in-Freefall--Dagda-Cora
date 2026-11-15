"""
Afinidad SEMANTICA con embeddings.

Corpus indexado (una entrada por elemento de la base):
  investigacion  -> titulo, hipotesis, objetivo, enfoque, combustibles, variables
  dataset        -> titulo, descripcion, rol, variables que contiene
  experimento    -> la "profile card" de documents (502 textos)
  fragmento      -> fragmentos de reportes (documents, record_type = chunk)
  hallazgo       -> enunciado + cita textual
Embeddings (EMB_BACKEND):
  ollama   -> POST {EMB_URL}/api/embed     (p. ej. qwen3-embedding:0.6b, bge-m3)
  openai   -> POST {EMB_URL}/embeddings    (vLLM, llama.cpp --embedding, TEI, API externa)
  tfidf    -> respaldo SIN modelo: similitud lexica (no es semantica; se marca como tal)
Calibracion: el coseno crudo de cada modelo tiene otra escala. Al indexar se mide la distribucion
de cosenos entre pares aleatorios del corpus (media mu y percentil 99 p99) y se reporta
    s = clip((coseno - mu) / (p99 - mu), 0, 1)
que significa "0 = tan parecido como dos documentos al azar; 1 = tan parecido como el 1 % mas parecido".
"""
from __future__ import annotations

import json
import os
import re

import numpy as np
import requests

DIR_INDICE = os.environ.get("INDICE_DIR", "indice_embeddings")
INSTRUCCION = os.environ.get("EMB_INSTRUCCION",
                             "Given a description of a combustion experiment, dataset or scientific question, "
                             "retrieve related microgravity combustion experiments, datasets, findings and report passages")


# -------------------------------------------------------------------------------------------
# Embeddings
# -------------------------------------------------------------------------------------------
class Embedder:
    def __init__(self):
        self.backend = os.environ.get("EMB_BACKEND", "tfidf")
        self.url = os.environ.get("EMB_URL", "http://localhost:11434").rstrip("/")
        self.modelo = os.environ.get("EMB_MODEL", "qwen3-embedding:0.6b")
        self.usa_instruccion = os.environ.get("EMB_USA_INSTRUCCION", "1") == "1"   # Qwen3-Embedding: si; BGE-M3: no
        self._tfidf = None

    @property
    def es_semantico(self) -> bool:
        return self.backend != "tfidf"

    def ajustar_tfidf(self, textos):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self._tfidf = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), stop_words="english").fit(textos)

    def _http(self, textos: list[str]) -> np.ndarray:
        out = []
        for i in range(0, len(textos), 32):
            lote = textos[i:i + 32]
            if self.backend == "ollama":
                r = requests.post(self.url + "/api/embed", json={"model": self.modelo, "input": lote}, timeout=300)
                r.raise_for_status()
                out += r.json()["embeddings"]
            else:
                cab = {"Authorization": f"Bearer {os.environ['EMB_API_KEY']}"} if os.environ.get("EMB_API_KEY") else {}
                r = requests.post(self.url + "/embeddings", json={"model": self.modelo, "input": lote}, headers=cab, timeout=300)
                r.raise_for_status()
                out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        return np.asarray(out, dtype=np.float32)

    def documentos(self, textos: list[str]) -> np.ndarray:
        if self.backend == "tfidf":
            if self._tfidf is None:
                self.ajustar_tfidf(textos)
            M = self._tfidf.transform(textos).toarray().astype(np.float32)
        else:
            M = self._http(textos)
        return _normalizar(M)

    def consulta(self, texto: str) -> np.ndarray:
        if self.backend == "tfidf":
            return _normalizar(self._tfidf.transform([texto]).toarray().astype(np.float32))[0]
        t = f"Instruct: {INSTRUCCION}\nQuery: {texto}" if self.usa_instruccion else texto
        return _normalizar(self._http([t]))[0]


def _normalizar(M):
    n = np.linalg.norm(M, axis=1, keepdims=True)
    return M / np.where(n == 0, 1, n)


# -------------------------------------------------------------------------------------------
# Corpus
# -------------------------------------------------------------------------------------------
def construir_corpus(base) -> list[dict]:
    items = []
    for i in base.inv:
        items.append({"id": i, "tipo": "investigacion", "inv": i, "texto": base.texto_investigacion(i)})
    for ds_id, d in base.datasets.items():
        P = base.perfil_ds.get(ds_id, {"rangos": {}})
        vars_ = ", ".join(base.variables[v].label for v in P["rangos"])
        items.append({"id": ds_id, "tipo": "dataset", "inv": d["investigation_id"],
                      "texto": f"{d.get('title', '')}. {d.get('description', '')} Role: {d.get('data_role', '')}. "
                               f"Investigation {d['investigation_id']}: {base.inv.get(d['investigation_id'], {}).get('title', '')}. Variables: {vars_}."})
    for doc in base.col["documents"]:
        if doc.get("record_type") == "profile_card" and doc.get("text"):
            items.append({"id": (doc.get("experiment_ids") or [doc["_id"]])[0], "tipo": "experimento",
                          "inv": (doc.get("investigation_ids") or [None])[0], "texto": doc["text"]})
    for c in base.chunks:
        items.append({"id": c["_id"], "tipo": "fragmento", "inv": (c.get("investigation_ids") or [None])[0],
                      "texto": c["text"], "paginas": [c.get("page_start"), c.get("page_end")], "documento": c.get("parent_document_id")})
    for h in base.hallazgos:
        ev = (h.get("evidence") or [{}])[0]
        items.append({"id": h["_id"], "tipo": "hallazgo", "inv": h["investigation_id"],
                      "texto": f"{h['statement']} Evidence: {ev.get('quote', '')}", "pagina": ev.get("page"), "documento": ev.get("document_id")})
    return items


class IndiceSemantico:
    def __init__(self, base, emb: Embedder | None = None):
        self.b, self.emb = base, emb or Embedder()
        self.items = construir_corpus(base)
        self.pos = {it["id"]: k for k, it in enumerate(self.items)}

    # --- construccion (indexar.py) y carga --------------------------------------------------
    def construir(self, guardar: bool = True):
        textos = [it["texto"][:6000] for it in self.items]
        self.M = self.emb.documentos(textos)
        rng = np.random.default_rng(0)
        a, b = rng.integers(len(textos), size=4000), rng.integers(len(textos), size=4000)
        cos = np.einsum("ij,ij->i", self.M[a], self.M[b])[a != b]
        self.calib = {"mu": float(cos.mean()), "p99": float(np.quantile(cos, 0.99))}
        self.meta = {"backend": self.emb.backend, "modelo": self.emb.modelo if self.emb.es_semantico else "tfidf",
                     "n": len(textos), "dim": int(self.M.shape[1]), "calibracion": self.calib,
                     "ids": [it["id"] for it in self.items]}
        if guardar and self.emb.es_semantico:
            os.makedirs(DIR_INDICE, exist_ok=True)
            np.save(os.path.join(DIR_INDICE, "vectores.npy"), self.M)
            with open(os.path.join(DIR_INDICE, "meta.json"), "w") as f:
                json.dump(self.meta, f)
        return self

    def actualizar(self):
        """Incremental update after new records were added to the base: reuses the saved vector of every
        item that already had one (same id, same model) and only embeds the new ones. Then recalibrates and saves."""
        if not self.emb.es_semantico:
            return self.construir(guardar=False)
        ruta = os.path.join(DIR_INDICE, "meta.json")
        viejos = {}
        if os.path.exists(ruta):
            meta = json.load(open(ruta))
            if meta.get("modelo") == self.emb.modelo:
                M0 = np.load(os.path.join(DIR_INDICE, "vectores.npy"))
                viejos = {i: M0[k] for k, i in enumerate(meta["ids"])}
        faltan = [k for k, it in enumerate(self.items) if it["id"] not in viejos]
        nuevos = self.emb.documentos([self.items[k]["texto"][:6000] for k in faltan]) if faltan else None
        dim = nuevos.shape[1] if nuevos is not None else len(next(iter(viejos.values())))
        self.M = np.zeros((len(self.items), dim), dtype=np.float32)
        pos_nuevo = {k: j for j, k in enumerate(faltan)}
        for k, it in enumerate(self.items):
            self.M[k] = nuevos[pos_nuevo[k]] if k in pos_nuevo else viejos[it["id"]]
        rng = np.random.default_rng(0)
        n = len(self.items)
        a, b = rng.integers(n, size=4000), rng.integers(n, size=4000)
        cos = np.einsum("ij,ij->i", self.M[a], self.M[b])[a != b]
        self.calib = {"mu": float(cos.mean()), "p99": float(np.quantile(cos, 0.99))}
        self.meta = {"backend": self.emb.backend, "modelo": self.emb.modelo, "n": n, "dim": int(dim),
                     "calibracion": self.calib, "ids": [it["id"] for it in self.items], "nuevos_embebidos": len(faltan)}
        os.makedirs(DIR_INDICE, exist_ok=True)
        np.save(os.path.join(DIR_INDICE, "vectores.npy"), self.M)
        with open(os.path.join(DIR_INDICE, "meta.json"), "w") as f:
            json.dump(self.meta, f)
        return self

    def cargar_o_construir(self):
        ruta = os.path.join(DIR_INDICE, "meta.json")
        if self.emb.es_semantico and os.path.exists(ruta):
            meta = json.load(open(ruta))
            if meta["ids"] == [it["id"] for it in self.items] and meta["modelo"] == self.emb.modelo:
                self.M, self.meta, self.calib = np.load(os.path.join(DIR_INDICE, "vectores.npy")), meta, meta["calibracion"]
                return self
        if self.emb.es_semantico:
            raise RuntimeError("Indice semantico ausente o desactualizado: ejecute  python -m motor_psi.indexar")
        return self.construir(guardar=False)

    # --- consulta ------------------------------------------------------------------------------
    def calibrar(self, cos):
        mu, p99 = self.calib["mu"], self.calib["p99"]
        return np.clip((cos - mu) / max(p99 - mu, 1e-6), 0, 1)

    def buscar(self, texto: str, k: int = 20, tipos: set | None = None):
        q = self.emb.consulta(texto)
        cos = self.M @ q
        s = self.calibrar(cos)
        orden = np.argsort(-cos)
        out = []
        for i in orden:
            it = self.items[i]
            if tipos and it["tipo"] not in tipos:
                continue
            out.append({"id": it["id"], "tipo": it["tipo"], "inv": it["inv"], "coseno": round(float(cos[i]), 4),
                        "similitud_semantica": round(float(s[i]), 3)})
            if len(out) >= k:
                break
        return out, cos

    def afinidad_por_investigacion(self, cos: np.ndarray, top: int = 3) -> dict:
        """Media de los `top` elementos mas parecidos de cada investigacion (evita depender de un solo acierto)."""
        res = {}
        for inv in self.b.inv:
            idx = [k for k, it in enumerate(self.items) if it["inv"] == inv]
            mejores = np.sort(cos[idx])[::-1][:top]
            c = float(mejores.mean()) if len(mejores) else 0.0
            res[inv] = {"coseno_medio_top": round(c, 4), "similitud_semantica": round(float(self.calibrar(np.array([c]))[0]), 3)}
        return res


# -------------------------------------------------------------------------------------------
# Texto que describe lo que subio el usuario (para compararlo con las profile cards)
# -------------------------------------------------------------------------------------------
def describir_usuario(U: dict, base, texto_libre: str = "") -> str:
    partes = []
    if U.get("combustibles"):
        partes.append("Fuel: " + ", ".join(U["combustibles"]) + ".")
    if U.get("fenomenos"):
        partes.append("Phenomenon: " + ", ".join(f.replace("_", " ") for f in U["fenomenos"]) + ".")
    if U.get("gravedad"):
        partes.append("Gravity: " + ", ".join(U["gravedad"]).replace("_", " ") + ".")
    for v, r in (U.get("rangos") or {}).items():
        lab = base.variables[v].label
        partes.append(f"{lab}: {r['min']:.4g}" + (f" to {r['max']:.4g}" if r["max"] != r["min"] else "") + f" {r['unidad']}.")
    if texto_libre:
        partes.append(re.sub(r"\s+", " ", texto_libre)[:4000])
    return " ".join(partes).strip()
