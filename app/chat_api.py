"""
/api/chat routes for suggestion.html / suggestion.js.

Adapter between motor_psi and the contract that suggestion.js already expects:
    POST /api/chat/interpret  {message}                    -> {scenario, summary, problems}
    POST /api/chat/answer     {scenario, confirmed: true}  -> {answer, results, abstained, recommendation_id, ...}
    POST /api/chat/feedback   {recommendation_id, experiment_id, grade}

Rules kept from motor_psi:
  * Every number in "results" comes from the database. Qwen never computes them.
  * Qwen only writes the "answer" text. Each statement is checked: its citations must be IDs that were
    given to Qwen and its numbers must appear in those facts. Unsupported statements are marked supported=false
    (suggestion.js hides them and says how many were removed).
  * If Qwen fails or is not configured, a deterministic answer is built from the same facts.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from motor_psi import llm
from motor_psi.afinidad import GRAV_MAPA
from motor_psi.narrador import validar

chat_api = Blueprint("chat_api", __name__, url_prefix="/api/chat")

MAX_RESULTS = 10
MAX_FACT_EXPERIMENTS = 6
MAX_FACT_FINDINGS = 4

# Motor phenomenon codes -> variable names that suggestion.js already labels ("Looking for: ...")
PHENOMENON_TO_OUTPUT = {"cool_flame": "cool_flame_observed"}
OUTPUT_TO_PHENOMENON = {v: k for k, v in PHENOMENON_TO_OUTPUT.items()}


# ----------------------------------------------------------------------------------- helpers
def _motor():
    return current_app.extensions.get("motor")


def _unavailable():
    return jsonify(error="The suggestion engine is not available: " + str(current_app.extensions.get("motor_error"))), 503


def _experiment_index(motor):
    """Raw experiment documents, as loaded by the motor (same data the scores were computed on)."""
    idx = current_app.extensions.get("experiment_index")
    if idx is None:
        idx = {e["_id"]: e for e in motor.srv.b.col["experiments"]}
        current_app.extensions["experiment_index"] = idx
    return idx


def _label(motor, var):
    v = motor.srv.b.variables.get(var)
    return v.label if v else var.replace("_", " ")


def _display(var, value, unit):
    """Human units for facts sent to Qwen (same conversions as suggestion.js)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    if unit == "Pa":
        return f"{value / 1000:.1f} kPa"
    if unit == "1" and "fraction" in var:
        return f"{value * 100:.1f}%"
    if unit == "kg/s":
        return f"{value * 1e6:.2f} mg/s"
    if unit == "m":
        return f"{value * 1000:.2f} mm"
    return f"{value:.4g} {unit if unit and unit != '1' else ''}".strip()


def _first_reported(records, var):
    for r in records or []:
        if r.get("variable") == var and r.get("status") == "reported":
            return r
    return None


def _kv_list(motor, records):
    out, seen = [], set()
    for r in records or []:
        if r.get("status") != "reported":
            continue
        norm = r.get("norm") or {}
        key = (r.get("variable"), r.get("qualifier"))
        if key in seen or norm.get("value") is None:
            continue
        seen.add(key)
        lab = _label(motor, r["variable"]) + (f" ({r['qualifier'].replace('_', ' ')})" if r.get("qualifier") else "")
        out.append({"label": lab, "value": norm.get("value"), "unit": norm.get("unit"),
                    "approximate": r.get("kind") == "approximate"})
    return out


def _quality_score(doc):
    q = doc.get("quality") or {}
    parts = [q.get("conditions_complete"), q.get("method_described"), q.get("peer_reviewed_source"), q.get("raw_data_available")]
    parts = [float(p) for p in parts if isinstance(p, (int, float, bool))]
    return round(sum(parts) / len(parts), 3) if parts else None


