"""
Data for Home (index.html) and downloadable files.

    GET /api/dashboard                          -> {summary, investigations, points, findings, references}
    GET /api/dashboard/<inv_id>/summary.pdf     -> one-page-ish PDF summary of an investigation, built from the database
    GET /api/files/<doc_id>                     -> the original file of a document/dataset (GridFS), or a redirect
                                                   to its external link

Everything is read from MongoDB on each request, so uploads appear without restarting.
The PDF summary needs the 'reportlab' package (pip install reportlab).
"""
from __future__ import annotations

import io
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, redirect, send_file

dashboard_api = Blueprint("dashboard_api", __name__)

PRESSURE, OXYGEN = "pressure", "ambient_oxygen_mole_fraction"
REFERENCES = [
    {"key": "exploration", "name": "Exploration atmosphere", "pressure_kpa": 56.5, "oxygen_pct": 34.0},
    {"key": "iss", "name": "ISS cabin", "pressure_kpa": 101.3, "oxygen_pct": 21.0},
]
MAX_FINDINGS = 6
SKIP_FINDING_TYPES = {"method_note"}
PDF_TYPES = {"report", "engineering_document", "publication"}


# ------------------------------------------------------------------------------ helpers
def _db():
    return current_app.extensions["db"]


def _records(doc):
    return (doc.get("conditions") or []) + (doc.get("measurements") or []) + (doc.get("outcomes") or [])


def _value(doc, var):
    for r in _records(doc):
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


def _date(v):
    """Mongo dates arrive as datetime; seed-style {'$date': '...'} is accepted too."""
    if isinstance(v, dict):
        v = v.get("$date")
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, str):
        return v[:10]
    return None


def _shown(value, unit, var=""):
    if value is None:
        return "n/a"
    if unit == "Pa":
        return f"{value / 1000:.1f} kPa" if value < 1e6 else f"{value / 1e6:.2f} MPa"
    if unit == "1" and "fraction" in var:
        return f"{value * 100:.1f}%"
    if unit == "m":
        return f"{value * 1000:.2f} mm"
    if unit == "kg/s":
        return f"{value * 1e6:.2f} mg/s"
    return f"{value:.4g} {unit if unit and unit != '1' else ''}".strip()


def _file_available(db, location):
    if not location or location.get("storage") != "gridfs" or not location.get("file_id"):
        return False
    bucket = location.get("bucket") or "fs"
    try:
        return db[f"{bucket}.files"].find_one({"_id": location["file_id"]}, {"_id": 1}) is not None
    except Exception:  # noqa: BLE001
        return False


def _documents_for(db, inv_id):
    """Report / paper PDFs of an investigation and whether the file is really stored in this database."""
    out = []
    for d in db.documents.find({"investigation_ids": inv_id, "record_type": {"$in": sorted(PDF_TYPES)}},
                               {"title": 1, "record_type": 1, "location": 1, "doi": 1, "url": 1, "year": 1, "n_pages": 1}):
        loc = d.get("location") or {}
        stored = _file_available(db, loc)
        link = loc.get("url") or d.get("url") or (f"https://doi.org/{d['doi']}" if d.get("doi") else None)
        is_pdf = (loc.get("media_type") == "application/pdf") or str(loc.get("filename", "")).lower().endswith(".pdf")
        out.append({"id": d["_id"], "title": d.get("title") or loc.get("filename") or d["_id"], "type": d.get("record_type"),
                    "year": d.get("year"), "pages": d.get("n_pages"), "pdf": is_pdf, "stored": stored,
                    "external": link if not stored else None})
    # stored PDFs first, then reports, then the rest
    out.sort(key=lambda x: (not x["stored"], x["type"] != "report", x["type"] != "engineering_document", str(x["title"])))
    return out


