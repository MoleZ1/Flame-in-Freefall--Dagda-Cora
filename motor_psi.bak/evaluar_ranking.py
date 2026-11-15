"""
Evalua el ranking contra juicios de relevancia hechos por el equipo (la unica forma honesta de
saber si un cambio mejora o empeora).

juicios.csv  (una fila por par consulta-elemento juzgado):
    consulta,id,relevancia
    "gota de n-heptano 0.5 mm 1 atm 21% O2 microgravedad",PSI-117,3
    "gota de n-heptano 0.5 mm 1 atm 21% O2 microgravedad",DS:PSI-117:PROCI_2019_Stable_Cool,3
    "gota de n-heptano 0.5 mm 1 atm 21% O2 microgravedad",PSI-159,1
relevancia: 0 = nada, 1 = algo, 2 = util, 3 = muy util.

Compara cuatro variantes con las MISMAS consultas: solo lexica, solo semantica, solo numerica e hibrida (RRF).
Metricas: nDCG@5, nDCG@10, Recall@10. Reporta media y desviacion por consulta: si la diferencia entre
variantes es menor que esa desviacion, no hay evidencia de mejora.

    python -m motor_psi.evaluar_ranking juicios.csv
"""
import csv
import math
import os
import sys
from collections import defaultdict

import numpy as np

from .servicio import Servicio


def dcg(rels):
    return sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(rels))


def ndcg(orden, juicio, k):
    ideal = dcg(sorted(juicio.values(), reverse=True)[:k])
    return dcg([juicio.get(i, 0) for i in orden[:k]]) / ideal if ideal > 0 else 0.0


def recall(orden, juicio, k):
    rel = {i for i, r in juicio.items() if r >= 2}
    return len(rel & set(orden[:k])) / len(rel) if rel else 0.0


juicios = defaultdict(dict)
with open(sys.argv[1], encoding="utf-8") as f:
    for row in csv.DictReader(f):
        juicios[row["consulta"]][row["id"]] = int(row["relevancia"])
srv = Servicio()
res = defaultdict(lambda: defaultdict(list))
for consulta, juicio in juicios.items():
    variantes = {"hibrida_rrf": [x["id"] for x in srv.ranking(consulta, k=50)["resultados"]]}
    for lista in ("semantica", "lexica", "numerica"):
        variantes[f"solo_{lista}"] = [x["id"] for x in srv.ranking(consulta, k=50, usar=(lista,))["resultados"]]
    for nombre, orden in variantes.items():
        res[nombre]["nDCG@5"].append(ndcg(orden, juicio, 5))
        res[nombre]["nDCG@10"].append(ndcg(orden, juicio, 10))
        res[nombre]["Recall@10"].append(recall(orden, juicio, 10))
print(f"{len(juicios)} consultas juzgadas")
for nombre, m in res.items():
    print(nombre.ljust(14), "  ".join(f"{k}={np.mean(v):.3f}±{np.std(v):.3f}" for k, v in m.items()))