# --------------------------------------------------------------- scenario <-> motor consulta
def _scenario_from_consulta(motor, message, c):
    conditions = []
    for var, v in c["condiciones"].items():
        unit = motor.srv.b.variables[var].canonical_unit if var in motor.srv.b.variables else None
        conditions.append({"variable": var, "kind": "point",
                           "raw": {"value": v["texto"], "unit": ""},           # what the user wrote, e.g. "2 bar"
                           "norm": {"value": v["valor"], "unit": unit}})        # canonical value used for matching
    for g in c["gravedad"]:
        conditions.append({"variable": "gravity_environment", "kind": "categorical", "norm": {"value": g}})
    structured = {
        "fuel": c["combustibles"][0] if c["combustibles"] else None,
        "other_fuels": c["combustibles"][1:],
        "configuration": {},
        "conditions": conditions,
        "outputs_of_interest": [PHENOMENON_TO_OUTPUT.get(f, f) for f in c["fenomenos"]],
        "hard_filters": [],
    }
    return {"question": message, "structured": structured, "unmapped": []}


def _consulta_from_scenario(scenario):
    s = scenario.get("structured") or {}
    cond, grav = {}, []
    for c in s.get("conditions") or []:
        norm = c.get("norm") or {}
        if c.get("kind") == "categorical":
            if c.get("variable") == "gravity_environment" and norm.get("value"):
                grav.append(norm["value"])
        elif isinstance(norm.get("value"), (int, float)):
            cond[c["variable"]] = {"valor": float(norm["value"]), "texto": str((c.get("raw") or {}).get("value", ""))}
    fuels = ([s["fuel"]] if s.get("fuel") else []) + list(s.get("other_fuels") or [])
    phen = [OUTPUT_TO_PHENOMENON.get(o, o) for o in s.get("outputs_of_interest") or []]
    # texto="" on purpose: chips the user removed must not come back through the free text
    return {"condiciones": cond, "combustibles": fuels, "gravedad": grav, "fenomenos": phen, "texto": ""}


def _problems(consulta):
    p = []
    n = len(consulta["condiciones"])
    if n < 2:
        p.append(f"{'Only one numeric condition was' if n == 1 else 'No numeric conditions were'} recognized. "
                 "Matching against experiments needs at least two (for example pressure and oxygen); "
                 "otherwise results rely on text similarity only.")
    if len(consulta["combustibles"]) > 1:
        p.append("More than one fuel was recognized; the first one is shown.")
    return p


