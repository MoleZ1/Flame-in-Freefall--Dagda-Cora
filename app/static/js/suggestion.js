/* Suggestions (suggestion.html)
 * Talks to the chat API:
 *   POST /api/chat/interpret  {message}                    -> {scenario, summary, problems}
 *   POST /api/chat/answer     {scenario, confirmed: true}  -> {answer, results, abstained, recommendation_id, ...}
 *   POST /api/chat/feedback   {recommendation_id, experiment_id, grade}
 * Numbers shown in the results come from the database, never from the language model.
 */
(() => {
  const API = { interpret: "/api/chat/interpret", answer: "/api/chat/answer", feedback: "/api/chat/feedback" };

  const LABELS = {
    pressure: "Pressure", ambient_oxygen_mole_fraction: "Ambient O₂", burner_oxygen_mole_fraction: "Burner O₂",
    burner_fuel_mole_fraction: "Burner fuel fraction", ambient_fuel_mole_fraction: "Ambient fuel fraction",
    fuel_mole_fraction: "Fuel fraction", fuel_mass_fraction: "Fuel mass fraction",
    ambient_nitrogen_mole_fraction: "Ambient N₂", ambient_helium_mole_fraction: "Ambient He",
    oxidizer_ozone_mole_fraction: "Ozone in oxidizer", fuel_mass_flow_rate: "Fuel flow", oxygen_mass_flow_rate: "O₂ flow",
    initial_droplet_diameter: "Droplet diameter", strain_rate: "Strain rate", adiabatic_flame_temperature: "Flame temperature",
    gravity_environment: "Gravity", ozone_addition: "Ozone", cool_flame_observed: "Cool flame",
    hot_ignition: "Hot ignition", burn_time: "Burn time", flame_type: "Flame type", fuel: "Fuel",
  };
  const CATEGORY_LABELS = {
    microgravity_iss: "Microgravity (ISS)", normal_gravity_lab: "Ground lab", simulated_microgravity: "Simulated microgravity",
    with_ozone: "With ozone", without_ozone: "Without ozone",
  };
  const COMPONENT_LABELS = {
    s_phys: "Physical conditions", s_out: "Measures what you asked", s_mat: "Same or similar fuel",
    s_text: "Text match", s_meth: "Same setup",
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const human = (s) => String(s ?? "").replace(/_/g, " ");
  const label = (v) => LABELS[v] || human(v).replace(/^./, (c) => c.toUpperCase());
  const fmt = (n, d = 2) => Number(n).toLocaleString("en-US", { maximumFractionDigits: d });
  const score = (x) => Number(x).toFixed(2);

  // Database values for the details panel: 3 significant digits, readable units and categories.
  function pretty(value, unit) {
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (typeof value === "number") {
      const n = Math.abs(value) >= 1000 ? fmt(value, 0) : Number(value.toPrecision(3)).toString();
      const u = { degC: "°C", "1": "", "mm^2/s": "mm²/s", "m^2/s": "m²/s" }[unit] ?? (unit || "");
      return `${n} ${u}`.trim();
    }
    return CATEGORY_LABELS[value] || human(value);
  }

  let scenario = null;
  let lastQuestion = "";
  let recommendationId = null;

  /* ------------------------------------------------------------- http */
  async function post(url, body) {
    let r;
    try {
      r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) });
    } catch (_) {
      throw new Error("Could not reach the server. Check that Flask is running and try again.");
    }
    let data = {};
    try { data = await r.json(); } catch (_) { /* non-JSON error page */ }
    if (r.ok) return data;
    if (r.status === 404) throw new Error("The suggestion service is not connected yet. Register the /api/chat routes in Flask.");
    throw new Error(data.error || `The server returned error ${r.status}. Try again.`);
  }

  /* ---------------------------------------------------- unit display */
  // Values from the API are in canonical units (Pa, mole fraction, kg/s, m, K, s); show friendlier ones.
  function display(variable, value, unit) {
    if (value == null) return "not reported";
    if (unit === "Pa") return `${fmt(value / 1000, 1)} kPa`;
    if (unit === "1" && /fraction/.test(variable)) return `${fmt(value * 100, 1)}%`;
    if (unit === "kg/s") return `${fmt(value * 1e6, 2)} mg/s`;
    if (unit === "m") return `${fmt(value * 1000, 2)} mm`;
    return `${fmt(value, 3)} ${unit && unit !== "1" ? unit : ""}`.trim();
  }

  /* ----------------------------------------------- step 1: interpret */
  $("askForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("question").value.trim();
    $("askMessage").textContent = "";
    if (q.length < 10) { $("askMessage").textContent = "Describe your scenario in at least a few words."; return; }
    const btn = $("askBtn");
    btn.disabled = true; btn.textContent = "Interpreting…";
    try {
      const d = await post(API.interpret, { message: q });
      lastQuestion = q;
      scenario = d.scenario;
      renderScenario(d.problems || []);
      $("resultsView").hidden = true;
      $("confirmPanel").hidden = false;
      $("confirmPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      $("askMessage").textContent = err.message;
    } finally {
      btn.disabled = false; btn.textContent = "Interpret";
    }
  });

  /* ------------------------------------------------- step 2: confirm */
  function scenarioItems() {
    const s = scenario.structured;
    const items = [];
    if (s.fuel) items.push({ key: "fuel", text: `Fuel: ${human(s.fuel)}`, remove: () => { s.fuel = null; } });
    if (s.configuration && s.configuration.flame_type) {
      items.push({ key: "flame_type", text: `Flame type: ${s.configuration.flame_type}`, remove: () => { delete s.configuration.flame_type; } });
    }
    s.conditions.forEach((c) => {
      const raw = c.raw || {};
      let value;
      if (c.kind === "categorical") value = CATEGORY_LABELS[c.norm.value] || human(c.norm.value);
      else if (c.kind === "range") value = `${raw.min}–${raw.max} ${raw.unit || ""}`;
      else value = `${raw.value} ${raw.unit || ""}`;
      items.push({ key: c.variable, text: `${label(c.variable)}: ${value.trim()}`,
                   remove: () => { s.conditions = s.conditions.filter((x) => x !== c); } });
    });
    s.outputs_of_interest.forEach((o) => {
      items.push({ key: o, text: `Looking for: ${label(o).toLowerCase()}`,
                   remove: () => { s.outputs_of_interest = s.outputs_of_interest.filter((x) => x !== o); } });
    });
    (s.hard_filters || []).forEach((f) => {
      items.push({ key: f.variable, text: `Only ${label(f.variable).toLowerCase()}: ${f.values.map((v) => CATEGORY_LABELS[v] || human(v)).join(", ")}`,
                   remove: () => { s.hard_filters = s.hard_filters.filter((x) => x !== f); } });
    });
    return items;
  }

  function renderScenario(problems) {
    const items = scenarioItems();
    const ul = $("scenarioChips");
    ul.innerHTML = "";
    if (!items.length) {
      ul.innerHTML = '<li class="muted-text">No conditions were recognized. Edit your question and add pressure, oxygen, fuel or gravity.</li>';
    }
    items.forEach((it) => {
      const li = document.createElement("li");
      li.className = "chip";
      li.innerHTML = `${esc(it.text)}<button type="button" aria-label="Remove ${esc(it.text)}">×</button>`;
      li.querySelector("button").onclick = () => { it.remove(); renderScenario([]); };
      ul.appendChild(li);
    });
    const notes = problems.concat((scenario.unmapped || []).map((u) => `Not recognized: “${u}”`));
    $("scenarioProblems").hidden = !notes.length;
    $("scenarioProblems").textContent = notes.join(" ");
    $("searchBtn").disabled = !items.length;
  }

  $("editBtn").addEventListener("click", () => {
    $("confirmPanel").hidden = true;
    $("question").value = lastQuestion;
    $("question").focus();
  });

  /* -------------------------------------------------- step 3: search */
  $("searchBtn").addEventListener("click", async () => {
    const btn = $("searchBtn");
    $("searchMessage").textContent = "";
    btn.disabled = true; btn.textContent = "Searching…";
    try {
      const d = await post(API.answer, { scenario, confirmed: true });
      recommendationId = d.recommendation_id;
      renderAnswer(d);
      renderResults(d);
      $("resultsView").hidden = false;
      $("resultsView").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      $("searchMessage").textContent = err.message;
    } finally {
      btn.disabled = false; btn.textContent = "Search experiments";
    }
  });

  function citationChips(ids, rankOf) {
    return ids.map((id) => {
      if (rankOf[id]) return `<a class="cite" href="#res-${rankOf[id]}" title="${esc(id)}">#${rankOf[id]}</a>`;
      const kind = id.startsWith("FND:") ? "Finding" : id.startsWith("CHK:") ? "Report" : "Source";
      return `<span class="cite" title="${esc(id)}">${kind}</span>`;
    }).join(" ");
  }

  function renderAnswer(d) {
    const a = d.answer || {};
    const rankOf = {};
    (d.results || []).forEach((r) => { rankOf[r.experiment_id] = r.rank; });
    const claims = (list) => (list || [])
      .filter((c) => c.supported !== false && c.citations && c.citations.length)
      .map((c) => `<li>${esc(c.statement)} ${citationChips(c.citations, rankOf)}</li>`).join("");
    const evidence = claims(a.evidence);
    const interpretation = claims(a.interpretation);
    const dropped = (a.evidence || []).filter((c) => c.supported === false).length;
    const limits = (a.limitations || []).map((l) => `<li>${esc(l)}</li>`).join("");

    $("answerPanel").classList.toggle("is-abstained", !!d.abstained);
    $("answerBody").innerHTML = `
      <p class="answer-summary">${esc(a.summary || "")}</p>
      ${evidence ? `<h3>Evidence</h3><ul class="claim-list">${evidence}</ul>` : ""}
      ${interpretation ? `<h3>What it suggests</h3><ul class="claim-list">${interpretation}</ul>` : ""}
      ${limits || dropped ? `<details class="limits"><summary>Limitations</summary><ul>${limits}${
        dropped ? `<li>${dropped} ${dropped === 1 ? "statement was" : "statements were"} removed because ${dropped === 1 ? "it was" : "they were"} not backed by the cited sources.</li>` : ""}</ul></details>` : ""}`;
  }

  /* ----------------------------------------------------------- results */
  function tags(r) {
    const out = [];
    if (r.data_origin === "simulation") out.push("Simulation");
    if (r.flags.some((f) => f.startsWith("extrapolation:"))) out.push("Outside tested range");
    if (r.flags.some((f) => f.startsWith("different:gravity"))) out.push("Different gravity");
    const missing = r.flags.filter((f) => f.startsWith("cannot_verify:")).map((f) => label(f.split(":")[1]).toLowerCase());
    if (missing.length) out.push(`No ${missing.join(", ")}`);
    if (r.flags.some((f) => f.startsWith("data_quality:"))) out.push("Data under review");
    return out.slice(0, 3);
  }

  function matchLines(r) {
    return (r.variable_scores || []).filter((v) => v.status !== "not_applicable").map((v) => {
      if (v.status !== "reported") return `<li><span>${esc(label(v.variable))}</span><span class="muted-text">not reported</span></li>`;
      if (v.experiment_value == null) return `<li><span>${esc(label(v.variable))}</span><span>${v.s_v === 1 ? "matches" : "differs"}</span></li>`;
      return `<li><span>${esc(label(v.variable))}</span><span>${esc(display(v.variable, v.experiment_value, v.unit))}
        <span class="muted-text">(you: ${esc(display(v.variable, v.scenario_value, v.unit))})</span></span></li>`;
    }).join("");
  }

  function details(r) {
    const comps = (r.components || []).map((c) => `
      <li><span>${esc(COMPONENT_LABELS[c.name] || c.name)}</span>
        ${c.observed ? `<span class="mini-bar" aria-hidden="true"><span style="width:${Math.round((c.score || 0) * 100)}%"></span></span>
          <span class="num">${score(c.score || 0)}</span>` : '<span class="muted-text">not available</span>'}</li>`).join("");
    const list = (items) => items.map((i) => `<li><span>${esc(i.label)}</span><span>${i.approximate ? "~" : ""}${esc(pretty(i.value, i.unit))}</span></li>`).join("");
    const pct = (x) => (x == null ? "n/a" : `${Math.round(x * 100)}%`);
    return `
      <div class="detail-grid">
        <div>
          <h4>Why it ranks here</h4>
          <ul class="kv comp-list">${comps}</ul>
          <ul class="kv">
            <li><span>Inside the tested range</span><span>${pct(r.T)}</span></li>
            <li><span>Documentation quality</span><span>${pct(r.Q)}</span></li>
            <li><span>Score uncertainty</span><span>${pct(r.U)}</span></li>
          </ul>
        </div>
        <div>
          <h4>Recorded conditions</h4>
          <ul class="kv">${list(r.conditions || [])}</ul>
          ${(r.outcomes || []).length ? `<h4>Outcomes</h4><ul class="kv">${list(r.outcomes)}</ul>` : ""}
          ${(r.measurements || []).length ? `<h4>Measurements</h4><ul class="kv">${list(r.measurements)}</ul>` : ""}
        </div>
      </div>
      <div class="rate" role="group" aria-label="Rate the relevance of ${esc(r.experiment_id)}">
        <span class="muted-text">How relevant is this experiment?</span>
        ${[0, 1, 2, 3].map((g) => `<button type="button" class="rate-btn" data-grade="${g}" title="${["Not relevant", "Slightly", "Relevant", "Very relevant"][g]}">${g}</button>`).join("")}
      </div>`;
  }

  function renderResults(d) {
    const results = d.results || [];
    $("resultsTitle").textContent = d.abstained ? "Nearest records (not comparable)" : "Closest experiments";
    $("resultList").innerHTML = results.length ? results.map((r) => `
      <li class="result" id="res-${r.rank}">
        <div class="result-head">
          <span class="rank">${r.rank}</span>
          <div class="result-main">
            <p class="result-id">${esc(r.experiment_id)}</p>
            <p class="muted-text">${esc(human(r.fuel || "fuel not reported"))}, ${r.data_origin === "simulation" ? "simulated" : "measured"}${
              tags(r).map((t) => ` <span class="tag">${esc(t)}</span>`).join("")}</p>
          </div>
          <div class="relevance" title="Relevance ${score(r.R)} of 1">
            <span class="relevance-bar" aria-hidden="true"><span style="width:${Math.round(r.R * 100)}%"></span></span>
            <span class="num">${score(r.R)}</span>
          </div>
        </div>
        <ul class="kv match-list">${matchLines(r)}</ul>
        <details class="result-more"><summary>Details</summary>${details(r)}</details>
      </li>`).join("") : '<li class="muted-text">No experiments matched these conditions.</li>';

    document.querySelectorAll(".rate").forEach((group) => {
      const expId = group.closest(".result").querySelector(".result-id").textContent;
      group.querySelectorAll(".rate-btn").forEach((b) => b.addEventListener("click", async () => {
        try {
          await post(API.feedback, { recommendation_id: recommendationId, experiment_id: expId, grade: Number(b.dataset.grade) });
          group.innerHTML = `<span class="muted-text">Thanks. Rated ${b.dataset.grade} of 3.</span>`;
        } catch (err) {
          group.insertAdjacentHTML("beforeend", `<span class="warn-text">${esc(err.message)}</span>`);
        }
      }));
    });

    const provisional = results.some((r) => r.flags.includes("provisional_sigma"));
    $("resultsFoot").textContent =
      `${d.candidates_considered || 0} candidates considered.` +
      (provisional ? " Similarity tolerances are provisional until a domain expert sets them." : "");
  }

  /* --------------------------------------------- prefill from dashboard */
  const params = new URLSearchParams(location.search);
  const p = params.get("pressure_kpa"), o2 = params.get("o2_pct");
  if (p && o2) {
    $("question").value = `What happens to a fire in a habitat atmosphere at ${p} kPa with ${o2}% oxygen?`;
  }
})();
