"""
Resumen relacional en lenguaje natural.

Qwen3.5-9B (o cualquier modelo servido con API compatible con OpenAI: vLLM, SGLang, llama.cpp
server, Ollama) SOLO redacta a partir de los hechos que calculo el motor de afinidad. Despues:
  - cada [ID] citado debe existir en esos hechos;
  - cada numero escrito debe aparecer en los hechos (tolerancia de redondeo);
  - si algo falla, o el modelo no responde, se usa un resumen de plantilla deterministico.
Variables de entorno:
  La conexion con Qwen (QWEN_URL, QWEN_MODEL, LLM_BACKEND...) se configura en llm.py.
"""
from __future__ import annotations

import json
import os
import re


ID_RE = re.compile(r"\[([A-Za-z0-9_.:@+\-]+)\]")
NUM_RE = re.compile(r"(?<![A-Za-z0-9_\-:])(\d+(?:[.,]\d+)?)")

SISTEMA = """Eres un asistente de analisis de datos de combustion en microgravedad (NASA PSI).
Redactas en espanol un resumen relacional para un estudiante o investigador.
REGLAS ESTRICTAS:
1. Usa SOLO los hechos del JSON que recibes. No agregues conocimiento externo ni supongas datos.
2. Cita cada investigacion, experimento, dataset o hallazgo con su ID exacto entre corchetes, p. ej. [PSI-159] o [FND:PSI-159:001]. No inventes IDs.
3. No escribas numeros que no esten en el JSON. Puedes escribir el nivel de afinidad (alta, media, baja, nula).
4. Explica EN QUE SENTIDO hay afinidad (variables compartidas, rangos, combustible, gravedad, configuracion) y en que difiere.
5. Distingue similitud de transferibilidad: si hay extrapolacion o configuracion distinta, dilo.
6. Si no hay base para relacionar, dilo claramente.
7. Maximo 200 palabras. Formato: una frase de conclusion; luego un parrafo por investigacion relevante; al final las advertencias principales."""


def hechos_compactos(r: dict) -> dict:
    """Lo minimo que el modelo necesita (menos texto => menos oportunidad de inventar)."""
    inv = []
    for x in r["investigaciones"]:
        inv.append({"id": x["investigacion"], "titulo": x["titulo"], "afinidad": x["afinidad"], "nivel": x["nivel"],
                    "afinidad_semantica": x.get("afinidad_semantica"), "afinidad_total": x.get("afinidad_total"),
                    "nivel_total": x.get("nivel_total"),
                    "sentido": x["sentido"], "transferibilidad": x["transferibilidad"],
                    "configuracion_distinta": x.get("configuracion_distinta"),
                    "rangos": {k: {"estado": v["estado"], "usuario": [round(v["usuario"][0], 6), round(v["usuario"][1], 6)],
                                   "base": [round(v["base"][0], 6), round(v["base"][1], 6)], "unidad": v["unidad"]} for k, v in x["rangos"].items()},
                    "experimentos_cercanos": [v["experimento"] for v in x["vecinos"][:3]]})
    return {"datos_del_usuario": r["perfil_usuario"], "investigaciones": inv,
            "datasets_afines": [{"id": d["dataset"], "investigacion": d["investigacion"], "titulo": d["titulo"],
                                 "variables_compartidas": d["variables_compartidas"]} for d in r["datasets"][:4]],
            "hallazgos": [{"id": h["hallazgo"], "enunciado": h["enunciado"], "motivo": h["motivo"], "pagina": h["pagina"]} for h in r["hallazgos"][:4]],
            "relaciones_entre_investigaciones": r["relaciones_entre_investigaciones"],
            "advertencias": r["advertencias"][:5]}


def _ids_en(obj) -> set[str]:
    s = json.dumps(obj, ensure_ascii=False)
    ids = set(re.findall(r'"(?:id|investigacion)":\s*"([^"]+)"', s))
    ids |= set(re.findall(r'"(PSI-\d+(?::[^"]+)?)"', s))
    return ids


