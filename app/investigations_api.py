"""
Investigations uploaded from insertion.html, and their analysis for suggestion.html.

    POST   /api/investigations                       form from insertion.js -> stored in MongoDB and added to the base
    GET    /api/investigations                       uploaded investigations (for the selector in suggestion.html)
    GET    /api/investigations/<id>/analysis         numbers and chart data (database only, no language model)
    POST   /api/investigations/<id>/annotations      summary, notes per variable and recommendations (Qwen, checked)
    DELETE /api/investigations/<id>                  removes an uploaded investigation (never a NASA one)

Every uploaded record carries origin="user_upload", so it can be told apart from (and removed without touching)
the NASA PSI records. An uploaded investigation is never compared with itself.
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import gridfs
from flask import Blueprint, current_app, jsonify, request
from pymongo.errors import BulkWriteError, WriteError
from werkzeug.utils import secure_filename

from motor_psi import Motor, llm
from motor_psi.afinidad import GRAV_MAPA
from motor_psi.ingesta import FAMILIA, detectar_combustibles, detectar_fenomenos, detectar_gravedad, leer_archivo
from motor_psi.narrador import validar

from .chat_api import _display

investigations_api = Blueprint("investigations_api", __name__, url_prefix="/api/investigations")

ORIGIN = "user_upload"
DATA_ROLES = {"experimental_data", "simulation_data"}
TEXT_ROLES = {"report", "data_dictionary", "metadata", "other"}
ALLOWED_EXT = {"csv", "xlsx", "xls", "pdf", "txt", "zip", "json"}
CHUNK_CHARS = 1200
USER_ID_START = 9001                 # uploads without an ID get PSI-9001, PSI-9002, ...
RAW_BUCKET = "raw_files"             # same GridFS bucket the NASA files use
PIPELINE, PIPELINE_VERSION, SCHEMA_VERSION = "flame_app.upload", "0.1.0", "0.1"
MAX_SCATTER_POINTS = 2500
_lock = threading.Lock()   # one insertion/reload at a time


# ------------------------------------------------------------------------------------ helpers
def _db():
    return current_app.extensions["db"]


def _motor():
    return current_app.extensions.get("motor")


def _now():
    return datetime.now(timezone.utc)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", Path(name).stem).strip("_")[:60] or "file"


def _var(motor, v):
    return motor.srv.b.variables.get(v)


def _label(motor, v):
    x = _var(motor, v)
    return x.label if x else v.replace("_", " ")


def _unit(motor, v):
    x = _var(motor, v)
    return x.canonical_unit if x else None


def _shown(motor, v, value):
    return _display(v, value, _unit(motor, v))


def _reload_motor():
    """Re-read MongoDB and update the embeddings index (only new items are embedded)."""
    current_app.extensions["motor"] = Motor.recargar()
    current_app.extensions["motor_error"] = None
    current_app.extensions.pop("experiment_index", None)   # cache used by chat_api


def _next_user_id(db) -> str:
    """The database only accepts IDs like PSI-<number>. Uploads without an ID get PSI-9001, PSI-9002…
    (a range far from the NASA PSI numbers), so they never collide with a real investigation."""
    nums = [int(m.group(1)) for d in db.investigations.find({"origin": ORIGIN}, {"_id": 1})
            if (m := re.fullmatch(r"PSI-(\d+)", str(d["_id"]))) and int(m.group(1)) >= USER_ID_START]
    return f"PSI-{(max(nums) + 1) if nums else USER_ID_START}"


def _chunks(text: str):
    """Split text into ~CHUNK_CHARS pieces, keeping the [p.N] page tags that leer_archivo writes for PDFs."""
    out, buf, page, start = [], "", None, None
    for part in re.split(r"(\[p\.\d+\])", text):
        m = re.match(r"\[p\.(\d+)\]", part)
        if m:
            page = int(m.group(1))
            continue
        for para in re.split(r"\n\s*\n|\n", part):
            para = para.strip()
            if not para:
                continue
            if start is None:
                start = page
            if len(buf) + len(para) > CHUNK_CHARS and buf:
                out.append((buf.strip(), start, page))
                buf, start = "", page
            buf += para + " "
    if buf.strip():
        out.append((buf.strip(), start, page))
    return out


# ------------------------------------------------------------------ fields the validators require
def _extraction():
    return {"pipeline": PIPELINE, "pipeline_version": PIPELINE_VERSION, "extracted_at": _now(), "review_status": "unreviewed"}


def _source_type(ext):
    return {"csv": "csv", "xlsx": "xlsx", "xls": "xlsx", "pdf": "pdf_table"}.get(ext, "manual")


def _store_file(db, inv_id, name, content):
    """Raw file into GridFS bucket raw_files, like the NASA files (file_id = sha256:<hash>)."""
    sha = hashlib.sha256(content).hexdigest()
    file_id = f"sha256:{sha}"
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    bucket = gridfs.GridFSBucket(db, bucket_name=RAW_BUCKET)
    if not db[f"{RAW_BUCKET}.files"].find_one({"_id": file_id}):
        bucket.upload_from_stream_with_id(file_id, name, io.BytesIO(content),
                                          metadata={"origin": ORIGIN, "investigation_id": inv_id, "media_type": media})
    return {"storage": "gridfs", "bucket": RAW_BUCKET, "file_id": file_id, "filename": name, "sha256": sha,
            "size_bytes": len(content), "media_type": media}


def _test_setup(gravity, role):
    """record_type / method.test_type are required enums. Returns (record_type, test_type, data_origin, assumed)."""
    if role == "simulation_data" or gravity == "simulated_microgravity":
        return "simulation_case", "numerical_model", "simulation", False
    if gravity == "microgravity_iss":
        return "flight_test_point", "orbital_platform", "experimental", False
    return "ground_test_point", "ground_lab", "experimental", gravity != "normal_gravity_lab"


# --------------------------------------------------------------------------------- insertion
def _build_records(db, motor, inv_id, form, stored):
    """stored: list of (name, role, bytes). Returns the MongoDB documents plus a report for the user."""
    b = motor.srv.b
    text_all = " ".join([form["title"], form["description"], " ".join(form["keywords"])])
    gravity = form["gravity_environment"] or next(iter(sorted(detectar_gravedad(text_all))), None)
    experiments, datasets, documents, report = [], [], [], {"files": [], "unmapped_columns": [], "warnings": []}
    text_fuels = set(detectar_combustibles(text_all))
    data_fuels = set()
    parsed, locations = [], {}

    for name, role, content in stored:
        ext = name.lower().rsplit(".", 1)[-1]
        locations[name] = _store_file(db, inv_id, name, content)
        info = {"name": name, "role": role, "status": "stored"}
        if ext in ("csv", "xlsx", "xls", "pdf"):
            try:
                e = leer_archivo(name, content, b)
            except Exception as exc:  # noqa: BLE001
                info["status"] = f"stored, could not be read ({type(exc).__name__})"
                report["files"].append(info)
                continue
            parsed.append((name, role, e))
            if ext in ("csv", "xlsx", "xls") or role in DATA_ROLES:
                data_fuels |= set(e["categoricos"]["combustibles"])
            if e["texto"]:
                info["status"] = "text indexed"
            if any(t["filas"] for t in e["tablas"]) and (role in DATA_ROLES or ext != "pdf"):
                info["status"] = "data read"
        elif ext == "txt":
            parsed.append((name, role, {"texto": content.decode("utf-8", "replace"), "tablas": [], "asignaciones": []}))
            info["status"] = "text indexed"
        else:
            info["status"] = "stored, not analyzed (zip/json)"
            documents.append({"_id": f"DOC:{inv_id}:{_slug(name)}", "record_type": "metadata_file", "origin": ORIGIN,
                              "investigation_ids": [inv_id], "title": name, "location": locations[name],
                              "schema_version": SCHEMA_VERSION, "extraction": _extraction(), "created_at": _now()})
        report["files"].append(info)

    # The data files decide the fuel; the description is used only when the files name none.
    fuels = data_fuels or text_fuels
    fuel = sorted(fuels)[0] if len(fuels) == 1 else None
    if text_fuels and data_fuels and not (text_fuels & data_fuels):
        report["warnings"].append(f"The data files use {', '.join(sorted(data_fuels))}, but the description mentions "
                                  f"{', '.join(sorted(text_fuels))}. The fuel from the files was used.")
    if len(fuels) > 1:
        report["warnings"].append(f"Several fuels were recognized ({', '.join(sorted(fuels))}). Each data file uses its own fuel "
                                  "when it names only one; otherwise its experiments are saved without a fuel.")
    elif not fuels:
        report["warnings"].append("No fuel was recognized. Mentioning it in the description or keywords improves matching.")
    phen = detectar_fenomenos(text_all)
    assumed_any = False

    for name, role, e in parsed:
        ext = name.lower().rsplit(".", 1)[-1]
        stem = _slug(name)
        src = _source_type(ext)
        # ---- tables -> experiments (one per row) + one dataset per file
        if ext in ("csv", "xlsx", "xls") or role in DATA_ROLES:
            mapped = []
            for a in e.get("asignaciones", []):
                if a.get("estado") in ("sin_asignar", "unidad_desconocida"):
                    report["unmapped_columns"].append(f"{name}: {a['columna']}")
                elif a.get("estado") in ("asignada", "sugerida"):
                    mapped.append(a)
            n_mapped = max(1, len({a["variable"] for a in mapped}))
            ds_id = f"DS:{inv_id}:{stem}"
            ds_exps, row_no = [], 0
            rec_type, test_type, data_origin, assumed = _test_setup(gravity, role)
            file_fuels = set(e["categoricos"]["combustibles"])
            f_fuel = sorted(file_fuels)[0] if len(file_fuels) == 1 else fuel      # this file's fuel, else the global one
            assumed_any |= assumed
            for t in e.get("tablas", []):
                for row in t["filas"]:
                    row_no += 1
                    exp_id = f"{inv_id}:upload:{stem}:{row_no:04d}"
                    prov = {"source_type": src, "method": PIPELINE, "file": name, "dataset_id": ds_id, "row": row_no, "table": t["tabla"]}
                    recs = {"conditions": [], "measurements": [], "outcomes": []}
                    for v, val in row.items():
                        var = b.variables.get(v)
                        if var is None:
                            continue
                        bucket = {"condition": "conditions", "outcome": "outcomes"}.get(var.role, "measurements")
                        recs[bucket].append({"variable": v, "status": "reported", "kind": "point", "origin": "measured",
                                             "unit_source": "source_header", "norm": {"value": float(val), "unit": var.canonical_unit},
                                             "provenance": prov})
                    if gravity:
                        recs["conditions"].append({"variable": "gravity_environment", "status": "reported", "kind": "categorical",
                                                   "norm": {"value": gravity, "unit": None},
                                                   "provenance": {"source_type": "manual", "method": "upload_form"}})
                    flags = ["user_upload"] + (["test_type_assumed"] if assumed else [])
                    experiments.append({
                        "_id": exp_id, "experiment_id": exp_id, "investigation_id": inv_id, "origin": ORIGIN,
                        "record_type": rec_type, "data_origin": data_origin, "phenomenon": phen[0] if phen else "unspecified",
                        "configuration": {}, "fuel": {"status": "reported" if f_fuel else "not_reported", "canonical": f_fuel,
                                                      "family": FAMILIA.get(f_fuel) if f_fuel else None},
                        **recs, "labels": {}, "method": {"test_type": test_type},
                        "dataset_ids": [ds_id], "document_ids": [], "finding_ids": [],
                        "quality": {"conditions_complete": round(min(1.0, len(row) / n_mapped), 3), "uncertainty_reported": 0.0,
                                    "raw_data_available": True, "method_described": False, "peer_reviewed_source": False,
                                    "flags": flags},
                        "provenance": {"source_type": src, "method": PIPELINE, "file": name, "dataset_id": ds_id, "row": row_no},
                        "extraction": _extraction(), "schema_version": SCHEMA_VERSION, "created_at": _now(),
                    })
                    ds_exps.append(exp_id)
            if ds_exps:
                datasets.append({"_id": ds_id, "dataset_id": ds_id, "investigation_id": inv_id, "origin": ORIGIN,
                                 "title": name, "description": f"Uploaded {role.replace('_', ' ')} file.",
                                 "data_role": "experimental_table", "data_origin": data_origin, "availability": "in_archive",
                                 "format": ext, "location": locations[name], "n_rows": len(ds_exps),
                                 "columns": [{"header": str(a["columna"]), "variable": a["variable"], "unit": a.get("unidad")}
                                             for a in mapped],
                                 "experiment_ids": ds_exps, "quality_flags": ["user_upload"],
                                 "extraction": _extraction(), "schema_version": SCHEMA_VERSION, "created_at": _now()})
        # ---- text -> parent document + chunks (searchable by meaning)
        if e.get("texto") and (role in TEXT_ROLES or ext in ("pdf", "txt")):
            doc_id = f"DOC:{inv_id}:{stem}"
            kind = {"data_dictionary": "data_dictionary", "metadata": "metadata_file"}.get(role, "report")
            documents.append({"_id": doc_id, "record_type": kind, "origin": ORIGIN, "investigation_ids": [inv_id],
                              "title": name, "location": locations[name],
                              "schema_version": SCHEMA_VERSION, "extraction": _extraction(), "created_at": _now()})
            for k, (txt, p0, p1) in enumerate(_chunks(e["texto"]), 1):
                ch = {"_id": f"CHK:{inv_id}:{stem}:{k:03d}", "record_type": "chunk", "origin": ORIGIN,
                      "investigation_ids": [inv_id], "parent_document_id": doc_id, "chunk_index": k,
                      "text": txt, "n_words": len(txt.split()),
                      "schema_version": SCHEMA_VERSION, "extraction": _extraction(), "created_at": _now()}
                if p0 is not None:
                    ch["page_start"], ch["page_end"] = int(p0), int(p1 if p1 is not None else p0)
                documents.append(ch)
    if assumed_any:
        report["warnings"].append("No environment was selected; the experiments were saved as ground tests. "
                                  "Choose the environment in the form if that is not right.")

    inv = {"_id": inv_id, "investigation_id": inv_id, "origin": ORIGIN, "status": "added",
           "title": form["title"], "objective": form["description"], "description": form["description"],
           "keywords": form["keywords"], "doi": form["doi"] or None, "research_area": "Combustion Science",
           "project_type": "User upload", "gravity_environment": gravity, "fuel": fuel,
           "dataset_ids": [d["_id"] for d in datasets], "document_ids": [d["_id"] for d in documents if d["record_type"] != "chunk"],
           "files": [{"name": n, "role": r, "size": len(c), "file_id": locations[n]["file_id"]} for n, r, c in stored],
           "upload_report": report, "schema_version": SCHEMA_VERSION, "extraction": _extraction(), "created_at": _now()}
    return inv, experiments, datasets, documents, report


def _delete_records(db, inv_id):
    db.experiments.delete_many({"investigation_id": inv_id, "origin": ORIGIN})
    db.measurements_metadata.delete_many({"investigation_id": inv_id, "origin": ORIGIN})
    db.documents.delete_many({"investigation_ids": inv_id, "origin": ORIGIN})
    db.investigations.delete_many({"_id": inv_id, "origin": ORIGIN})
    bucket = gridfs.GridFSBucket(db, bucket_name=RAW_BUCKET)
    for f in db[f"{RAW_BUCKET}.files"].find({"metadata.origin": ORIGIN, "metadata.investigation_id": inv_id}, {"_id": 1}):
        try:
            bucket.delete(f["_id"])
        except Exception:  # noqa: BLE001
            pass


def _write_error(exc):
    """Readable message for a MongoDB validation error (code 121)."""
    details = getattr(exc, "details", None) or {}
    info = (details.get("errInfo") or {}).get("details") or {}
    return (f"MongoDB rejected record {(details.get('errInfo') or {}).get('failingDocumentId')}: "
            f"{json.dumps(info.get('schemaRulesNotSatisfied', details.get('errmsg', str(exc))), default=str)[:600]}")


@investigations_api.post("")
def create():
    motor = _motor()
    if motor is None:
        return jsonify(error="The analysis engine is not available: " + str(current_app.extensions.get("motor_error"))), 503
    f = request.form
    try:
        keywords = [str(k).strip() for k in json.loads(f.get("keywords") or "[]") if str(k).strip()][:15]
        roles = json.loads(f.get("file_roles") or "[]")
    except json.JSONDecodeError:
        return jsonify(error="keywords and file_roles must be JSON lists."), 400
    form = {"title": (f.get("title") or "").strip()[:200], "description": (f.get("description") or "").strip()[:4000],
            "investigation_id": (f.get("investigation_id") or "").strip().upper(), "keywords": keywords,
            "gravity_environment": (f.get("gravity_environment") or "").strip(), "doi": (f.get("doi") or "").strip()}
    uploads = request.files.getlist("files")
    errors = []
    if len(form["title"]) < 5:
        errors.append("Enter a title of at least 5 characters.")
    if len(form["description"]) < 30:
        errors.append("The description needs at least 30 characters.")
    if not keywords:
        errors.append("Add at least one keyword.")
    if form["investigation_id"] and not re.fullmatch(r"PSI-\d+", form["investigation_id"]):
        errors.append("Use the format PSI-number for the ID.")
    if not uploads:
        errors.append("Add at least one document.")
    if errors:
        return jsonify(error=" ".join(errors)), 400

    stored, seen = [], set()
    for k, up in enumerate(uploads):
        name = secure_filename(up.filename or f"file_{k}") or f"file_{k}"
        if name.rsplit(".", 1)[-1].lower() not in ALLOWED_EXT:
            return jsonify(error=f"{up.filename}: format not supported."), 400
        if _slug(name) in seen:
            return jsonify(error=f"Two files are named {Path(name).stem}. Rename one of them."), 400
        seen.add(_slug(name))
        role = roles[k] if k < len(roles) else "other"
        stored.append((name, role, up.read()))

    db = _db()
    with _lock:
        inv_id = form["investigation_id"] or _next_user_id(db)
        if db.investigations.find_one({"_id": inv_id}):
            return jsonify(error=f"{inv_id} already exists in the database. Use another ID or leave it empty."), 409
        try:
            inv, exps, dss, docs, report = _build_records(db, motor, inv_id, form, stored)
            if not exps and not any(d["record_type"] == "chunk" for d in docs):
                _delete_records(db, inv_id)        # removes the raw files already put in GridFS
                return jsonify(error="Nothing could be read from the files: no table columns matched known variables "
                                     "and no text was found. Check the column names and units.",
                               files=report["files"], unmapped_columns=report["unmapped_columns"][:30]), 422
            db.investigations.insert_one(inv)
            if exps:
                db.experiments.insert_many(exps)
            if dss:
                db.measurements_metadata.insert_many(dss)
            if docs:
                db.documents.insert_many(docs)
        except (WriteError, BulkWriteError) as exc:
            _delete_records(db, inv_id)
            current_app.logger.error("insertion rejected by MongoDB: %s", exc)
            return jsonify(error=_write_error(exc) + " Nothing was saved."), 422
        except Exception as exc:  # noqa: BLE001
            _delete_records(db, inv_id)
            current_app.logger.exception("insertion failed")
            return jsonify(error=f"The investigation could not be saved ({type(exc).__name__}: {str(exc)[:300]}). Nothing was saved."), 500
        try:
            _reload_motor()
        except Exception as exc:  # noqa: BLE001 - undo, keep the previous engine running
            _delete_records(db, inv_id)
            from motor_psi.base import reiniciar_base
            reiniciar_base()
            current_app.logger.exception("reload after insertion failed")
            return jsonify(error=f"The investigation could not be added to the search index ({type(exc).__name__}: {exc}). Nothing was saved."), 500

    return jsonify(investigation_id=inv_id, status="added", experiments=len(exps), datasets=len(dss),
                   text_chunks=sum(1 for d in docs if d["record_type"] == "chunk"),
                   files=report["files"], unmapped_columns=report["unmapped_columns"][:30], warnings=report["warnings"],
                   url=f"/suggestion?investigation={inv_id}"), 201


@investigations_api.get("")
def list_uploaded():
    docs = _db().investigations.find({"origin": ORIGIN}, {"title": 1, "created_at": 1})
    out = [{"investigation_id": d["_id"], "title": d.get("title"), "created_at": d.get("created_at")} for d in docs]
    out.sort(key=lambda d: str(d["created_at"]), reverse=True)
    return jsonify(investigations=out)


@investigations_api.delete("/<inv_id>")
def delete(inv_id):
    db = _db()
    if not db.investigations.find_one({"_id": inv_id, "origin": ORIGIN}):
        return jsonify(error="Only uploaded investigations can be deleted here."), 404
    with _lock:
        _delete_records(db, inv_id)
        _reload_motor()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------------- analysis
def _first_value(doc, var):
    for r in (doc.get("conditions") or []) + (doc.get("measurements") or []) + (doc.get("outcomes") or []):
        if r.get("variable") == var and r.get("status") == "reported":
            v = (r.get("norm") or {}).get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def _entries_from_db(motor, inv_id):
    """Rebuild the 'user input' from what was stored, so the analysis uses exactly the saved data."""
    b = motor.srv.b
    inv = b.inv[inv_id]
    own = [e for e in b.col["experiments"] if e["investigation_id"] == inv_id]
    rows = []
    for e in own:
        row = {}
        for r in (e.get("conditions") or []) + (e.get("measurements") or []) + (e.get("outcomes") or []):
            v = (r.get("norm") or {}).get("value")
            if r.get("variable") in b.variables and isinstance(v, (int, float)) and not isinstance(v, bool):
                row[r["variable"]] = float(v)
        if row:
            rows.append(row)
    text = " ".join([inv.get("title") or "", inv.get("description") or "", " ".join(inv.get("keywords") or [])])
    chunks = " ".join(c["text"] for c in b.chunks if inv_id in (c.get("investigation_ids") or []))[:3000]
    grav = inv.get("gravity_environment")
    entry = {"archivo": inv_id, "tipo": "db", "paginas": 0, "asignaciones": [],
             "tablas": [{"tabla": "uploaded", "filas": rows}], "texto": (text + " " + chunks).strip(),
             "categoricos": {"combustibles": [inv["fuel"]] if inv.get("fuel") else [],
                             "gravedad": [GRAV_MAPA.get(grav, grav)] if grav else []},
             "fenomenos": detectar_fenomenos(text), "cantidades_en_texto": {}}
    return entry, own


def _reasons(motor, x, U):
    P = motor.srv.b.perfil_inv[x["investigacion"]]
    out = []
    if x["rangos"]:
        out.append("Shares " + ", ".join(_label(motor, v) for v in x["rangos"]))
    same = set(U["combustibles"]) & set(P["combustibles"])
    if same:
        out.append("Same fuel: " + ", ".join(sorted(same)))
    elif U["combustibles"]:
        out.append("Different fuel")
    g_p = {GRAV_MAPA.get(g, g) for g in P["gravedad"]}
    if set(U["gravedad"]) & g_p:
        out.append("Same gravity environment")
    outside = [_label(motor, v) for v, d in x["rangos"].items() if d["estado"] == "fuera"]
    if outside:
        out.append("Outside its tested range: " + ", ".join(outside))
    if x.get("configuracion_distinta"):
        out.append("Different setup (only conditions are comparable)")
    return out


def _analysis(motor, inv_id):
    b = motor.srv.b
    entry, own = _entries_from_db(motor, inv_id)
    r = motor.srv.afinidad([entry], texto="")
    U = r["perfil_usuario"]
    others = [x for x in r["investigaciones"] if x["investigacion"] != inv_id]      # never compare with itself
    invs = [{"investigation_id": x["investigacion"], "title": x["titulo"], "acronym": x["acronimo"],
             "total": x["afinidad_total"], "numeric": x["afinidad_numerica"], "semantic": x["afinidad_semantica"],
             "level": {"alta": "high", "media": "medium", "baja": "low"}.get(x["nivel_total"], "none"),
             "reasons": _reasons(motor, x, U), "user_upload": b.inv[x["investigacion"]].get("origin") == ORIGIN}
            for x in others]

    ranges = []
    for v, ur in U["rangos"].items():
        rows = []
        for x in others:
            P = b.perfil_inv[x["investigacion"]]["rangos"].get(v)
            if P:
                st = (x["rangos"].get(v) or {}).get("estado")
                rows.append({"investigation_id": x["investigacion"], "min": P["min"], "max": P["max"], "n": P["n"],
                             "status": {"dentro": "inside", "parcial": "partial", "fuera": "outside"}.get(st)})
        var = _var(motor, v)
        ranges.append({"variable": v, "label": _label(motor, v), "unit": _unit(motor, v), "log": bool(var and var.phi == "log"),
                       "yours": {"min": ur["min"], "max": ur["max"], "n": ur["n"]}, "investigations": rows})

    V = list(U["rangos"])
    pts = []
    for e in b.col["experiments"]:
        if e["investigation_id"] == inv_id:
            continue
        vals = {v: x for v in V if (x := _first_value(e, v)) is not None}
        if len(vals) >= 2:
            pts.append({"id": e["_id"], "inv": e["investigation_id"], "v": vals})
    pts = pts[:MAX_SCATTER_POINTS]
    mine = [{"id": e["_id"], "v": {v: x for v in V if (x := _first_value(e, v)) is not None}} for e in own]
    best, best_n = None, -1
    for i, a in enumerate(V):
        for c in V[i + 1:]:
            n = sum(1 for p in pts if a in p["v"] and c in p["v"])
            if n > best_n:
                best, best_n = (a, c), n
    scatter = {"variables": [{"variable": v, "label": _label(motor, v), "unit": _unit(motor, v),
                              "log": bool(_var(motor, v) and _var(motor, v).phi == "log")} for v in V],
               "default": list(best) if best else V[:2], "database": pts, "yours": mine}

    inv = b.inv[inv_id]
    rep = inv.get("upload_report") or {}
    overview = {"investigation_id": inv_id, "title": inv.get("title"), "description": inv.get("description"),
                "keywords": inv.get("keywords") or [], "doi": inv.get("doi"), "gravity_environment": inv.get("gravity_environment"),
                "fuel": inv.get("fuel"), "files": inv.get("files") or [], "experiments": len(own),
                "variables": [_label(motor, v) for v in V], "unmapped_columns": rep.get("unmapped_columns", []),
                "warnings": rep.get("warnings", []), "created_at": inv.get("created_at")}
    datasets = [d for d in r["datasets"] if d["investigacion"] != inv_id]
    findings = [h for h in r["hallazgos"] if h["investigacion"] != inv_id]
    provisional = r["parametros"].get("tolerancias_provisionales") or []
    return {"overview": overview, "affinity": invs, "ranges": ranges, "scatter": scatter,
            "datasets": [{"dataset_id": d["dataset"], "investigation_id": d["investigacion"], "title": d["titulo"],
                          "shared_variables": [_label(motor, v) for v in d["variables_compartidas"]],
                          "range_overlap": d["solape_rangos"], "close_experiments": d["vecinos_en_top"]} for d in datasets],
            "findings": [{"finding_id": h["hallazgo"], "investigation_id": h["investigacion"], "statement": h["enunciado"]} for h in findings],
            "notes": [], "provisional_tolerances": [_label(motor, v) for v in provisional],
            "semantic_backend": r["semantica"]["backend"]}, U, others


@investigations_api.get("/<inv_id>/analysis")
def analysis(inv_id):
    motor = _motor()
    if motor is None:
        return jsonify(error="The analysis engine is not available: " + str(current_app.extensions.get("motor_error"))), 503
    if inv_id not in motor.srv.b.inv:
        return jsonify(error=f"{inv_id} is not in the database."), 404
    a, _, _ = _analysis(motor, inv_id)
    return jsonify(a)


# ------------------------------------------------------------------------------- annotations
SYSTEM = """You are the scientific assistant of Flame in Freefall (NASA PSI microgravity combustion data).
A user uploaded an investigation. You annotate it using ONLY the FACTS you receive; you know nothing else about it.
Write in English, precise and cautious.
Rules:
1. Cite IDs that appear in FACTS (investigation IDs like PSI-159, dataset IDs like DS:PSI-159:normal_flames_table,
   finding IDs like FND:PSI-159:001). Never invent IDs.
