/* Suggestions (suggestion.html): an uploaded investigation in the context of the database.
 *   GET  /api/investigations                    -> {investigations: [{investigation_id, title, created_at}]}
 *   GET  /api/investigations/<id>/analysis      -> overview, affinity, ranges, scatter (numbers from the database)
 *   POST /api/investigations/<id>/annotations   -> summary, notes, recommendations (Qwen, checked against the facts)
 * Numbers in the charts never come from the language model.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Up to d decimals, but never fewer than 3 significant digits (small values would otherwise show as 0).
  const fmt = (n, d = 2) => {
    const x = Number(n);
    if (x !== 0 && Math.abs(x) < 1) return Number(x.toPrecision(3)).toLocaleString("en-US", { maximumFractionDigits: 12 });
    return x.toLocaleString("en-US", { maximumFractionDigits: d });
  };
  const score = (x) => (x == null ? "n/a" : Number(x).toFixed(2));
  const SVGNS = "http://www.w3.org/2000/svg";
  const PALETTE = ["#2563eb", "#0d9488", "#7c3aed", "#ca8a04", "#db2777", "#64748b", "#16a34a", "#9333ea"];
  const GRAVITY = { microgravity_iss: "Microgravity (ISS)", normal_gravity_lab: "Ground lab", simulated_microgravity: "Simulation",
                    microgravity: "Microgravity", normal_gravity: "Normal gravity" };

  let analysis = null;
  let currentId = null;
  const invColor = {};

  /* --------------------------------------------------------------- units */
  // Values arrive in canonical units (Pa, mole fraction, m, kg/s, K, s). Same conversions as before.
  function display(variable, value, unit) {
    if (value == null) return "not reported";
    if (unit === "Pa") return value >= 1e6 ? `${fmt(value / 1e6, 2)} MPa` : `${fmt(value / 1000, 1)} kPa`;
    if (unit === "1" && /fraction/.test(variable)) return `${fmt(value * 100, 1)}%`;
    if (unit === "kg/s") return `${fmt(value * 1e6, 2)} mg/s`;
    if (unit === "m") return `${fmt(value * 1000, 2)} mm`;
    return `${fmt(value, 3)} ${unit && unit !== "1" ? unit : ""}`.trim();
  }

  /* ---------------------------------------------------------------- http */
  async function call(method, url, body) {
    let r;
    try {
      r = await fetch(url, { method, headers: { "Content-Type": "application/json", Accept: "application/json" },
                             body: body ? JSON.stringify(body) : undefined });
    } catch (_) {
      throw new Error("Could not reach the server. Check that Flask is running and try again.");
    }
    let data = {};
    try { data = await r.json(); } catch (_) { /* non-JSON error page */ }
    if (r.ok) return data;
    if (r.status === 404 && !data.error) throw new Error("The investigations service is not connected. Register the /api/investigations routes in Flask.");
    throw new Error(data.error || `The server returned error ${r.status}. Try again.`);
  }

  /* -------------------------------------------------------------- picker */
  async function init() {
    const sel = $("invSelect");
    let list = [];
    try {
      list = (await call("GET", "/api/investigations")).investigations || [];
    } catch (err) {
      $("pickMessage").textContent = err.message;
      sel.innerHTML = '<option value="">Unavailable</option>';
      return;
    }
    if (!list.length) {
      sel.innerHTML = '<option value="">None yet</option>';
      $("emptyState").hidden = false;
      return;
    }
    sel.innerHTML = list.map((x) => `<option value="${esc(x.investigation_id)}">${esc(x.investigation_id)}: ${esc(x.title || "")}</option>`).join("");
    sel.disabled = false;
    const wanted = new URLSearchParams(location.search).get("investigation");
    sel.value = list.some((x) => x.investigation_id === wanted) ? wanted : list[0].investigation_id;
    sel.addEventListener("change", () => load(sel.value));
    load(sel.value);
  }

  async function load(id) {
    currentId = id;
    $("pickMessage").textContent = "";
    history.replaceState(null, "", `?investigation=${encodeURIComponent(id)}`);
    $("view").hidden = true;
    try {
      const a = await call("GET", `/api/investigations/${encodeURIComponent(id)}/analysis`);
      if (id !== currentId) return;
      analysis = a;
      (a.affinity || []).forEach((x, i) => { invColor[x.investigation_id] = PALETTE[i % PALETTE.length]; });
      renderOverview(a.overview);
      renderAffinity(a.affinity || []);
      renderRanges(a.ranges || []);
      setupScatter(a.scatter);
      const foot = [];
      if ((a.provisional_tolerances || []).length) foot.push(`Similarity tolerances are provisional for ${a.provisional_tolerances.join(", ")} until a domain expert sets them.`);
      if (a.semantic_backend === "tfidf") foot.push("Meaning-based affinity is using word matching only (embeddings not configured).");
      $("pageFoot").textContent = foot.join(" ");
      $("view").hidden = false;
      loadNotes(id, false);
    } catch (err) {
      $("pickMessage").textContent = err.message;
    }
  }

  /* ------------------------------------------------------------ overview */
  function renderOverview(o) {
    $("ovId").textContent = `${o.investigation_id} · User upload`;
    $("ovTitle").textContent = o.title || o.investigation_id;
    const facts = [
      ["Fuel", o.fuel ? o.fuel.replace(/_/g, " ") : "not recognized"],
      ["Environment", GRAVITY[o.gravity_environment] || (o.gravity_environment ? o.gravity_environment.replace(/_/g, " ") : "not specified")],
      ["Experiments read", o.experiments],
      ["Variables", (o.variables || []).join(", ") || "none recognized"],
    ];
    if (o.doi) facts.push(["Publication", o.doi]);
    $("ovFacts").innerHTML = facts.map(([k, v]) => `<li><span class="muted-text">${esc(k)}</span> <strong>${esc(v)}</strong></li>`).join("");
    $("ovDesc").textContent = o.description || "";
    $("ovKeywords").innerHTML = (o.keywords || []).map((k) => `<li class="chip">${esc(k)}</li>`).join("");
    const files = o.files || [];
    $("ovFilesSummary").textContent = `${files.length} ${files.length === 1 ? "file" : "files"}`;
    $("ovFiles").innerHTML = files.map((f) => `<li>${esc(f.name)} <span class="muted-text">${esc((f.role || "").replace(/_/g, " "))}</span></li>`).join("");
    const warn = (o.warnings || []).concat((o.unmapped_columns || []).length ? [`Columns not recognized: ${o.unmapped_columns.join("; ")}.`] : []);
    $("ovWarnings").hidden = !warn.length;
    $("ovWarnings").textContent = warn.join(" ");
  }

  /* ---------------------------------------------------------- annotations */
  function cites(ids) {
    return (ids || []).map((id) => `<span class="cite" title="${esc(id)}">${esc(id)}</span>`).join(" ");
  }

  async function loadNotes(id, refresh) {
    const box = document.querySelector(".ins-notes");
    box.setAttribute("aria-busy", "true");
    $("annStatus").textContent = "Writing…";
    $("annStatus").className = "tag";
    $("annSummary").textContent = "The assistant is reading the comparison. This can take 10 to 30 seconds.";
    $("annNotes").innerHTML = ""; $("annRecs").innerHTML = ""; $("annFoot").textContent = "";
    $("annRefresh").hidden = true;
    try {
      const n = await call("POST", `/api/investigations/${encodeURIComponent(id)}/annotations`, { refresh });
      if (id !== currentId) return;
      $("annSummary").textContent = n.summary || "";
      $("annNotes").innerHTML = (n.notes || []).map((x) => `
        <li><span class="ins-note-var">${esc(x.label || x.variable)}</span>
            <span>${esc(x.note)} ${cites(x.citations)}</span></li>`).join("") || '<li class="muted-text">No variables to annotate.</li>';
      $("annRecs").innerHTML = (n.recommendations || []).map((x) => `
        <li><strong>${esc(x.title)}</strong> <span>${esc(x.detail)}</span> ${cites(x.citations)}</li>`).join("") || '<li class="muted-text">No recommendations.</li>';
      const foot = [n.source === "qwen" ? "Written by the assistant and checked against the database." : "Built directly from the database (the assistant was not available)."];
      if (n.removed_statements) foot.push(`${n.removed_statements} ${n.removed_statements === 1 ? "statement was" : "statements were"} removed because ${n.removed_statements === 1 ? "it was" : "they were"} not backed by the data.`);
      if (n.cached) foot.push("Saved earlier.");
      $("annFoot").textContent = foot.join(" ");
      $("annStatus").textContent = n.source === "qwen" ? "Checked" : "From data";
      $("annStatus").className = "tag ins-tag-ok";
      $("annRefresh").hidden = false;
    } catch (err) {
      $("annSummary").textContent = err.message;
      $("annStatus").textContent = "Error";
      $("annRefresh").hidden = false;
    } finally {
      box.setAttribute("aria-busy", "false");
    }
  }
  $("annRefresh").addEventListener("click", () => currentId && loadNotes(currentId, true));

  /* ------------------------------------------------------------- affinity */
  function renderAffinity(list) {
    const rows = list.filter((x) => x.total != null).slice(0, 8);
    $("affChart").innerHTML = rows.length ? rows.map((x) => `
      <li class="ins-aff-row">
        <div class="ins-aff-label">
          <span class="ins-dot" style="background:${invColor[x.investigation_id]}"></span>
          <strong>${esc(x.investigation_id)}</strong>
          <span class="muted-text ins-trunc" title="${esc(x.title || "")}">${esc(x.acronym || x.title || "")}</span>
          ${x.user_upload ? '<span class="tag">User upload</span>' : ""}
        </div>
        <div class="ins-aff-bar" role="img" aria-label="Affinity ${score(x.total)} of 1">
          <span class="ins-level-${esc(x.level)}" style="width:${Math.round((x.total || 0) * 100)}%"></span>
        </div>
        <div class="ins-aff-num"><span class="num">${score(x.total)}</span> <span class="muted-text">${esc(x.level)}</span></div>
        <p class="muted-text ins-aff-why">Conditions ${score(x.numeric)} · Meaning ${score(x.semantic)}${
          (x.reasons || []).length ? " · " + x.reasons.map(esc).join(" · ") : ""}</p>
      </li>`).join("") : '<li class="muted-text">No investigation could be compared.</li>';
  }

  /* --------------------------------------------------------------- ranges */
  // values: every value the axis must show. On a log axis, zero or negative values are pinned to the smallest positive one.
  function scaler(values, log, a, b) {
    const f = log ? Math.log10 : (v) => v;
    const pos = values.filter((v) => v > 0);
    const floor = log && pos.length ? Math.min(...pos) : 1e-12;
    const lo = log ? floor : Math.min(...values), hi = Math.max(...values, log ? floor : -Infinity);
    let L = f(lo), H = f(hi);
    if (H - L < 1e-12) { L -= 0.5; H += 0.5; }
    const pad = (H - L) * 0.05; L -= pad; H += pad;
    const s = (v) => a + ((f(log ? Math.max(v, floor) : v) - L) / (H - L)) * (b - a);
    s.inv = (p) => { const t = L + ((p - a) / (b - a)) * (H - L); return log ? 10 ** t : t; };
    return s;
  }

  function svg(w, h, cls) {
    const el = document.createElementNS(SVGNS, "svg");
    el.setAttribute("viewBox", `0 0 ${w} ${h}`);
    el.setAttribute("class", cls);
    el.setAttribute("role", "img");
    return el;
  }

  function renderRanges(ranges) {
    const box = $("rangeChart");
    box.innerHTML = "";
    if (!ranges.length) { box.innerHTML = '<p class="muted-text">No numeric variables were recognized in your files.</p>'; return; }
    ranges.forEach((rg) => {
      const rows = [{ label: "You", min: rg.yours.min, max: rg.yours.max, you: true }]
        .concat(rg.investigations.map((x) => ({ label: x.investigation_id, min: x.min, max: x.max, status: x.status, n: x.n })));
      const W = 640, left = 92, right = 24, rowH = 22, top = 8, H = top + rows.length * rowH + 30;
      const all = rows.flatMap((r) => [r.min, r.max]);
      const sx = scaler(all, rg.log, left, W - right);
      const el = svg(W, H, "ins-range-svg");
      el.setAttribute("aria-label", `${rg.label}: your range compared with tested ranges`);
      let html = "";
      rows.forEach((r, i) => {
        const y = top + i * rowH + rowH / 2;
        const x1 = sx(r.min), x2 = Math.max(sx(r.max), x1 + 3);
        const color = r.you ? "var(--ins-you)" : invColor[r.label] || "var(--ins-muted)";
        html += `<text x="${left - 8}" y="${y + 4}" text-anchor="end" class="ins-svg-label">${esc(r.label)}</text>`;
        html += `<line x1="${left}" x2="${W - right}" y1="${y}" y2="${y}" class="ins-svg-track"/>`;
        html += `<rect x="${x1}" y="${y - 6}" width="${x2 - x1}" height="12" rx="3" fill="${color}" opacity="${r.you ? 1 : 0.75}">
                   <title>${esc(r.label)}: ${esc(display(rg.variable, r.min, rg.unit))} to ${esc(display(rg.variable, r.max, rg.unit))}${
                     r.status ? ` (yours is ${r.status})` : ""}${r.n ? `, ${r.n} experiments` : ""}</title></rect>`;
      });
      const yAxis = top + rows.length * rowH + 6;
      [0, 0.5, 1].forEach((t) => {
        const px = left + t * (W - right - left);
        html += `<text x="${px}" y="${yAxis + 14}" text-anchor="${t === 0 ? "start" : t === 1 ? "end" : "middle"}" class="ins-svg-tick">${esc(display(rg.variable, sx.inv(px), rg.unit))}</text>`;
      });
      el.innerHTML = html;
      const fig = document.createElement("figure");
      fig.className = "ins-range";
      const youStatus = rg.investigations.length ? "" : " · not measured by any other investigation";
      fig.innerHTML = `<figcaption><strong>${esc(rg.label)}</strong> <span class="muted-text">yours: ${esc(display(rg.variable, rg.yours.min, rg.unit))}${
        rg.yours.max !== rg.yours.min ? ` to ${esc(display(rg.variable, rg.yours.max, rg.unit))}` : ""}${rg.log ? " · log scale" : ""}${youStatus}</span></figcaption>`;
      fig.appendChild(el);
      box.appendChild(fig);
    });
  }

  /* -------------------------------------------------------------- scatter */
  function setupScatter(sc) {
    const vars = (sc && sc.variables) || [];
    const xs = $("xVar"), ys = $("yVar");
    if (vars.length < 2) {
      xs.innerHTML = ys.innerHTML = "";
      xs.disabled = ys.disabled = true;
      $("scatter").innerHTML = '<p class="muted-text">At least two numeric variables are needed for this chart.</p>';
      $("scatterLegend").innerHTML = ""; $("scatterFoot").textContent = "";
      return;
    }
    const opts = vars.map((v) => `<option value="${esc(v.variable)}">${esc(v.label)}</option>`).join("");
    xs.innerHTML = ys.innerHTML = opts;
    xs.disabled = ys.disabled = false;
    xs.value = sc.default[0]; ys.value = sc.default[1];
    xs.onchange = ys.onchange = () => drawScatter(sc);
    drawScatter(sc);
  }

  function drawScatter(sc) {
    const xv = $("xVar").value, yv = $("yVar").value;
    const meta = Object.fromEntries(sc.variables.map((v) => [v.variable, v]));
    const box = $("scatter");
    if (xv === yv) { box.innerHTML = '<p class="muted-text">Choose two different variables.</p>'; return; }
    const db = sc.database.filter((p) => p.v[xv] != null && p.v[yv] != null);
    const mine = sc.yours.filter((p) => p.v[xv] != null && p.v[yv] != null);
    const all = db.concat(mine);
    if (!all.length) { box.innerHTML = '<p class="muted-text">No experiments report both variables.</p>'; return; }
    const W = 640, H = 400, L = 70, R = 16, T = 12, B = 46;
    const X = all.map((p) => p.v[xv]), Y = all.map((p) => p.v[yv]);
    const sx = scaler(X, meta[xv].log, L, W - R);
    const sy = scaler(Y, meta[yv].log, H - B, T);
    const el = svg(W, H, "ins-scatter-svg");
    el.setAttribute("aria-label", `${meta[xv].label} versus ${meta[yv].label}`);
    let html = "";
    for (let i = 0; i <= 4; i++) {
      const px = L + (i / 4) * (W - R - L), py = (H - B) - (i / 4) * (H - B - T);
      html += `<line x1="${px}" x2="${px}" y1="${T}" y2="${H - B}" class="ins-svg-grid"/>`;
      html += `<line x1="${L}" x2="${W - R}" y1="${py}" y2="${py}" class="ins-svg-grid"/>`;
      html += `<text x="${px}" y="${H - B + 16}" text-anchor="middle" class="ins-svg-tick">${esc(display(xv, sx.inv(px), meta[xv].unit))}</text>`;
      html += `<text x="${L - 6}" y="${py + 4}" text-anchor="end" class="ins-svg-tick">${esc(display(yv, sy.inv(py), meta[yv].unit))}</text>`;
    }
    html += `<text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle" class="ins-svg-label">${esc(meta[xv].label)}${meta[xv].log ? " (log)" : ""}</text>`;
    html += `<text transform="translate(14 ${(T + H - B) / 2}) rotate(-90)" text-anchor="middle" class="ins-svg-label">${esc(meta[yv].label)}${meta[yv].log ? " (log)" : ""}</text>`;
    db.forEach((p) => {
      html += `<circle cx="${sx(p.v[xv]).toFixed(1)}" cy="${sy(p.v[yv]).toFixed(1)}" r="3.5" fill="${invColor[p.inv] || "var(--ins-muted)"}" opacity="0.55">
                 <title>${esc(p.id)}: ${esc(display(xv, p.v[xv], meta[xv].unit))}, ${esc(display(yv, p.v[yv], meta[yv].unit))}</title></circle>`;
    });
    mine.forEach((p) => {
      html += `<circle cx="${sx(p.v[xv]).toFixed(1)}" cy="${sy(p.v[yv]).toFixed(1)}" r="6" class="ins-you-pt">
                 <title>Yours, ${esc(p.id)}: ${esc(display(xv, p.v[xv], meta[xv].unit))}, ${esc(display(yv, p.v[yv], meta[yv].unit))}</title></circle>`;
    });
    el.innerHTML = html;
    box.innerHTML = "";
    box.appendChild(el);
    const invs = [...new Set(db.map((p) => p.inv))];
    $("scatterLegend").innerHTML = `<li><span class="ins-dot ins-dot-you"></span>Your data (${mine.length})</li>` +
      invs.map((i) => `<li><span class="ins-dot" style="background:${invColor[i] || "var(--ins-muted)"}"></span>${esc(i)} (${db.filter((p) => p.inv === i).length})</li>`).join("");
    $("scatterFoot").textContent = mine.length ? "" : "Your files do not report both of these variables in the same row.";
  }

  init();
})();
