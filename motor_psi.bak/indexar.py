"""
Precalcula los embeddings de toda la base (investigaciones, datasets, experimentos, fragmentos, hallazgos).
Ejecutar cada vez que cambie la base o el modelo de embeddings:

    EMB_BACKEND=ollama EMB_URL=http://localhost:11434 EMB_MODEL=qwen3-embedding:0.6b python -m motor_psi.indexar

Guarda indice_embeddings/vectores.npy y meta.json. Con  --mongo  tambien escribe la coleccion
"embeddings" (un documento por elemento: _id, tipo, investigacion, modelo, vector).
"""
import os
import sys
import time


from .base import obtener_base
from .semantica import Embedder, IndiceSemantico

emb = Embedder()
if not emb.es_semantico:
    sys.exit("EMB_BACKEND no configurado (ollama u openai). Con tfidf no hace falta indexar.")
t0 = time.time()
idx = IndiceSemantico(obtener_base(), emb).construir(guardar=True)
print(f"{idx.meta['n']} elementos, dimension {idx.meta['dim']}, modelo {idx.meta['modelo']}, "
      f"calibracion mu={idx.calib['mu']:.3f} p99={idx.calib['p99']:.3f}, {time.time() - t0:.1f}s")
if "--mongo" in sys.argv:
    from pymongo import MongoClient, ReplaceOne
    db = MongoClient(os.environ["MONGO_URI"])[os.environ.get("MONGO_DB", "psi_science")]
    ops = [ReplaceOne({"_id": f"{idx.meta['modelo']}::{it['id']}"},
                      {"_id": f"{idx.meta['modelo']}::{it['id']}", "item_id": it["id"], "tipo": it["tipo"], "investigacion": it["inv"],
                       "modelo": idx.meta["modelo"], "vector": idx.M[k].tolist()}, upsert=True) for k, it in enumerate(idx.items)]
    db.embeddings.bulk_write(ops)
    db.embeddings.create_index([("modelo", 1), ("tipo", 1)])
    print(f"MongoDB: {len(ops)} vectores en {db.name}.embeddings")