2. Do not write any number that does not appear in FACTS. Prefer the "shown" values.
3. "summary": how the upload relates to the database and in what sense (at most 80 words).
4. "notes": one per variable in FACTS.variables, saying whether the user's range was tested before and where.
5. "recommendations": at most 4 concrete next steps, based on FACTS.suggested_actions.
6. Similar conditions do not mean the same outcome. Say so when relevant."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "object", "properties": {
            "variable": {"type": "string"}, "note": {"type": "string"}, "citations": {"type": "array", "items": {"type": "string"}}},
            "required": ["variable", "note", "citations"]}},
        "recommendations": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"}, "citations": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "detail", "citations"]}},
    },
    "required": ["summary", "notes", "recommendations"],
}


def _facts(motor, a):
    variables = []
    for rg in a["ranges"]:
        v = rg["variable"]
        variables.append({
            "variable": v, "label": rg["label"],
            "yours": {"min": rg["yours"]["min"], "max": rg["yours"]["max"],
                      "shown": f"{_shown(motor, v, rg['yours']['min'])} to {_shown(motor, v, rg['yours']['max'])}"},
            "tested_by": [{"investigation": x["investigation_id"], "status": x["status"],
                           "shown": f"{_shown(motor, v, x['min'])} to {_shown(motor, v, x['max'])}", "experiments": x["n"]}
                          for x in rg["investigations"]]})
    actions = []
    for d in a["datasets"][:3]:
        actions.append({"action": "compare_dataset", "id": d["dataset_id"], "investigation": d["investigation_id"],
                        "title": d["title"], "shared": d["shared_variables"], "close_experiments": d["close_experiments"]})
    for v in variables:
        if not v["tested_by"]:
            actions.append({"action": "no_reference_data", "variable": v["label"]})
        elif all(t["status"] == "outside" for t in v["tested_by"]):
            actions.append({"action": "check_extrapolation", "variable": v["label"],
                            "investigations": [t["investigation"] for t in v["tested_by"]]})
    for h in a["findings"][:2]:
        actions.append({"action": "read_finding", "id": h["finding_id"], "statement": h["statement"]})
    if not a["overview"]["fuel"]:
        actions.append({"action": "add_fuel"})
    top = [x for x in a["affinity"] if (x["total"] or 0) > 0][:5]
    return {"upload": {"id": a["overview"]["investigation_id"], "title": a["overview"]["title"],
                       "fuel": a["overview"]["fuel"], "gravity": a["overview"]["gravity_environment"],
                       "experiments": a["overview"]["experiments"]},
            "most_related": [{"investigation": x["investigation_id"], "title": x["title"], "affinity": x["total"],
                              "level": x["level"], "reasons": x["reasons"]} for x in top],
            "variables": variables, "suggested_actions": actions,
            "provisional_tolerances": a["provisional_tolerances"]}


