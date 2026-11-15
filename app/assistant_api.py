"""
Database assistant (the chat icon on every page).

    POST /api/assistant  {message, history: [{role, content}], context: {page, investigation_id}}
      -> {answer, recommendations, sources, removed_statements, source}

How an answer is built:
  1. The question (plus the previous one, for follow-ups) is searched in the database with motor_psi's ranking:
     meaning (embeddings) + exact words (BM25) + numbers (neighbor experiments), fused with RRF.
  2. The best records, a small catalog of the investigations and, on the Suggestions page, the investigation
     being viewed, are given to Qwen as FACTS. Qwen sees nothing else.
  3. Every sentence of the answer and every recommendation is checked: cited IDs must be among the FACTS and
     numbers must appear in them. Sentences that fail are removed (and counted), never shown.
  4. If Qwen is not available, the most relevant records are listed instead.

When the question gives conditions (e.g. "my cabin is at 70 kPa with 30% oxygen"), the experiments tested near
those conditions are found by the numbers themselves and summarized (did flames ignite, did they self-extinguish or
keep burning until the fuel was cut, burn times, how the outcome changes with oxygen). That summary is computed here,
not by Qwen, and it also produces a "data signal" alert with an explicit rule.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter

from flask import Blueprint, current_app, jsonify, request

from motor_psi import llm
from motor_psi.narrador import ID_RE, validar

assistant_api = Blueprint("assistant_api", __name__, url_prefix="/api/assistant")

K_SOURCES = 8
SOURCE_CHARS = 650
MAX_HISTORY = 6
WINDOWS = (1.5, 2.5)          # how close an experiment must be, in tolerances (sigma) per variable
MIN_MATCHES = 5
OXYGEN = "ambient_oxygen_mole_fraction"
# The same two reference atmospheres the dashboard shows (index.html). They are app references, not database records.
REFERENCE_ATMOSPHERES = [{"name": "ISS cabin", "pressure_kPa": 101.3, "oxygen_percent": 21},
                         {"name": "Exploration atmosphere", "pressure_kPa": 56.5, "oxygen_percent": 34}]
IGNITED = {"yes", "flash"}
TYPE_EN = {"investigacion": "investigation", "dataset": "dataset", "experimento": "experiment",
           "fragmento": "report passage", "hallazgo": "finding"}

SYSTEM = """You are the database assistant of PSI Scientific Advisor (Flame in Freefall): microgravity combustion
experiments from NASA Physical Sciences Informatics, plus investigations uploaded by users (marked user_upload).
Use ONLY the FACTS you receive; you have no other knowledge of these experiments. Answer in the language of the question.
Your job is to INTERPRET the data for the user, not to send them elsewhere.
Rules:
1. Cite the ID behind each claim in square brackets, e.g. [PSI-159], [FND:PSI-159:001], [PSI-159:normal:21174C5].
   Only IDs that appear in FACTS. Never invent IDs.
2. Do not write any number that does not appear in FACTS (percentages of fractions in FACTS are fine).
3. When FACTS.scenario exists, the user described conditions. Explain, with numbers from FACTS.scenario:
   whether their values are inside the tested ranges; what happened in the close tests (ignition, flames that went out
   on their own versus flames that kept burning until the fuel was cut, burn times); how the outcome changes with
   oxygen (oxygen_trend); and what setup and fuel those tests used, because the result may not transfer to a cabin fire.
   If FACTS.alert exists, your answer must agree with it.
4. Use the closest data when there is no exact match and say how close it is. Say that the database does not contain
   something only when nothing in FACTS is related.
5. Under 220 words, plain text, short paragraphs, no headings.
6. "recommendations": up to 3 concrete steps grounded in FACTS, each citing at least one ID. For a risky scenario,
   point to the conditions where tests went out on their own or did not ignite, citing them; never present them as a
   certified safe limit. Empty list if none apply.
7. This is not a safety certification; user uploads are not reviewed. Say so when relevant."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "detail", "citations"]}},
    },
    "required": ["answer", "recommendations"],
}