# ---------------------------------------------------------------------------- dashboard
@dashboard_api.get("/api/dashboard")
def dashboard():
    db = _db()
    labels = {v["_id"]: (v.get("label") or v["_id"]) for v in db.variables.find({}, {"label": 1})}
    invs = list(db.investigations.find({}))

    counts, fuels, phen, grav, varcount = Counter(), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    origin = defaultdict(Counter)
    points = []
    for e in db.experiments.find({}, {"investigation_id": 1, "fuel": 1, "conditions": 1, "measurements": 1, "outcomes": 1,
                                      "data_origin": 1, "phenomenon": 1}):
        inv = e.get("investigation_id")
        counts[inv] += 1
        origin[inv][e.get("data_origin") or "unknown"] += 1
        if (f := (e.get("fuel") or {}).get("canonical")):
            fuels[inv][f] += 1
        if e.get("phenomenon") and e.get("phenomenon") != "unspecified":
            phen[inv][e["phenomenon"]] += 1
        for r in _records(e):
            if r.get("status") == "reported":
                if r.get("variable") == "gravity_environment":
                    grav[inv][str((r.get("norm") or {}).get("value"))] += 1
                else:
                    varcount[inv][r.get("variable")] += 1
        p, o2 = _value(e, PRESSURE), _value(e, OXYGEN)
        if p is not None and o2 is not None:
            points.append({"id": e["_id"], "investigation": inv, "fuel": (e.get("fuel") or {}).get("canonical"),
                           "pressure_kpa": round(p / 1000, 3), "oxygen_pct": round(o2 * 100, 2),
                           "hot_ignition": _outcome(e, "hot_ignition"), "termination": _outcome(e, "termination_mode")})

    datasets = Counter(d.get("investigation_id") for d in db.measurements_metadata.find({}, {"investigation_id": 1}))
    nfind = Counter(f.get("investigation_id") for f in db.scientific_findings.find({}, {"investigation_id": 1}))

    cards = []
    for d in invs:
        i = d["_id"]
        dates = d.get("dates") or {}
        docs = _documents_for(db, i)
        cards.append({
            "id": i, "title": d.get("title"), "acronym": d.get("acronym"),
            "user_upload": d.get("origin") == "user_upload",
            "type": d.get("project_type"), "area": d.get("sub_research_area") or d.get("research_area"),
            "center": d.get("managing_center"), "platform": d.get("flight_platform"),
            "hardware": [h for h in (d.get("hardware") or []) if h][:3],
            "start": _date(dates.get("start")), "end": _date(dates.get("end")),
            "released": _date(dates.get("public_release")) or _date(d.get("created_at")),
            "objective": d.get("objective") or d.get("description") or "",
            "doi": d.get("doi"),
            "contacts": [c.get("name") for c in (d.get("contacts") or []) if c.get("name")][:3],
            "experiments": counts.get(i, 0), "datasets": datasets.get(i, 0), "findings": nfind.get(i, 0),
            "simulated": origin[i].get("simulation", 0),
            "fuels": [f for f, _ in fuels[i].most_common(5)],
            "phenomena": [p for p, _ in phen[i].most_common(3)],
            "gravity": [g for g, _ in grav[i].most_common(2)],
            "variables": [labels.get(v, v) for v, _ in varcount[i].most_common(8)],
            "n_variables": len(varcount[i]),
            "documents": docs,
            "pdf_url": f"/api/dashboard/{i}/summary.pdf",
        })
    cards.sort(key=lambda x: (x["user_upload"], -x["experiments"], x["id"]))

    by_inv = defaultdict(list)
    for f in db.scientific_findings.find({}, {"investigation_id": 1, "statement": 1, "statement_type": 1}):
        if f.get("statement") and f.get("statement_type") not in SKIP_FINDING_TYPES:
            by_inv[f.get("investigation_id")].append({"id": f["_id"], "investigation": f.get("investigation_id"),
                                                      "statement": f.get("statement"), "type": f.get("statement_type")})
    findings = []
    while len(findings) < MAX_FINDINGS and any(by_inv.values()):
        for inv in sorted(by_inv):
            if by_inv[inv] and len(findings) < MAX_FINDINGS:
                findings.append(by_inv[inv].pop(0))

    return jsonify(
        summary={"investigations": len(cards), "experiments": sum(counts.values()), "points": len(points),
                 "user_uploads": sum(1 for x in cards if x["user_upload"])},
        investigations=cards, points=points, findings=findings, references=REFERENCES)