def _fallback(motor, a, facts):
    top = facts["most_related"]
    if top:
        t = top[0]
        summary = (f"The closest investigation in the database is {t['investigation']} ({t['title']}), "
                   f"with {t['level']} affinity. " + ("; ".join(t["reasons"][:2]) + "." if t["reasons"] else ""))
    else:
        summary = "No investigation in the database is related to this upload by conditions or description."
    notes = []
    for v in facts["variables"]:
        if not v["tested_by"]:
            notes.append({"variable": v["variable"], "note": f"No other investigation reports {v['label']}.", "citations": []})
            continue
        ins = [t for t in v["tested_by"] if t["status"] == "inside"]
        t = ins[0] if ins else v["tested_by"][0]
        word = {"inside": "is inside", "partial": "partly overlaps", "outside": "is outside"}.get(t["status"], "compares with")
        notes.append({"variable": v["variable"], "citations": [t["investigation"]],
                      "note": f"Your range ({v['yours']['shown']}) {word} the range tested in {t['investigation']} ({t['shown']})."})
    recs = []
    for x in facts["suggested_actions"][:4]:
        if x["action"] == "compare_dataset":
            recs.append({"title": "Compare with a dataset", "citations": [x["id"]],
                         "detail": f"{x['title']} ({x['investigation']}) shares {', '.join(x['shared']) or 'context'} with your data."})
        elif x["action"] == "check_extrapolation":
            recs.append({"title": "Check the extrapolation", "citations": x["investigations"],
                         "detail": f"Your {x['variable']} is outside every tested range; similar records may not apply."})
        elif x["action"] == "no_reference_data":
            recs.append({"title": "No reference data", "citations": [],
                         "detail": f"No investigation in the database measured {x['variable']}."})
        elif x["action"] == "read_finding":
            recs.append({"title": "Read a related finding", "citations": [x["id"]], "detail": x["statement"]})
        elif x["action"] == "add_fuel":
            recs.append({"title": "Add the fuel", "citations": [],
                         "detail": "No fuel was recognized; adding it to the description or keywords improves matching."})
    return {"summary": summary, "notes": notes, "recommendations": recs}