# ------------------------------------------------------------------------------ results
def _result(motor, rank, exp_id, R, inv_entry, consulta, provisional, docs, origin_kind):
    doc = docs.get(exp_id, {})
    b_exp = next((e for e in motor.srv.b.exps if e["id"] == exp_id), {})
    records = (doc.get("conditions") or []) + (doc.get("measurements") or []) + (doc.get("outcomes") or [])
    flags = []
    rangos = (inv_entry or {}).get("rangos") or {}
    for var, d in rangos.items():
        if d.get("estado") != "dentro":
            flags.append(f"extrapolation:{var}")
    user_grav = {GRAV_MAPA.get(g, g) for g in consulta["gravedad"]}
    exp_grav = GRAV_MAPA.get(b_exp.get("gravedad"), b_exp.get("gravedad"))
    if user_grav and exp_grav and exp_grav not in user_grav:
        flags.append("different:gravity")
    variable_scores = []
    for var, v in consulta["condiciones"].items():
        rec = _first_reported(records, var)
        unit = motor.srv.b.variables[var].canonical_unit if var in motor.srv.b.variables else None
        if rec is None or not isinstance((rec.get("norm") or {}).get("value"), (int, float)):
            flags.append(f"cannot_verify:{var}")
            variable_scores.append({"variable": var, "status": "not_reported", "scenario_value": v["valor"], "unit": unit})
        else:
            variable_scores.append({"variable": var, "status": "reported", "experiment_value": rec["norm"]["value"],
                                    "scenario_value": v["valor"], "unit": rec["norm"].get("unit") or unit})
    if user_grav:
        variable_scores.append({"variable": "gravity_environment", "status": "reported" if exp_grav else "not_reported",
                                "experiment_value": None, "s_v": 1 if exp_grav in user_grav else 0})
    for f in ((doc.get("quality") or {}).get("flags") or []):
        flags.append(f"data_quality:{f}")
    if provisional:
        flags.append("provisional_sigma")

    comp = (inv_entry or {}).get("componentes") or {}
    names = [("s_phys", "puntos"), ("s_out", "variables"), ("s_mat", "contexto"), ("Range overlap", "rangos"), ("s_text", "texto")]
    components = [{"name": n, "score": comp.get(k), "observed": comp.get(k) is not None} for n, k in names]
    overlaps = [d["solape"] for d in rangos.values() if d.get("solape") is not None]
    return {
        "rank": rank, "experiment_id": exp_id, "investigation_id": doc.get("investigation_id"),
        "fuel": b_exp.get("combustible"), "data_origin": doc.get("data_origin"),
        "R": round(float(R), 3), "match_type": origin_kind,
        "T": round(sum(overlaps) / len(overlaps), 3) if overlaps else None,   # share of your values inside the tested ranges
        "Q": _quality_score(doc),                                            # from the record's own quality block
        "U": None,                                                           # not estimated yet
        "flags": flags, "variable_scores": variable_scores, "components": components,
        "conditions": _kv_list(motor, doc.get("conditions")),
        "outcomes": _kv_list(motor, doc.get("outcomes")),
        "measurements": _kv_list(motor, doc.get("measurements"))[:8],
    }


def _collect(motor, r, consulta):
    docs = _experiment_index(motor)
    inv_by_id = {x["investigacion"]: x for x in r["investigaciones"]}
    provisional = bool(r["parametros"].get("tolerancias_provisionales"))
    # R = how close this experiment's conditions are (similitud) x how related its investigation is overall
    # (fuel, gravity, phenomenon, ranges, meaning: afinidad_total). A gas flame at the same pressure does not
    # outrank a droplet test when you asked about droplets.
    neigh = [(v["experimento"], v["similitud"] * (x.get("afinidad_total") or 0), x["investigacion"])
             for x in r["investigaciones"] for v in x.get("vecinos") or []]
    neigh = [t for t in neigh if t[1] > 0]
    neigh.sort(key=lambda t: -t[1])
    abstained = not neigh
    if abstained:   # no numeric match: show nearest records by meaning, clearly marked as not comparable
        sem = [(m["id"], m["similitud_semantica"], m["inv"]) for m in r["semantica"]["mas_parecidos"] if m["tipo"] == "experimento"]
        neigh, kind = sem, "text"
    else:
        kind = "conditions"
    out = []
    for i, (eid, R, inv) in enumerate(neigh[:MAX_RESULTS], 1):
        out.append(_result(motor, i, eid, R, inv_by_id.get(inv), consulta, provisional, docs, kind))
    return out, abstained