# ------------------------------------------------------------------------- conditions -> experiments
def _value(doc, var):
    for r in (doc.get("conditions") or []) + (doc.get("measurements") or []) + (doc.get("outcomes") or []):
        if r.get("variable") == var and r.get("status") == "reported":
            v = (r.get("norm") or {}).get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def _outcome(doc, var):
    for r in doc.get("outcomes") or []:
        if r.get("variable") == var and r.get("status") == "reported":
            return (r.get("norm") or {}).get("value")
    return None


def _shown(var, value, unit):
    from .chat_api import _display
    return _display(var, value, unit)


def _scenario(motor, consulta):
    """Experiments tested near the conditions in the question, and what happened in them. No language model."""
    b = motor.srv.b
    q = {v: c["valor"] for v, c in (consulta or {}).get("condiciones", {}).items() if v in b.variables}
    if not q:
        return None
    exps = b.col["experiments"]
    vals = [{v: _value(e, v) for v in q} for e in exps]

    coverage = []
    for v, x in q.items():
        xs = [d[v] for d in vals if d[v] is not None]
        var = b.variables[v]
        row = {"variable": v, "label": var.label, "yours": x, "yours_shown": _shown(v, x, var.canonical_unit),
               "experiments_reporting_it": len(xs)}
        if xs:
            row.update(tested_min=min(xs), tested_max=max(xs), inside_tested_range=min(xs) <= x <= max(xs),
                       tested_shown=f"{_shown(v, min(xs), var.canonical_unit)} to {_shown(v, max(xs), var.canonical_unit)}")
        coverage.append(row)

    def z(d):
        if any(d[v] is None for v in q):
            return None
        return max(abs(b.variables[v].a_phi(d[v]) - b.variables[v].a_phi(x)) / b.variables[v].sigma for v, x in q.items())

    zs = [(i, z(d)) for i, d in enumerate(vals)]
    zs = [(i, zz) for i, zz in zs if zz is not None]
    window, chosen = None, []
    for w in WINDOWS:
        chosen = [i for i, zz in zs if zz <= w]
        if len(chosen) >= MIN_MATCHES:
            window = w
            break
    nearest_only = False
    if len(chosen) < MIN_MATCHES:          # nothing close enough: show the nearest, clearly marked
        chosen = [i for i, _ in sorted(zs, key=lambda t: t[1])[:8]]
        nearest_only = bool(chosen)
    matched = [exps[i] for i in chosen]

    def summary(docs):
        ign = Counter(str(_outcome(e, "hot_ignition")) for e in docs if _outcome(e, "hot_ignition") is not None)
        term = Counter(str(_outcome(e, "termination_mode")) for e in docs if _outcome(e, "termination_mode") is not None)
        n_ign = sum(ign.values())
        ignited = sum(c for k, c in ign.items() if k in IGNITED)
        n_term = sum(term.values())
        burn = sorted(t for e in docs if (t := _value(e, "burn_time")) is not None)
        return {"tests": len(docs), "with_ignition_result": n_ign, "ignited": ignited,
                "ignition_rate": round(ignited / n_ign, 2) if n_ign else None,
                "self_extinguished": term.get("self_extinguished", 0), "burned_until_fuel_cut": term.get("flow_terminated", 0),
                "self_extinction_rate": round(term.get("self_extinguished", 0) / n_term, 2) if n_term else None,
                "burn_time_s": {"min": round(burn[0], 1), "median": round(burn[len(burn) // 2], 1), "max": round(burn[-1], 1)} if burn else None}

    s_all = summary(matched)
    matched_ranges = {}
    for v in q:                                  # the values the "close tests" really have (window can be wide)
        xs = [x for e in matched if (x := _value(e, v)) is not None]
        if xs:
            u = b.variables[v].canonical_unit
            matched_ranges[b.variables[v].label] = f"{_shown(v, min(xs), u)} to {_shown(v, max(xs), u)}"
    examples = []
    for e in sorted(matched, key=lambda e: dict(zs).get(exps.index(e), 0))[:6]:
        examples.append({"id": e["_id"], "investigation": e["investigation_id"], "fuel": (e.get("fuel") or {}).get("canonical"),
                         "setup": f"{e.get('phenomenon')}, {(e.get('configuration') or {}).get('geometry', 'n/a')}",
                         "conditions": {b.variables[v].label: _shown(v, _value(e, v), b.variables[v].canonical_unit) for v in q},
                         "hot_ignition": _outcome(e, "hot_ignition"), "termination": _outcome(e, "termination_mode"),
                         "burn_time_s": _value(e, "burn_time")})

    trend = []
    if OXYGEN in q:        # how the outcome changes with oxygen, at similar values of the other variables
        others = {v: x for v, x in q.items() if v != OXYGEN}
        pool = []
        for e in exps:
            o2 = _value(e, OXYGEN)
            if o2 is None or _outcome(e, "hot_ignition") is None:
                continue
            if any((y := _value(e, v)) is None or abs(b.variables[v].a_phi(y) - b.variables[v].a_phi(x)) / b.variables[v].sigma > WINDOWS[-1]
                   for v, x in others.items()):
                continue
            pool.append((o2, e))
        for lo in range(0, 100, 5):
            band = [e for o2, e in pool if lo / 100 <= o2 < (lo + 5) / 100]
            if band:
                sm = summary(band)
                n_end = sm["self_extinguished"] + sm["burned_until_fuel_cut"]
                trend.append({"oxygen_band": f"{lo}-{lo + 5}%", "tests": sm["tests"], "ignition_rate": sm["ignition_rate"],
                              "self_extinction_rate": sm["self_extinction_rate"],
                              "kept_burning_rate": round(sm["burned_until_fuel_cut"] / n_end, 2) if n_end else None})

    setups = Counter(f"{e.get('phenomenon')} / {(e.get('configuration') or {}).get('geometry', 'n/a')}" for e in matched)
    fuels = Counter((e.get("fuel") or {}).get("canonical") or "not reported" for e in matched)
    return {"your_conditions": coverage, "window_tolerances": window, "nearest_only": nearest_only,
            "close_tests": s_all, "close_tests_range": matched_ranges, "examples": examples, "oxygen_trend": trend,
            "setups": dict(setups.most_common(3)), "fuels": dict(fuels.most_common(4)),
            "investigations": dict(Counter(e["investigation_id"] for e in matched)),
            "reference_atmospheres": REFERENCE_ATMOSPHERES,
            "what_the_outcomes_mean": {"hot_ignition": "whether a hot flame ignited when the igniter fired",
                                       "self_extinguished": "the flame went out on its own while fuel was still flowing",
                                       "burned_until_fuel_cut": "the flame kept burning until the operators stopped the fuel"}}


def _alert(sc):
    """Deterministic data signal. Explicit rule, no language model. Not a safety rating."""
    if not sc:
        return None
    outside = [c["label"] for c in sc["your_conditions"] if c.get("inside_tested_range") is False]
    o2 = next((c["yours"] for c in sc["your_conditions"] if c["variable"] == OXYGEN), None)
    t = sc["close_tests"]
    if outside:
        return {"level": "unknown", "title": "Outside the tested range",
                "text": f"Your {', '.join(outside)} is outside every value tested in the database, so the data cannot describe "
                        "what would happen. Treat it as untested.",
                "rule": "Shown when any value you gave is outside the minimum-maximum tested in the database."}
    if sc["nearest_only"] or not t["with_ignition_result"]:
        return {"level": "info", "title": "Few comparable tests",
                "text": "There are not enough tests close to your conditions with a recorded ignition result to give a signal.",
                "rule": f"Needs at least {MIN_MATCHES} tests within {WINDOWS[-1]} tolerances that report ignition."}
    sustained = t["burned_until_fuel_cut"]
    n_term = sustained + t["self_extinguished"]
    where = "; ".join(f"{k} {v}" for k, v in sc["close_tests_range"].items())
    head = f"Of {t['with_ignition_result']} tests at {where}, {t['ignited']} ignited"
    # The igniter forced ignition in these tests, so ignition alone says little; what separates outcomes is whether
    # the flame went out on its own or kept burning until the fuel was cut.
    if o2 is not None and o2 > 0.21 and n_term and sustained / n_term >= 0.25:
        return {"level": "high", "title": "Flames kept burning in similar tests",
                "text": f"{head}; {sustained} of {n_term} kept burning until the operators cut the fuel instead of going out "
                        f"on their own. Your oxygen ({round(o2 * 100, 1)}%) is above the ISS cabin reference (21%).",
                "rule": "Shown when oxygen is above 21% and at least a quarter of the close tests with a recorded end kept "
                        "burning until the fuel was cut."}
    return {"level": "info", "title": "What similar tests showed",
            "text": f"{head}" + (f"; {t['self_extinguished']} of {n_term} went out on their own." if n_term else "."),
            "rule": "Summary of the close tests; no threshold was crossed."}


def _catalog(motor):
    b = motor.srv.b
    out = []
    for inv_id, inv in b.inv.items():
        P = b.perfil_inv.get(inv_id, {})
        out.append({"id": inv_id, "title": inv.get("title"), "acronym": inv.get("acronym"),
                    "origin": "user_upload" if inv.get("origin") == "user_upload" else "NASA PSI",
                    "experiments": P.get("n_experimentos", 0), "fuels": sorted(P.get("combustibles", {})),
                    "gravity": sorted(P.get("gravedad", {})),
                    "variables": [b.variables[v].label for v in P.get("rangos", {})][:12]})
    return out


def _current_investigation(motor, inv_id):
    if not inv_id or inv_id not in motor.srv.b.inv:
        return None
    try:
        from .investigations_api import _analysis
        a, _, _ = _analysis(motor, inv_id)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("assistant context failed: %s", exc)
        return None
    return {"id": inv_id, "title": a["overview"]["title"], "fuel": a["overview"]["fuel"],
            "variables": a["overview"]["variables"], "experiments": a["overview"]["experiments"],
            "most_related": [{"investigation": x["investigation_id"], "affinity": x["total"], "level": x["level"],
                              "reasons": x["reasons"]} for x in a["affinity"][:4]],
            "your_ranges": [{"variable": r["label"], "min": r["yours"]["min"], "max": r["yours"]["max"], "unit": r["unit"],
                             "tested_by": [{"investigation": t["investigation_id"], "status": t["status"]} for t in r["investigations"]]}
                            for r in a["ranges"]]}


def _sources(motor, query):
    rk = motor.ranking(query, k=K_SOURCES)
    items, pos = motor.srv.sem.items, motor.srv.sem.pos
    out = []
    for x in rk["resultados"]:
        it = items[pos[x["id"]]]
        out.append({"id": x["id"], "type": TYPE_EN.get(x["tipo"], x["tipo"]), "investigation": x["investigacion"],
                    "text": it["texto"][:SOURCE_CHARS], "page": x.get("pagina")})
    return out


def _clean_sentences(text, facts):
    """Keep only sentences whose IDs and numbers are backed by FACTS."""
    kept, dropped = [], 0
    for para in [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]:
        good = []
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z\[])", para):
            if not sent.strip():
                continue
            if validar(sent, facts):
                dropped += 1
            else:
                good.append(sent.strip())
        if good:
            kept.append(" ".join(good))
    return "\n\n".join(kept), dropped


def _allowed(facts):
    s = json.dumps(facts, ensure_ascii=False)
    return {m.rstrip(".:-") for m in re.findall(r"(?:FND|DS|CHK|DOC):[A-Za-z0-9_.:\-]+|PSI-\d+(?::[A-Za-z0-9_.:\-]+)?", s)}


@assistant_api.post("")
def ask():
    motor = current_app.extensions.get("motor")
    if motor is None:
        return jsonify(error="The assistant is not available: " + str(current_app.extensions.get("motor_error"))), 503
    body = request.get_json(silent=True) or {}
    message = str(body.get("message", "")).strip()[:1500]
    if len(message) < 2:
        return jsonify(error="Write a question."), 400
    history = [h for h in (body.get("history") or []) if isinstance(h, dict) and h.get("role") in ("user", "assistant")][-MAX_HISTORY:]
    ctx = body.get("context") or {}
    current = _current_investigation(motor, str(ctx.get("investigation_id") or "").strip())

    # retrieval: this question + the previous user question (follow-ups like "and at 2 bar?")
    prev_user = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
    query = " ".join([message, str(prev_user)[:300], current["title"] if current else ""]).strip()
    sources = _sources(motor, query)
    consulta = motor.interpretar(message)
    if not consulta.get("condiciones") and prev_user:          # follow-up such as "and with 25%?" keeps earlier numbers
        consulta = motor.interpretar(f"{prev_user} {message}")
    elif prev_user:
        before = motor.interpretar(str(prev_user)).get("condiciones", {})
        consulta["condiciones"] = {**before, **consulta["condiciones"]}
    scenario = _scenario(motor, consulta)
    alert = _alert(scenario)
    if scenario:
        for ex in scenario["examples"]:
            if ex["id"] not in {s_["id"] for s_ in sources}:
                sources.append({"id": ex["id"], "type": "experiment", "investigation": ex["investigation"],
                                "text": json.dumps(ex, ensure_ascii=False, default=str)[:SOURCE_CHARS], "page": None})
    facts = {"question": message, "page": ctx.get("page"), "investigations_catalog": _catalog(motor),
             "current_investigation": current, "scenario": scenario, "alert": alert, "sources": sources}
    allowed = _allowed(facts)

    out = {"answer": "", "recommendations": [], "removed_statements": 0, "source": "template", "note": "",
           "alert": alert, "scenario": None if not scenario else {
               "conditions": scenario["your_conditions"], "close_tests": scenario["close_tests"],
               "close_tests_range": scenario["close_tests_range"],
               "oxygen_trend": scenario["oxygen_trend"], "nearest_only": scenario["nearest_only"],
               "setups": scenario["setups"], "fuels": scenario["fuels"]}}
    try:
        if not llm.servidores():
            raise RuntimeError("QWEN_URL is not configured")
        convo = "\n".join(f"{h['role'].upper()}: {str(h.get('content', ''))[:600]}" for h in history)
        user = (f"CONVERSATION SO FAR (context only; it is not a source):\n{convo}\n\n" if convo else "") + \
               "FACTS:\n" + json.dumps(facts, ensure_ascii=False, default=str) + f"\n\nQUESTION: {message}"
        raw = json.loads(llm.generar(SYSTEM, user, max_tokens=900, esquema=SCHEMA))
        answer, dropped = _clean_sentences(str(raw.get("answer", "")), facts)
        recs = []
        for r in raw.get("recommendations") or []:
            cits = [str(c).strip("[] ") for c in r.get("citations") or []]
            txt = f"{r.get('title', '')}. {r.get('detail', '')}"
            if cits and all(c in allowed for c in cits) and not validar(txt, facts):
                recs.append({"title": str(r.get("title", "")).strip(), "detail": str(r.get("detail", "")).strip(), "citations": cits})
            else:
                dropped += 1
        if not answer:
            answer = "I could not write an answer that the database backs up. These are the most relevant records."
        out.update(answer=answer, recommendations=recs[:3], removed_statements=dropped, source="qwen")
    except Exception as exc:  # noqa: BLE001
        out["answer"] = ("The assistant is not available right now. " +
                         (alert["text"] if alert else "These are the records most related to your question."))
        out["note"] = f"{type(exc).__name__}: {str(exc)[:150]}"

    cited = set(ID_RE.findall(out["answer"])) | {c for r in out["recommendations"] for c in r["citations"]}
    out["sources"] = [{k: s[k] for k in ("id", "type", "investigation", "page")} | {"excerpt": s["text"][:220], "cited": s["id"] in cited}
                      for s in sources]
    return jsonify(out)