def _allowed_ids(facts):
    s = json.dumps(facts)
    found = re.findall(r"(?:FND|DS|CHK):[A-Za-z0-9_.:\-]+|PSI-\d+(?::[A-Za-z0-9_.:\-]+)?|USR-\d+", s)
    return {i.rstrip(".:-") for i in found}


def _checked(items, text_key, allowed, facts, valid_vars=None):
    kept, dropped = [], 0
    for it in items or []:
        txt = str(it.get(text_key, "")).strip()
        cits = [str(c).strip("[] ") for c in it.get("citations") or []]
        ok = bool(txt) and all(c in allowed for c in cits) and not validar(txt, facts)
        if valid_vars is not None and (it.get("variable") not in valid_vars or it.get("variable") in {k.get("variable") for k in kept}):
            ok = False                                   # unknown variable, or a second note for the same one
        if ok:
            kept.append({**{k: it[k] for k in it if k != "citations"}, "citations": cits})
        else:
            dropped += 1
    return kept, dropped


@investigations_api.post("/<inv_id>/annotations")
def annotations(inv_id):
    motor = _motor()
    if motor is None:
        return jsonify(error="The analysis engine is not available: " + str(current_app.extensions.get("motor_error"))), 503
    if inv_id not in motor.srv.b.inv:
        return jsonify(error=f"{inv_id} is not in the database."), 404
    db = _db()
    refresh = bool((request.get_json(silent=True) or {}).get("refresh"))
    cached = (db.investigations.find_one({"_id": inv_id}, {"annotations": 1}) or {}).get("annotations")
    if cached and not refresh:
        return jsonify({**cached, "cached": True})

    a, _, _ = _analysis(motor, inv_id)
    facts = _facts(motor, a)
    allowed = _allowed_ids(facts)
    fb = _fallback(motor, a, facts)
    source, note, dropped = "template", "", 0
    out = fb
    try:
        if not llm.servidores():
            raise RuntimeError("QWEN_URL is not configured")
        raw = json.loads(llm.generar(SYSTEM, "FACTS:\n" + json.dumps(facts, ensure_ascii=False, default=str),
                                     max_tokens=1100, esquema=SCHEMA))
        vars_ = {v["variable"] for v in facts["variables"]}
        notes, d1 = _checked(raw.get("notes"), "note", allowed, facts, vars_)
        recs, d2 = _checked(raw.get("recommendations"), "detail", allowed, facts)
        summary = str(raw.get("summary", "")).strip()
        if not summary or validar(summary, facts) or any(i not in allowed for i in _allowed_ids({"s": summary})):
            summary, d3 = fb["summary"], 1
        else:
            d3 = 0
        have = {n["variable"] for n in notes}                  # every variable gets a note, checked or deterministic
        notes += [n for n in fb["notes"] if n["variable"] not in have]
        out = {"summary": summary, "notes": notes, "recommendations": recs or fb["recommendations"]}
        dropped, source = d1 + d2 + d3, "qwen"
    except Exception as exc:  # noqa: BLE001
        note = f"{type(exc).__name__}: {str(exc)[:150]}"
    labels = {rg["variable"]: rg["label"] for rg in a["ranges"]}
    for n in out["notes"]:
        n["label"] = labels.get(n["variable"], n["variable"])
    result = {**out, "source": source, "removed_statements": dropped, "note": note, "created_at": _now()}
    try:
        db.investigations.update_one({"_id": inv_id}, {"$set": {"annotations": result}})
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("annotations not cached: %s", exc)
    return jsonify({**result, "cached": False})
