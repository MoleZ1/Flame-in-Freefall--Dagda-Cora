"""
Ranking hibrido: lexico (BM25) + semantico (embeddings) + numerico (vecinos fisicos).

Cada lista produce un orden; se combinan con Reciprocal Rank Fusion (RRF):
    rrf(d) = sum_listas 1 / (K + rango_lista(d)),   K = 60
RRF no necesita que las puntuaciones esten en la misma escala, no tiene parametros que entrenar
(salvo K, que se deja en el valor estandar) y por eso no sobreajusta con pocos datos.
"""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

K_RRF = 60
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-\.]*")
_STOP = set("the a an of and or in on for to with by at from is are was were be this that these those as it its into than then".split())


def tokenizar(t: str) -> list[str]:
    return [w for w in _TOKEN.findall(t.lower()) if w not in _STOP and len(w) > 1]


class BM25:
    def __init__(self, textos: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [Counter(tokenizar(t)) for t in textos]
        self.len = np.array([sum(d.values()) for d in self.docs], dtype=float)
        self.avg = float(self.len.mean()) if len(self.len) else 1.0
        df = Counter(w for d in self.docs for w in d)
        N = len(self.docs)
        self.idf = {w: math.log(1 + (N - n + 0.5) / (n + 0.5)) for w, n in df.items()}
        self.k1, self.b = k1, b

    def puntuar(self, consulta: str) -> np.ndarray:
        q = tokenizar(consulta)
        s = np.zeros(len(self.docs))
        for i, d in enumerate(self.docs):
            norm = self.k1 * (1 - self.b + self.b * self.len[i] / self.avg)
            for w in q:
                f = d.get(w)
                if f:
                    s[i] += self.idf[w] * f * (self.k1 + 1) / (f + norm)
        return s


def rrf(listas: dict[str, list[str]], k: int = K_RRF) -> dict[str, float]:
    puntaje = {}
    for nombre, ids in listas.items():
        for r, i in enumerate(ids, start=1):
            puntaje[i] = puntaje.get(i, 0.0) + 1.0 / (k + r)
    return puntaje