# --------------------------------------------------------------------------------- answer
SYSTEM = """You are the scientific assistant of Flame in Freefall (NASA PSI microgravity combustion data).
Answer in English, briefly and precisely, using ONLY the FACTS you receive. You have no other knowledge of these experiments.
Rules:
1. Every statement must cite at least one ID that appears in FACTS (experiment IDs such as PSI-159:normal:21174C5, finding IDs such as FND:PSI-159:001). Never invent IDs.
2. Do not write any number that does not appear in FACTS. Prefer the "shown" values (kPa, %, mm).
3. "evidence": what was recorded or measured in the cited records. "interpretation": what it cautiously suggests for the user's scenario. Similar conditions do not guarantee the same outcome.
4. If the FACTS do not answer the question, say so in the summary and leave "evidence" empty.
5. Summary: at most 60 words. At most 4 evidence and 3 interpretation statements. Limitations: at most 3, without numbers."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "statement": {"type": "string"}, "citations": {"type": "array", "items": {"type": "string"}}},
            "required": ["statement", "citations"]}},
        "interpretation": {"type": "array", "items": {"type": "object", "properties": {
            "statement": {"type": "string"}, "citations": {"type": "array", "items": {"type": "string"}}},
            "required": ["statement", "citations"]}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "evidence", "interpretation", "limitations"],
}


def _facts(motor, scenario, consulta, results, r, abstained):
    exps = []
    for x in results[:MAX_FACT_EXPERIMENTS]:
        exps.append({
            "id": x["experiment_id"], "investigation": x["investigation_id"], "fuel": x["fuel"],
            "data_origin": x["data_origin"], "match": x["match_type"], "relevance": x["R"],
            "conditions": {c["label"]: {"value": c["value"], "unit": c["unit"], "shown": _display(c["label"].lower(), c["value"], c["unit"])}
                           for c in x["conditions"][:8]},
            "outcomes": {o["label"]: o["value"] for o in x["outcomes"]},
            "flags": x["flags"],
        })
    findings = [{"id": h["hallazgo"], "investigation": h["investigacion"], "statement": h["enunciado"]}
                for h in r.get("hallazgos", [])[:MAX_FACT_FINDINGS]]
    user = {_label(motor, k): {"value": v["valor"], "shown": _display(k, v["valor"], motor.srv.b.variables[k].canonical_unit
                                                                      if k in motor.srv.b.variables else None)}
            for k, v in consulta["condiciones"].items()}
    return {"question": scenario.get("question", ""), "scenario": {"fuel": consulta["combustibles"], "gravity": consulta["gravedad"],
            "looking_for": consulta["fenomenos"], "conditions": user},
            "comparable_matches_found": not abstained, "experiments": exps, "findings": findings}


def _check(claims, allowed_ids, facts):
    out = []
    for c in claims or []:
        st = str(c.get("statement", "")).strip()
        cits = [str(i).strip("[] ") for i in c.get("citations") or []]
        ok = bool(st) and bool(cits) and all(i in allowed_ids for i in cits) and not validar(st, facts)
        out.append({"statement": st, "citations": cits, "supported": ok})
    return out


def _fallback(results, abstained):
    if abstained or not results:
        return {"summary": "No experiment in the database matches these conditions closely enough to compare. "
                           "The records listed are the nearest by description only.",
                "evidence": [], "interpretation": [], "limitations": []}
    top = results[0]
    ev = []
    for x in results[:3]:
        outs = ", ".join(f"{o['label']}: {o['value']}" for o in x["outcomes"][:3])
        if outs:
            ev.append({"statement": f"Experiment {x['experiment_id']} ({x['fuel'] or 'fuel not reported'}) recorded {outs}.",
                       "citations": [x["experiment_id"]], "supported": True})
    return {"summary": f"{len(results)} experiments in the database were tested under conditions close to yours. "
                       f"The closest is {top['experiment_id']}.",
            "evidence": ev, "interpretation": [], "limitations": []}


def _limits(results, r, abstained):
    lim = []
    if r["parametros"].get("tolerancias_provisionales"):
        lim.append("Similarity tolerances are provisional until a domain expert sets them.")
    if any(f.startswith("extrapolation:") for x in results for f in x["flags"]):
        lim.append("Some of your values are outside the ranges tested in the matched investigations.")
    if any(x["data_origin"] == "simulation" for x in results):
        lim.append("Some matches are simulations, not measurements.")
    if not abstained:
        lim.append("Similar conditions do not guarantee the same outcome in your case.")
    return lim


def _answer(motor, scenario, consulta, results, r, abstained):
    facts = _facts(motor, scenario, consulta, results, r, abstained)
    allowed = {e["id"] for e in facts["experiments"]} | {h["id"] for h in facts["findings"]}
    source, reason = "template", ""
    try:
        if not llm.servidores():
            raise RuntimeError("QWEN_URL is not configured")
        raw = llm.generar(SYSTEM, "FACTS:\n" + json.dumps(facts, ensure_ascii=False, default=str), max_tokens=900, esquema=SCHEMA)
        a = json.loads(raw)
        answer = {"summary": str(a.get("summary", "")).strip(),
                  "evidence": _check(a.get("evidence"), allowed, facts),
                  "interpretation": _check(a.get("interpretation"), allowed, facts),
                  "limitations": [str(l) for l in (a.get("limitations") or [])[:3] if not validar(str(l), facts)]}
        if not answer["summary"] or validar(answer["summary"], facts):
            answer["summary"] = _fallback(results, abstained)["summary"]
        source = "qwen"
    except Exception as e:  # noqa: BLE001 - any LLM failure falls back to the deterministic answer
        answer, reason = _fallback(results, abstained), f"{type(e).__name__}: {str(e)[:150]}"
    answer["limitations"] = _limits(results, r, abstained) + answer["limitations"]
    return answer, source, reason


# --------------------------------------------------------------------------------- routes
@chat_api.post("/interpret")
def interpret():
    motor = _motor()
    if motor is None:
        return _unavailable()
    message = str((request.get_json(silent=True) or {}).get("message", "")).strip()
    if len(message) < 3:
        return jsonify(error="Describe your scenario in a few words."), 400
    c = motor.interpretar(message[:1500])
    scenario = _scenario_from_consulta(motor, message, c)
    n = len(scenario["structured"]["conditions"]) + bool(scenario["structured"]["fuel"])
    return jsonify(scenario=scenario, summary=f"{n} item(s) recognized.", problems=_problems(c))


@chat_api.post("/answer")
def answer():
    motor = _motor()
    if motor is None:
        return _unavailable()
    body = request.get_json(silent=True) or {}
    scenario = body.get("scenario")
    if not isinstance(scenario, dict) or not body.get("confirmed"):
        return jsonify(error="Confirm the scenario first."), 400
    consulta = _consulta_from_scenario(scenario)
    r = motor.afinidad(consulta=consulta)
    results, abstained = _collect(motor, r, consulta)
    ans, source, reason = _answer(motor, scenario, consulta, results, r, abstained)
    rec_id = uuid.uuid4().hex
    try:
        current_app.extensions["db"].chat_sessions.insert_one({
            "_id": rec_id, "created_at": datetime.now(timezone.utc), "question": scenario.get("question", ""),
            "consulta": consulta, "result_ids": [x["experiment_id"] for x in results],
            "abstained": abstained, "answer_source": source})
    except Exception as e:  # noqa: BLE001 - saving the session must not break the answer
        current_app.logger.warning("chat_sessions not saved: %s", e)
    return jsonify(answer=ans, results=results, abstained=abstained, recommendation_id=rec_id,
                   candidates_considered=len(motor.srv.b.exps), answer_source=source, answer_note=reason)


@chat_api.post("/feedback")
def feedback():
    body = request.get_json(silent=True) or {}
    try:
        grade = int(body.get("grade"))
    except (TypeError, ValueError):
        return jsonify(error="grade must be 0, 1, 2 or 3"), 400
    if grade not in (0, 1, 2, 3) or not body.get("experiment_id") or not body.get("recommendation_id"):
        return jsonify(error="Missing recommendation_id, experiment_id or grade."), 400
    db = current_app.extensions["db"]
    try:
        sess = db.chat_sessions.find_one({"_id": body["recommendation_id"]}, {"question": 1})
        db.relevance_judgments.insert_one({
            "recommendation_id": body["recommendation_id"], "experiment_id": str(body["experiment_id"]),
            "grade": grade, "question": (sess or {}).get("question", ""), "created_at": datetime.now(timezone.utc)})
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Could not save the rating: {type(e).__name__}"), 503
    return jsonify(ok=True)
