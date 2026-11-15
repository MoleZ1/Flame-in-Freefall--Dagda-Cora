/* Home (index.html)
 *   GET /api/dashboard                       -> investigation cards, points (pressure × oxygen), findings, references
 *   GET /api/dashboard/<id>/summary.pdf      -> PDF summary of one investigation (built from the database)
 *   GET /api/files/<document id>             -> original report PDF when it is stored in the database
 * No external libraries. Numbers come from MongoDB.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 1) => Number(n).toLocaleString("en-US", { maximumFractionDigits: d });
  const human = (s) => String(s ?? "").replace(/_/g, " ");
  const PALETTE = ["#38BDF8", "#2DD4BF", "#A78BFA", "#F472B6", "#FACC15", "#4ADE80", "#60A5FA", "#F97316"];
  const UPLOAD_COLOR = "#FB923C";
  const GRAVITY = { microgravity_iss: "Microgravity (ISS)", normal_gravity_lab: "Ground lab", simulated_microgravity: "Simulation", microgravity: "Microgravity" };
  const TOL_O2 = 3;     // "close" to an atmosphere: ±3 oxygen points…
  const TOL_P = 1.42;   // …and pressure within ×/÷ 1.42 (same idea as the matching engine)

  let data = null;
  const color = {};

  /* ---------------------------------------------------------------- load */
  async function load() {
    try {
      const r = await fetch("/api/dashboard", { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(r.status === 404 ? "The /api/dashboard route is not registered in Flask." : `Server error ${r.status}.`);
      data = await r.json();
    } catch (err) {
      $("sourceNote").hidden = false;
      $("sourceNote").textContent = `Could not load the database: ${err.message}`;
      $("invCards").innerHTML = "";
      return;
    }
    let k = 0;
    data.investigations.forEach((inv) => { color[inv.id] = inv.user_upload ? UPLOAD_COLOR : PALETTE[k++ % PALETTE.length]; });

    const s = data.summary;
    $("sourceNote").hidden = false;
    $("sourceNote").textContent = `${fmt(s.experiments, 0)} experiments from ${s.investigations} investigations in the database` +
      (s.user_uploads ? `, ${s.user_uploads} of them uploaded by users` : "") + ".";
    $("invCount").textContent = `${s.investigations} in the database`;

    renderCards();
    renderFindings();
    setupMission();
  }

  /* --------------------------------------------------------------- cards */
  function stat(value, label) {
    return `<div class="inv-stat"><span class="inv-stat-num">${fmt(value, 0)}</span><span class="inv-stat-label">${esc(label)}</span></div>`;
  }

  function card(inv, i) {
    const years = inv.start || inv.end ? [inv.start, inv.end].filter(Boolean).map((d) => d.slice(0, 4)).join("–") : (inv.released ? `Released ${inv.released.slice(0, 4)}` : "");
    const meta = [inv.type, inv.platform && inv.platform !== "Not Applicable" ? inv.platform : null, inv.center, years].filter(Boolean);
    const chips = inv.fuels.map((f) => `<li class="chip">${esc(human(f))}</li>`).join("");
    const env = inv.gravity.map((g) => GRAVITY[g] || human(g)).join(", ");
    const vars = inv.variables.slice(0, 6).map(esc).join(" · ") + (inv.n_variables > 6 ? ` · +${inv.n_variables - 6} more` : "");
    const stored = inv.documents.filter((d) => d.stored && d.pdf);
    const external = inv.documents.filter((d) => !d.stored && d.external).length;
    const reportBtn = stored.length
      ? `<a class="btn btn-ghost inv-btn" href="/api/files/${encodeURIComponent(stored[0].id)}" title="${esc(stored[0].title)}">Original report<span class="inv-btn-meta">PDF${stored[0].pages ? ` · ${stored[0].pages} p.` : ""}</span></a>`
      : "";
    const analyze = inv.user_upload ? `<a class="btn btn-ghost inv-btn" href="/suggestion?investigation=${encodeURIComponent(inv.id)}">Analyze</a>` : "";

    return `
    <article class="inv-card${inv.user_upload ? " is-upload" : ""}" style="--inv:${color[inv.id]}; animation-delay:${i * 70}ms">
      <header class="inv-card-head">
        <span class="inv-id"><span class="inv-dot"></span>${esc(inv.id)}${inv.acronym ? ` <span class="inv-acr">${esc(inv.acronym)}</span>` : ""}</span>
        ${inv.user_upload ? '<span class="tag inv-tag-upload">User upload</span>' : inv.area ? `<span class="tag">${esc(inv.area)}</span>` : ""}
      </header>
      <h3 class="inv-title">${esc(inv.title || inv.id)}</h3>
      ${meta.length ? `<p class="inv-meta">${meta.map(esc).join('<span aria-hidden="true"> · </span>')}</p>` : ""}

      <div class="inv-stats">
        ${stat(inv.experiments, "experiments")}
        ${stat(inv.datasets, "datasets")}
        ${stat(inv.findings, "findings")}
      </div>

      ${inv.objective ? `<details class="inv-objective"><summary><span class="inv-objective-text">${esc(inv.objective)}</span></summary>
         <p>${esc(inv.objective)}</p></details>` : ""}

      <dl class="inv-facts">
        ${chips ? `<div><dt>Fuels</dt><dd><ul class="chips-list">${chips}</ul></dd></div>` : ""}
        ${env ? `<div><dt>Environment</dt><dd>${esc(env)}</dd></div>` : ""}
        ${vars ? `<div><dt>Measures</dt><dd>${vars}</dd></div>` : ""}
        ${inv.hardware.length ? `<div><dt>Hardware</dt><dd>${esc(inv.hardware.join(", "))}</dd></div>` : ""}
        ${inv.contacts.length ? `<div><dt>Contacts</dt><dd>${esc(inv.contacts.join(", "))}</dd></div>` : ""}
      </dl>

      <footer class="inv-actions">
        <a class="btn btn-primary inv-btn" href="${esc(inv.pdf_url)}" download>Download PDF<span class="inv-btn-meta">summary</span></a>
        ${reportBtn}${analyze}
        ${inv.doi ? `<a class="inv-link" href="https://doi.org/${encodeURIComponent(inv.doi).replace(/%2F/g, "/")}" target="_blank" rel="noopener">DOI ${esc(inv.doi)} ↗</a>` : ""}
        ${external ? `<span class="inv-link-muted">${external} publication${external === 1 ? "" : "s"} linked</span>` : ""}
      </footer>
    </article>`;
  }

  function renderCards() {
    $("invCards").innerHTML = data.investigations.map(card).join("") ||
      '<p class="muted-text">The database has no investigations yet. <a href="/insertion">Add one</a>.</p>';
  }

  /* ------------------------------------------------------------ findings */
  function renderFindings() {
    $("findingList").innerHTML = data.findings.map((f) => `
      <li>
        <p class="finding-text">${esc(f.statement)}</p>
        <p class="muted-text"><span class="swatch-inline" style="background:${color[f.investigation] || "#8A93A6"}"></span>${esc(f.investigation)}${
          f.type ? ` · ${esc(human(f.type))}` : ""}</p>
      </li>`).join("") || '<li class="muted-text">No findings recorded yet.</li>';
  }

  /* --------------------------------------------------- crew atmosphere */
  function outcomeText(p) {
    const ign = p.hot_ignition == null ? "" : `ignition: ${p.hot_ignition}`;
    const end = p.termination === "self_extinguished" ? "went out on its own" : p.termination === "flow_terminated" ? "burned until the fuel was cut" : "";
    return [ign, end].filter(Boolean).join(", ");
  }

  function nearest(p, o2) {
    let best = null, bestD = Infinity;
    const close = [];
    data.points.forEach((pt) => {
      const dz = Math.max(Math.abs(pt.oxygen_pct - o2) / TOL_O2, Math.abs(Math.log(pt.pressure_kpa / p)) / Math.log(TOL_P));
      if (dz < bestD) { bestD = dz; best = pt; }
      if (dz <= 1) close.push(pt);
    });
    return { best, close };
  }

  function showMission() {
    const key = $("missionSelect").value;
    let p, o2, label;
    if (key === "custom") {
      p = parseFloat(String($("customP").value).replace(",", "."));
      o2 = parseFloat(String($("customO2").value).replace(",", "."));
      label = "Custom atmosphere";
      if (!(p > 0) || !(o2 > 0 && o2 <= 100)) {
        $("missionResult").innerHTML = '<p class="muted-text">Enter a pressure in kPa and an oxygen percentage between 0 and 100.</p>';
        return;
      }
    } else {
      const r = data.references.find((x) => x.key === key);
      p = r.pressure_kpa; o2 = r.oxygen_pct; label = r.name;
    }
    if (!data.points.length) { $("missionResult").innerHTML = '<p class="muted-text">No tested points to compare with.</p>'; return; }

    const { best, close } = nearest(p, o2);
    const dO2 = best.oxygen_pct - o2;
    const ended = close.filter((c) => c.termination === "self_extinguished" || c.termination === "flow_terminated");
    const kept = ended.filter((c) => c.termination === "flow_terminated").length;
    const ignited = close.filter((c) => c.hot_ignition === "yes" || c.hot_ignition === "flash").length;
    const withIgn = close.filter((c) => c.hot_ignition != null).length;
    const pTxt = best.pressure_kpa >= p ? `${fmt(best.pressure_kpa / p, 2)}× higher pressure` : `${fmt(p / best.pressure_kpa, 2)}× lower pressure`;
    const q = `My cabin is at ${fmt(p)} kPa with ${fmt(o2)}% oxygen. What did similar tests show?`;

    $("missionResult").innerHTML = `
      <p class="muted-text">${esc(label)} · ${fmt(p)} kPa, ${fmt(o2)}% O₂</p>
      <div class="gap-figure">${close.length}</div>
      <p class="muted-text">tests within ±${TOL_O2} O₂ points and ×${TOL_P} pressure</p>
      ${close.length ? `<ul class="mission-stats">
          <li><span>Ignited</span><strong>${withIgn ? `${ignited} of ${withIgn}` : "n/a"}</strong></li>
          <li><span>Went out on their own</span><strong>${ended.length ? `${ended.length - kept} of ${ended.length}` : "n/a"}</strong></li>
          <li><span>Burned until fuel was cut</span><strong class="${kept ? "is-warn" : ""}">${ended.length ? `${kept} of ${ended.length}` : "n/a"}</strong></li>
        </ul>` : `<p class="nearest">No test is that close. These conditions are outside what the database covers.</p>`}
      <p class="nearest">Nearest test: <span class="exp-id">${esc(best.id)}</span> at ${fmt(best.pressure_kpa)} kPa, ${fmt(best.oxygen_pct)}% O₂
        (${dO2 >= 0 ? "+" : ""}${fmt(dO2)} O₂ points, ${pTxt})${outcomeText(best) ? `: ${esc(outcomeText(best))}` : ""}.</p>
      <button type="button" class="text-link mission-ask">Ask the assistant about this atmosphere →</button>`;
    $("missionResult").querySelector(".mission-ask").onclick = () => askAssistant(q);
  }

  // opens the database assistant (assistant.js) with the question already written
  function askAssistant(q) {
    const fab = document.querySelector(".psi-assist-fab");
    if (!fab) return;
    if (document.getElementById("psiAssistPanel")?.hidden) fab.click();
    const input = document.getElementById("psiAssistInput");
    if (input) { input.value = q; input.focus(); }
  }

  function setupMission() {
    const sel = $("missionSelect");
    sel.addEventListener("change", () => { $("customFields").hidden = sel.value !== "custom"; showMission(); });
    ["customP", "customO2"].forEach((id) => $(id).addEventListener("input", () => { if (sel.value === "custom") showMission(); }));
    showMission();
  }

  load();
})();