# ------------------------------------------------------------------------- original files
@dashboard_api.get("/api/files/<path:doc_id>")
def original_file(doc_id):
    db = _db()
    doc = db.documents.find_one({"_id": doc_id}, {"location": 1, "url": 1, "doi": 1}) or \
          db.measurements_metadata.find_one({"_id": doc_id}, {"location": 1})
    if not doc:
        return jsonify(error=f"{doc_id} is not in the database."), 404
    loc = doc.get("location") or {}
    if _file_available(db, loc):
        import gridfs
        stream = gridfs.GridFSBucket(db, bucket_name=loc.get("bucket") or "fs").open_download_stream(loc["file_id"])
        return send_file(io.BytesIO(stream.read()), mimetype=loc.get("media_type") or "application/octet-stream",
                         as_attachment=True, download_name=loc.get("filename") or f"{doc_id}.pdf")
    link = loc.get("url") or doc.get("url") or (f"https://doi.org/{doc['doi']}" if doc.get("doi") else None)
    if link:
        return redirect(link)
    return jsonify(error="The original file is listed in the database but its content was not loaded into it."), 404


# --------------------------------------------------------------------------- PDF summary
def _pdf_summary(db, inv):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    inv_id = inv["_id"]
    esc = lambda s: (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    cyan, ink, muted, line = colors.HexColor("#0E7FB0"), colors.HexColor("#0F172A"), colors.HexColor("#5B6475"), colors.HexColor("#D5DAE3")
    st = {
        "eyebrow": ParagraphStyle("eb", fontName="Helvetica-Bold", fontSize=8, textColor=cyan, leading=10, spaceAfter=4),
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=17, textColor=ink, leading=21, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, textColor=ink, leading=13, spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9, textColor=ink, leading=12.5, alignment=TA_LEFT),
        "small": ParagraphStyle("s", fontName="Helvetica", fontSize=7.5, textColor=muted, leading=10),
        "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=8.3, textColor=ink, leading=10.5),
        "cellb": ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=8.3, textColor=muted, leading=10.5),
    }

    exps = list(db.experiments.find({"investigation_id": inv_id}))
    variables = {v["_id"]: v for v in db.variables.find({}, {"label": 1, "canonical_unit": 1, "role": 1})}
    ranges = defaultdict(list)
    fuels, outcomes = Counter(), defaultdict(Counter)
    for e in exps:
        if (f := (e.get("fuel") or {}).get("canonical")):
            fuels[f] += 1
        for r in _records(e):
            if r.get("status") != "reported":
                continue
            v = (r.get("norm") or {}).get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                ranges[r["variable"]].append(float(v))
            elif v is not None and r in (e.get("outcomes") or []):
                outcomes[r["variable"]][str(v)] += 1
    findings = list(db.scientific_findings.find({"investigation_id": inv_id}, {"statement": 1, "statement_type": 1}))
    dsets = list(db.measurements_metadata.find({"investigation_id": inv_id}, {"title": 1, "n_rows": 1, "experiment_ids": 1}))
    docs = _documents_for(db, inv_id)
    dates = inv.get("dates") or {}

    story = [Paragraph(f"{esc(inv_id)}{'  ·  ' + esc(inv.get('acronym')) if inv.get('acronym') else ''}  ·  "
                       f"{'USER UPLOAD (NOT REVIEWED)' if inv.get('origin') == 'user_upload' else 'NASA PHYSICAL SCIENCES INFORMATICS'}", st["eyebrow"]),
             Paragraph(esc(inv.get("title")), st["title"])]

    meta = [("Type", inv.get("project_type")), ("Area", inv.get("sub_research_area") or inv.get("research_area")),
            ("Managing center", inv.get("managing_center")), ("Platform", inv.get("flight_platform")),
            ("Hardware", ", ".join(inv.get("hardware") or [])), ("Period", " to ".join(x for x in (_date(dates.get("start")), _date(dates.get("end"))) if x)),
            ("Public release", _date(dates.get("public_release"))), ("DOI", inv.get("doi")),
            ("Contacts", ", ".join(c.get("name", "") for c in (inv.get("contacts") or [])[:4])),
            ("In this database", f"{len(exps)} experiments · {len(dsets)} datasets · {len(findings)} findings")]
    meta = [[Paragraph(k, st["cellb"]), Paragraph(esc(v), st["cell"])] for k, v in meta if v]
    t = Table(meta, colWidths=[38 * mm, 132 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -2), .4, line),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 4), t]

    for key, label in (("objective", "Objective"), ("hypothesis", "Hypothesis"), ("approach", "Approach"), ("description", "Description")):
        txt = inv.get(key)
        if txt and not (key == "description" and inv.get("objective") == txt):
            txt = re.sub(r"\s+", " ", str(txt))
            story += [Paragraph(label, st["h2"]), Paragraph(esc(txt[:1600] + ("…" if len(txt) > 1600 else "")), st["body"])]

    if ranges:
        rows = [[Paragraph(x, st["cellb"]) for x in ("Variable", "Tested range", "Experiments")]]
        for var, xs in sorted(ranges.items(), key=lambda kv: -len(kv[1]))[:14]:
            meta_v = variables.get(var, {})
            unit = meta_v.get("canonical_unit")
            rng = _shown(min(xs), unit, var) if min(xs) == max(xs) else f"{_shown(min(xs), unit, var)} to {_shown(max(xs), unit, var)}"
            rows.append([Paragraph(esc(meta_v.get("label") or var), st["cell"]), Paragraph(esc(rng), st["cell"]), Paragraph(str(len(xs)), st["cell"])])
        t = Table(rows, colWidths=[72 * mm, 72 * mm, 26 * mm], repeatRows=1)
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), .8, ink), ("LINEBELOW", (0, 1), (-1, -2), .3, line),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5), ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story += [KeepTogether([Paragraph("Conditions and measurements in the database", st["h2"]), t])]

    extra = []
    if fuels:
        extra.append("Fuels: " + ", ".join(f"{f.replace('_', ' ')} ({n})" for f, n in fuels.most_common(6)))
    for var, c in outcomes.items():
        extra.append(f"{(variables.get(var, {}).get('label') or var)}: " + ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in c.most_common(5)))
    if extra:
        story += [Paragraph("Fuels and recorded outcomes", st["h2"])] + [Paragraph(esc(x), st["body"]) for x in extra]

    if findings:
        story.append(Paragraph("Findings", st["h2"]))
        for f in findings[:10]:
            story.append(Paragraph(f"• {esc(f.get('statement'))} <font color='#5B6475' size='7.5'>[{esc(f['_id'])}]</font>", st["body"]))

    if dsets:
        story.append(Paragraph("Datasets", st["h2"]))
        for d in dsets[:10]:
            story.append(Paragraph(f"• {esc(d.get('title') or d['_id'])} <font color='#5B6475' size='7.5'>"
                                   f"({len(d.get('experiment_ids') or [])} experiments) [{esc(d['_id'])}]</font>", st["body"]))

    if docs:
        story.append(Paragraph("Reports and publications", st["h2"]))
        for d in docs[:12]:
            where = "file stored in this database" if d["stored"] else (d["external"] or "listed, file not loaded")
            story.append(Paragraph(f"• {esc(d['title'])} <font color='#5B6475' size='7.5'>({esc(d['type'].replace('_', ' '))}; {esc(where)})</font>", st["body"]))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.line(20 * mm, 14 * mm, 190 * mm, 14 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(muted)
        canvas.drawString(20 * mm, 10 * mm, f"PSI Scientific Advisor · generated {now} from the project database · "
                                            "tested conditions, not a safety certification")
        canvas.drawRightString(190 * mm, 10 * mm, f"{doc_.page}")
        canvas.setFillColor(cyan)
        canvas.rect(20 * mm, 285 * mm, 170 * mm, 1.2 * mm, stroke=0, fill=1)
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
                      title=f"{inv_id} summary", author="PSI Scientific Advisor").build(story, onFirstPage=footer, onLaterPages=footer)
    buf.seek(0)
    return buf


@dashboard_api.get("/api/dashboard/<inv_id>/summary.pdf")
def summary_pdf(inv_id):
    db = _db()
    inv = db.investigations.find_one({"_id": inv_id})
    if not inv:
        return jsonify(error=f"{inv_id} is not in the database."), 404
    try:
        buf = _pdf_summary(db, inv)
    except ImportError:
        return jsonify(error="PDF generation needs the reportlab package: pip install reportlab"), 501
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{inv_id}_summary.pdf")