def _numeros_en(obj) -> list[float]:
    return [float(n.replace(",", ".")) for n in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", json.dumps(obj))]


def validar(texto: str, hechos: dict) -> list[str]:
    errores = []
    permitidos = _ids_en(hechos)
    for i in ID_RE.findall(texto):
        if i not in permitidos:
            errores.append(f"ID inexistente en los hechos: [{i}]")
    nums_hechos = _numeros_en(hechos)
    sin_ids = ID_RE.sub(" ", texto)
    for tok in NUM_RE.findall(sin_ids):
        n = float(tok.replace(",", "."))
        dec = len(tok.replace(",", ".").split(".")[1]) if "." in tok.replace(",", ".") else 0
        tol = 0.5 * 10 ** (-dec)
        if n <= 10 and dec == 0:
            continue  # conteos pequenos ("3 investigaciones", "2 variables")
        if not any(abs(n - f) <= max(tol, 0.01 * abs(f)) or abs(n / 100 - f) <= max(tol / 100, 0.01 * abs(f)) for f in nums_hechos):
            errores.append(f"numero sin respaldo en los hechos: {tok}")
    if len(texto.split()) > 320:
        errores.append("resumen demasiado largo")
    return errores


def plantilla(r: dict) -> str:
    U = r["perfil_usuario"]
    afines = [x for x in r["investigaciones"] if x["nivel"] in ("alta", "media", "baja")]
    desc = []
    if U["variables"]:
        desc.append(f"{len(U['variables'])} variable(s) reconocida(s)")
    if U["combustibles"]:
        desc.append("combustible: " + ", ".join(U["combustibles"]))
    if U["fenomenos"]:
        desc.append("fenomeno: " + ", ".join(U["fenomenos"]))
    if not afines:
        return ("No encontre base suficiente para relacionar tus datos con las investigaciones disponibles. "
                + " ".join(r["advertencias"][:2]))
    partes = [f"Tus datos ({'; '.join(desc) or 'sin variables reconocidas'}) se relacionan sobre todo con "
              f"[{afines[0]['investigacion']}] (afinidad {afines[0]['nivel']})."]
    for x in afines[:3]:
        linea = f"[{x['investigacion']}] — afinidad {x['nivel']}: " + "; ".join(x["sentido"][:4]) + f". Transferibilidad: {x['transferibilidad']}."
        if x["vecinos"]:
            linea += " Experimentos mas cercanos: " + ", ".join(f"[{v['experimento']}]" for v in x["vecinos"][:2]) + "."
        partes.append(linea)
    if r["hallazgos"]:
        h = r["hallazgos"][0]
        partes.append(f"Hallazgo relacionado [{h['hallazgo']}]: {h['enunciado']}")
    partes.append("Advertencia: " + r["advertencias"][-1])
    return "\n\n".join(partes)


def llamar_qwen(hechos: dict) -> str:
    from . import llm
    return llm.generar(SISTEMA, "Hechos (JSON):\n" + json.dumps(hechos, ensure_ascii=False))


def resumir(r: dict, generador=None) -> dict:
    """generador: funcion(hechos) -> texto. Por defecto Qwen si QWEN_URL esta definido."""
    hechos = hechos_compactos(r)
    if generador is None and os.environ.get("QWEN_URL"):  # ver llm.py
        generador = llamar_qwen
    if generador is None:
        return {"texto": plantilla(r), "fuente": "plantilla", "motivo": "LLM no configurado (QWEN_URL)"}
    try:
        texto = generador(hechos)
    except Exception as e:  # red caida, timeout, modelo no cargado...
        return {"texto": plantilla(r), "fuente": "plantilla", "motivo": f"LLM no disponible: {type(e).__name__}"}
    errores = validar(texto, hechos)
    if errores:
        return {"texto": plantilla(r), "fuente": "plantilla", "motivo": "respuesta del LLM rechazada", "errores": errores[:5],
                "texto_rechazado": texto[:1500]}
    return {"texto": texto, "fuente": "qwen", "motivo": "verificado: IDs y numeros respaldados por los hechos"}
