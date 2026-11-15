"""
Recomendaciones en texto para lo que el usuario escribe o sube.

1. El CODIGO decide que recomendar (hechos verificables, cada uno con IDs):
     consultar_dataset      datasets mas afines y por que
     comparar_experimento   experimentos vecinos, en que difieren y que resultado tuvieron
     medir_variable         variables que la investigacion mide y el usuario no reporto
     revisar_extrapolacion  variables del usuario fuera del rango estudiado
     leer_hallazgo          hallazgos publicados relacionados (con pagina)
     revisar_datos          columnas sin asignar, unidades dudosas
2. QWEN las redacta en lenguaje natural con salida JSON obligatoria (JSON Schema).
3. Se VERIFICA cada recomendacion: sus IDs deben estar en los hechos y sus numeros tambien.
   Las que fallan se descartan (no toda la respuesta); si no queda ninguna, se usa la redaccion
   de plantilla, que siempre esta disponible.
"""
from __future__ import annotations

import json

from . import llm
from .narrador import validar

TIPOS = ["consultar_dataset", "comparar_experimento", "medir_variable", "revisar_extrapolacion", "leer_hallazgo", "revisar_datos"]

ESQUEMA = {
    "type": "object",
    "properties": {
        "resumen": {"type": "string"},
        "recomendaciones": {"type": "array", "items": {
            "type": "object",
            "properties": {"tipo": {"type": "string", "enum": TIPOS}, "titulo": {"type": "string"},
                           "detalle": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["tipo", "titulo", "detalle", "ids"]}},
        "limitaciones": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["resumen", "recomendaciones", "limitaciones"],
}

SISTEMA = """Eres el asistente cientifico del proyecto Flame in Freefall (combustion en microgravedad, NASA PSI).
Recibes los datos del usuario ya analizados y una lista de ACCIONES calculadas por el sistema.
Redacta recomendaciones practicas en espanol para el usuario.
REGLAS:
1. Usa SOLO las acciones y hechos del JSON. No inventes datasets, experimentos, cifras ni conclusiones.
2. Cada recomendacion debe incluir en "ids" los IDs exactos de las acciones en que se basa, y citarlos en el texto entre corchetes.
3. No escribas numeros que no aparezcan en el JSON.
4. Ordena de lo mas util a lo menos util; maximo 6 recomendaciones; cada "detalle" en 1 a 3 frases.
5. Explica en que sentido es util cada recomendacion y que limite tiene. Similitud no garantiza el mismo resultado.
6. "resumen": 2 frases sobre con que se relacionan los datos del usuario. "limitaciones": advertencias importantes.
Responde solo con el JSON pedido."""


def acciones(r: dict, rk: dict, base) -> list[dict]:
    """Recomendaciones deterministas a partir de la afinidad y el ranking."""
    out = []
    invs = [x for x in r["investigaciones"] if (x.get("afinidad_total") or 0) >= 0.15]
    U = r["perfil_usuario"]
    # datasets: primero los de las investigaciones mas afines (alta/media si las hay)
    orden_inv = {x["investigacion"]: k for k, x in enumerate(r["investigaciones"])}
    buenas = {x["investigacion"] for x in r["investigaciones"] if x.get("nivel_total") in ("alta", "media")} or {x["investigacion"] for x in invs[:1]}
    ds = sorted([d for d in r["datasets"] if d["investigacion"] in buenas],
                key=lambda d: (orden_inv[d["investigacion"]], -d["vecinos_en_top"], -(d["solape_rangos"] or 0)))
    for d in ds[:3]:
        out.append({"id": d["dataset"], "tipo": "consultar_dataset", "investigacion": d["investigacion"], "titulo": d["titulo"],
                    "variables_compartidas": d["variables_compartidas"], "solape_rangos": d["solape_rangos"],
                    "experimentos_cercanos_en_el_dataset": d["vecinos_en_top"]})
    for x in invs[:2]:
        for v in x["vecinos"][:2]:
            out.append({"id": v["experimento"], "tipo": "comparar_experimento", "investigacion": x["investigacion"],
                        "similitud": v["similitud"], "diferencias_en_sigmas": v["diferencias_sigmas"],
                        "resultados_medidos": {k: val for k, val in (v.get("resultados") or {}).items() if val is not None},
                        "combustible": v.get("combustible"), "dataset": v.get("dataset")})
        P = base.perfil_inv[x["investigacion"]]
        n = max(P["n_experimentos"], 1)
        faltan = [(base.variables[v].label, round(rr["n"] / n, 2)) for v, rr in P["rangos"].items()
                  if v not in U["variables"] and rr["n"] / n >= 0.5 and base.variables[v].role == "condition"]
        if faltan and U["variables"]:
            out.append({"id": x["investigacion"], "tipo": "medir_variable", "investigacion": x["investigacion"],
                        "variables_que_la_investigacion_reporta_y_tu_no": [f[0] for f in faltan[:5]],
                        "fraccion_de_experimentos_que_la_reportan": dict(faltan[:5])})
        for var, d in x["rangos"].items():
            if d["estado"] == "fuera":
                out.append({"id": x["investigacion"], "tipo": "revisar_extrapolacion", "investigacion": x["investigacion"],
                            "variable": base.variables[var].label, "tu_rango": d["usuario"], "rango_estudiado": d["base"],
                            "unidad": d["unidad"], "distancia_en_sigmas": d["distancia_sigmas"]})
    for h in r["hallazgos"][:2]:
        out.append({"id": h["hallazgo"], "tipo": "leer_hallazgo", "investigacion": h["investigacion"], "enunciado": h["enunciado"],
                    "pagina": h["pagina"], "documento": h["documento"], "motivo": h["motivo"]})
    for f in [x for x in rk["resultados"] if x["tipo"] == "fragmento"][:1]:
        out.append({"id": f["id"], "tipo": "leer_hallazgo", "investigacion": f["investigacion"], "extracto": f["extracto"], "pagina": f["pagina"]})
    sin = [c["columna"] for c in r.get("columnas", []) if c["estado"] in ("sin_asignar", "unidad_desconocida", "sugerida")]
    if sin:
        out.append({"id": "COLUMNAS", "tipo": "revisar_datos", "columnas_a_revisar": sin[:10]})
    return out


def plantilla(acc: list[dict], r: dict) -> dict:
    textos = {
        "consultar_dataset": lambda a: (f"Consulta el dataset [{a['id']}]",
                                        f"Comparte {', '.join(a['variables_compartidas']) or 'contexto'} con tus datos."),
        "comparar_experimento": lambda a: (f"Compara con el experimento [{a['id']}]",
                                           "Es de los mas cercanos a tus condiciones; revisa en que variables difiere."),
        "medir_variable": lambda a: (f"Agrega variables para comparar con [{a['id']}]",
                                     "Esa investigacion reporta: " + ", ".join(a["variables_que_la_investigacion_reporta_y_tu_no"]) + "."),
        "revisar_extrapolacion": lambda a: (f"Cuidado: {a['variable']} fuera del rango de [{a['id']}]",
                                            "Tus valores estan fuera de lo estudiado; los resultados podrian no aplicar."),
        "leer_hallazgo": lambda a: (f"Lee [{a['id']}]", a.get("enunciado") or a.get("extracto", "")[:200]),
        "revisar_datos": lambda a: ("Revisa columnas de tus archivos", "No se pudieron asignar con seguridad: " + ", ".join(a["columnas_a_revisar"]) + "."),
    }
    recs = [{"tipo": a["tipo"], "titulo": textos[a["tipo"]](a)[0], "detalle": textos[a["tipo"]](a)[1], "ids": [a["id"]]} for a in acc[:6]]
    top = r["investigaciones"][0]
    return {"resumen": f"Tus datos se relacionan sobre todo con [{top['investigacion']}] (afinidad {top.get('nivel_total', top['nivel'])}).",
            "recomendaciones": recs, "limitaciones": r["advertencias"][-2:]}


def recomendar(srv, entradas: list[dict], texto: str = "") -> dict:
    r = srv.afinidad(entradas, texto)
    r["columnas"] = [{"columna": a["columna"], "estado": a["estado"]} for e in entradas for a in e["asignaciones"]]
    rk = srv.ranking(texto, entradas, k=10)
    acc = acciones(r, rk, srv.b)
    afin = [{"investigacion": x["investigacion"], "afinidad_total": x.get("afinidad_total"), "nivel": x.get("nivel_total"),
             "sentido": x["sentido"][:4], "transferibilidad": x["transferibilidad"]} for x in r["investigaciones"]]
    hechos = {"datos_del_usuario": r["perfil_usuario"], "afinidad": afin, "acciones": acc, "advertencias": r["advertencias"][:4]}
    salida = {"acciones_calculadas": acc, "afinidad": afin, "ranking": rk["resultados"]}
    if not acc:
        salida.update(fuente="sin_acciones", texto={"resumen": "No encontre base suficiente para recomendar algo concreto.",
                                                   "recomendaciones": [], "limitaciones": r["advertencias"]})
        return salida
    if not llm.configurado():
        salida.update(fuente="plantilla", motivo="LLM no configurado", texto=plantilla(acc, r))
        return salida
    try:
        crudo = llm.generar(SISTEMA, "HECHOS Y ACCIONES (JSON):\n" + json.dumps(hechos, ensure_ascii=False),
                            max_tokens=1200, esquema=ESQUEMA)
        resp = json.loads(crudo)
    except Exception as e:
        salida.update(fuente="plantilla", motivo=f"LLM no disponible o JSON invalido: {type(e).__name__}", texto=plantilla(acc, r))
        return salida
    ids_ok = {a["id"] for a in acc} | {x["investigacion"] for x in afin}
    hechos_val = {"investigaciones": [{"id": i} for i in ids_ok], "datos": hechos}
    aceptadas, descartadas = [], []
    for rec in resp.get("recomendaciones", [])[:6]:
        errores = [f"ID sin respaldo: {i}" for i in rec.get("ids", []) if i not in ids_ok]
        if not rec.get("ids") and rec.get("tipo") != "revisar_datos":
            errores.append("sin IDs")
        errores += validar(f"{rec.get('titulo', '')} {rec.get('detalle', '')}", hechos_val)
        (descartadas if errores else aceptadas).append({**rec, **({"errores": errores} if errores else {})})
    resumen_ok = not validar(resp.get("resumen", ""), hechos_val)
    if not aceptadas:
        salida.update(fuente="plantilla", motivo="todas las recomendaciones de Qwen fallaron la verificacion",
                      texto=plantilla(acc, r), descartadas=descartadas)
        return salida
    salida.update(fuente="qwen", motivo="cada recomendacion verificada contra las acciones calculadas",
                  texto={"resumen": resp.get("resumen") if resumen_ok else plantilla(acc, r)["resumen"],
                         "recomendaciones": aceptadas,
                         "limitaciones": [l for l in resp.get("limitaciones", []) if not validar(l, hechos_val)][:4]},
                  descartadas=descartadas)
    return salida
