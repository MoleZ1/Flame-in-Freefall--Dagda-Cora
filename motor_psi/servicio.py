"""
Capa de servicio: une los motores y ofrece tres usos de la base.

  afinidad(...)  -> para cada investigacion: afinidad NUMERICA (fisica), SEMANTICA (embeddings) y total
  ranking(...)   -> lista ordenada de investigaciones, datasets, experimentos, fragmentos y hallazgos
  chat(...)      -> respuesta en lenguaje natural de Qwen, solo con fuentes recuperadas y verificada

Afinidad total (pesos PROVISIONALES, sin calibrar):
    A_total = ALFA * A_numerica + (1 - ALFA) * A_semantica    si hay ambas
    A_total = la que exista                                   si solo hay una
"""
from __future__ import annotations

import os

import numpy as np

from . import llm
from .afinidad import Afinidad, nivel
from .base import obtener_base
from .ingesta import interpretar_texto
from .narrador import validar
from .ranking import BM25, rrf
from .semantica import IndiceSemantico, describir_usuario

ALFA = float(os.environ.get("ALFA_NUMERICA", 0.6))
LAMBDA_MMR = float(os.environ.get("LAMBDA_MMR", 0.7))


class Servicio:
    def __init__(self, actualizar_indice: bool = False):
        self.b = obtener_base()
        self.num = Afinidad(self.b)
        idx = IndiceSemantico(self.b)
        self.sem = idx.actualizar() if actualizar_indice else idx.cargar_o_construir()
        self.bm25 = BM25([it["texto"] for it in self.sem.items])

    # ------------------------------------------------------------------------------------------
    def _consulta(self, texto: str):
        if not texto:
            return None
        q = interpretar_texto(texto, self.b)
        q["texto"] = texto
        return q

    def afinidad(self, entradas: list[dict], texto: str = "", consulta: dict | None = None) -> dict:
        """consulta: escenario ya interpretado (p. ej. confirmado por el usuario). Si no se da, se interpreta 'texto'."""
        if consulta is None:
            consulta = self._consulta(texto)
        r = self.num.relacionar(entradas, consulta)
        U = self.num.perfil_usuario(entradas, consulta)
        texto_libre = " ".join([texto] + [e["texto"][:3000] for e in entradas if e.get("texto")])
        descripcion = describir_usuario(U, self.b, texto_libre)
        sem_por_inv, top_sem = {}, []
        if descripcion:
            top_sem, cos = self.sem.buscar(descripcion, k=10)
            sem_por_inv = self.sem.afinidad_por_investigacion(cos)
        for x in r["investigaciones"]:
            a_num = x["afinidad"]
            a_sem = sem_por_inv.get(x["investigacion"], {}).get("similitud_semantica") if sem_por_inv else None
            if a_num is not None and a_sem is not None:
                tot = ALFA * a_num + (1 - ALFA) * a_sem
            else:
                tot = a_num if a_num is not None else a_sem
            x["afinidad_numerica"], x["afinidad_semantica"] = a_num, a_sem
            x["afinidad_total"] = None if tot is None else round(tot, 3)
            x["nivel_total"] = nivel(tot)
        r["investigaciones"].sort(key=lambda x: -(x["afinidad_total"] or 0))
        r["semantica"] = {"backend": self.sem.meta["backend"], "modelo": self.sem.meta["modelo"],
                          "es_semantica": self.sem.emb.es_semantico, "texto_comparado": descripcion[:600],
                          "mas_parecidos": top_sem, "calibracion": self.sem.calib}
        r["parametros"]["alfa_numerica"] = ALFA
        if not self.sem.emb.es_semantico:
            r["advertencias"].append("Embeddings no configurados: la 'afinidad semantica' es lexica (TF-IDF). Configure EMB_BACKEND.")
        return r

    # ------------------------------------------------------------------------------------------
    def ranking(self, texto: str, entradas: list[dict] | None = None, k: int = 10, tipos: set | None = None,
                usar: tuple = ("semantica", "lexica", "numerica"), diversificar: bool = True) -> dict:
        entradas = entradas or []
        r = self.afinidad(entradas, texto)
        descripcion = r["semantica"]["texto_comparado"] or texto
        items = self.sem.items
        # lista semantica
        _, cos = self.sem.buscar(descripcion, k=1)
        orden_sem = [items[i]["id"] for i in np.argsort(-cos)]
        # lista lexica
        bm = self.bm25.puntuar(descripcion)
        orden_lex = [items[i]["id"] for i in np.argsort(-bm) if bm[i] > 0]
        # lista numerica: experimentos vecinos, sus datasets y las investigaciones por afinidad numerica
        num = {}
        for x in r["investigaciones"]:
            if x["afinidad_numerica"]:
                num[x["investigacion"]] = x["afinidad_numerica"]
            for v in x["vecinos"]:
                num[v["experimento"]] = max(num.get(v["experimento"], 0), v["similitud"])
                if v.get("dataset"):
                    num[v["dataset"]] = max(num.get(v["dataset"], 0), v["similitud"] * 0.99)
        orden_num = [i for i, _ in sorted(num.items(), key=lambda t: -t[1]) if i in self.sem.pos]
        listas = {"semantica": orden_sem[:200], "lexica": orden_lex[:200], "numerica": orden_num}
        fus = rrf({n: l for n, l in listas.items() if l and n in usar})
        pos = {n: {i: p + 1 for p, i in enumerate(l)} for n, l in listas.items()}
        cal = self.sem.calibrar(cos)
        bm_max = float(bm.max()) if bm.max() > 0 else 1.0
        cands = [(i, sc) for i, sc in sorted(fus.items(), key=lambda t: -t[1])
                 if not tipos or items[self.sem.pos[i]]["tipo"] in tipos][:60]
        orden = self._mmr(cands, k) if diversificar else [i for i, _ in cands[:k]]
        res = []
        for i in orden:
            it, j = items[self.sem.pos[i]], self.sem.pos[i]
            res.append({"id": i, "tipo": it["tipo"], "investigacion": it["inv"], "puntaje_rrf": round(fus[i], 5),
                        "similitud_semantica": round(float(cal[j]), 3), "bm25_relativo": round(float(bm[j] / bm_max), 3),
                        "similitud_numerica": num.get(i), "posiciones": {n: pos[n].get(i) for n in listas},
                        "extracto": it["texto"][:240], "pagina": it.get("pagina") or it.get("paginas")})
        return {"consulta": texto, "texto_comparado": descripcion[:600], "resultados": res,
                "afinidad_por_investigacion": [{k2: x[k2] for k2 in ("investigacion", "afinidad_total", "nivel_total", "afinidad_numerica", "afinidad_semantica")}
                                               for x in r["investigaciones"]],
                "metodo": "RRF(K=60) de listas semantica, lexica (BM25) y numerica" + (f" + MMR(lambda={LAMBDA_MMR})" if diversificar else ""), "advertencias": r["advertencias"]}

    def _mmr(self, cands, k):
        """Maximal Marginal Relevance: evita que el top-k sean corridas casi iguales del mismo dataset.
        puntaje = lambda * relevancia - (1 - lambda) * max(similitud con lo ya elegido)."""
        if not cands:
            return []
        ids = [i for i, _ in cands]
        rel = np.array([s for _, s in cands]); rel = (rel - rel.min()) / (rel.max() - rel.min() + 1e-12)
        V = self.sem.M[[self.sem.pos[i] for i in ids]]
        S = V @ V.T
        elegidos = [0]
        while len(elegidos) < min(k, len(ids)):
            resto = [t for t in range(len(ids)) if t not in elegidos]
            pen = S[np.ix_(resto, elegidos)].max(axis=1)
            puntaje = LAMBDA_MMR * rel[resto] - (1 - LAMBDA_MMR) * pen
            elegidos.append(resto[int(np.argmax(puntaje))])
        return [ids[t] for t in elegidos]

    # ------------------------------------------------------------------------------------------
    SISTEMA_CHAT = """Eres el asistente cientifico del proyecto Flame in Freefall (combustion en microgravedad, NASA PSI).
Respondes en espanol, de forma breve y precisa.
REGLAS:
1. Responde SOLO con la informacion de las FUENTES y de la AFINIDAD que recibes. Si la respuesta no esta ahi, di "No encuentro eso en la base".
2. Cita cada afirmacion con el ID exacto de la fuente entre corchetes, p. ej. [PSI-159:normal:21088A1] o [FND:PSI-159:001]. No inventes IDs.
3. No escribas numeros que no aparezcan en las fuentes o en la afinidad.
4. Distingue lo medido de la interpretacion de los autores. Similitud no implica que el resultado sea transferible.
5. Maximo 220 palabras."""

    def chat(self, pregunta: str, entradas: list[dict] | None = None, k: int = 8) -> dict:
        rk = self.ranking(pregunta, entradas, k=k)
        fuentes = []
        for x in rk["resultados"]:
            it = self.sem.items[self.sem.pos[x["id"]]]
            fuentes.append({"id": x["id"], "tipo": x["tipo"], "investigacion": x["investigacion"], "texto": it["texto"][:700],
                            "pagina": x["pagina"]})
        afin = rk["afinidad_por_investigacion"]
        hechos = {"pregunta": pregunta, "afinidad": afin, "fuentes": fuentes}
        if not llm.configurado():
            return {"respuesta": None, "fuente": "sin_llm", "motivo": "QWEN_URL no configurado", "fuentes": fuentes, "afinidad": afin}
        import json
        try:
            texto = llm.generar(self.SISTEMA_CHAT, "AFINIDAD Y FUENTES (JSON):\n" + json.dumps(hechos, ensure_ascii=False)
                                + f"\n\nPREGUNTA: {pregunta}")
        except Exception as e:
            return {"respuesta": None, "fuente": "error_llm", "motivo": f"{type(e).__name__}: {str(e)[:150]}", "fuentes": fuentes, "afinidad": afin}
        errores = validar(texto, {"investigaciones": [{"id": a["investigacion"]} for a in afin] + [{"id": f["id"]} for f in fuentes],
                                  "datos": hechos})
        if errores:
            return {"respuesta": None, "fuente": "rechazada", "motivo": "la respuesta cito IDs o numeros sin respaldo",
                    "errores_validacion": errores[:5], "texto_rechazado": texto[:1500], "fuentes": fuentes, "afinidad": afin}
        return {"respuesta": texto, "fuente": "qwen", "motivo": "verificada contra las fuentes", "fuentes": fuentes, "afinidad": afin}
