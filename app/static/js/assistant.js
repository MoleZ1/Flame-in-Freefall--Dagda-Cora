/* Database assistant: floating chat button available on every page.
 * Include on a page with:
 *   <link rel="stylesheet" href="{{ url_for('static', filename='css/assistant.css') }}">
 *   <script src="{{ url_for('static', filename='js/assistant.js') }}" defer></script>
 * Talks to POST /api/assistant. Answers come only from the database and are checked before they are shown.
 */
(() => {
  const STORE = "psiAssistant.v1";
  const page = location.pathname.replace(/^\/+|\/+$/g, "") || "home";
  const investigationId = () => new URLSearchParams(location.search).get("investigation");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const STARTERS = {
    home: ["My cabin is at 70 kPa with 30% oxygen. Is that a fire risk?", "What happens near 34% oxygen and 56.5 kPa?", "Which investigations are in the database?"],
    suggestion: ["How does this investigation compare with the database?", "Which dataset should I compare it with?", "Were my conditions tested before?"],
    insertion: ["Which variables does the database recognize?", "What kind of files can I upload?", "Which investigations study droplets?"],
  };

  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(STORE) || "[]"); } catch (_) { history = []; }
  const save = () => { try { sessionStorage.setItem(STORE, JSON.stringify(history.slice(-20))); } catch (_) { /* storage off */ } };

  /* ------------------------------------------------------------- DOM */
  const root = document.createElement("div");
  root.className = "psi-assist";
  root.innerHTML = `
    <button type="button" class="psi-assist-fab" aria-expanded="false" aria-controls="psiAssistPanel" title="Ask the database">
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.2 3.6c-.5.4-1.3.1-1.3-.6V16A2.5 2.5 0 0 1 4 13.5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="8.5" cy="9.5" r="1.1" fill="currentColor"/><circle cx="12" cy="9.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="9.5" r="1.1" fill="currentColor"/></svg>
      <span class="psi-assist-sr">Ask the database assistant</span>
    </button>
    <section class="psi-assist-panel" id="psiAssistPanel" role="dialog" aria-modal="false" aria-labelledby="psiAssistTitle" hidden>
      <header class="psi-assist-head">
        <div>
          <h2 id="psiAssistTitle">Database assistant</h2>
          <p>Answers only from the PSI database, checked before they are shown.</p>
        </div>
        <div class="psi-assist-actions">
          <button type="button" class="psi-assist-icon" data-act="clear" title="New conversation" aria-label="New conversation">↺</button>
          <button type="button" class="psi-assist-icon" data-act="close" title="Close" aria-label="Close">×</button>
        </div>
      </header>
      <div class="psi-assist-log" aria-live="polite"></div>
      <form class="psi-assist-form">
        <label class="psi-assist-sr" for="psiAssistInput">Your question</label>
        <textarea id="psiAssistInput" rows="2" maxlength="1500" placeholder="Ask about experiments, conditions, findings…"></textarea>
        <button type="submit" class="psi-assist-send">Ask</button>
      </form>
    </section>`;
  document.body.appendChild(root);

  const fab = root.querySelector(".psi-assist-fab");
  const panel = root.querySelector(".psi-assist-panel");
  const log = root.querySelector(".psi-assist-log");
  const form = root.querySelector(".psi-assist-form");
  const input = root.querySelector("textarea");
  const send = root.querySelector(".psi-assist-send");

  function open(on) {
    panel.hidden = !on;
    fab.setAttribute("aria-expanded", String(on));
    root.classList.toggle("is-open", on);
    if (on) { render(); input.focus(); }
  }
  fab.addEventListener("click", () => open(panel.hidden));
  root.querySelector('[data-act="close"]').addEventListener("click", () => { open(false); fab.focus(); });
  root.querySelector('[data-act="clear"]').addEventListener("click", () => { history = []; save(); render(); input.focus(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) { open(false); fab.focus(); } });

  /* ---------------------------------------------------------- render */
  // Long IDs (report passages, datasets) are shown short; the full ID stays in the tooltip.
  function short(id) {
    let m;
    if ((m = id.match(/^CHK:(PSI-\d+):.*:0*(\d+)$/))) return `Report ${m[1]} §${m[2]}`;
    if ((m = id.match(/^FND:(PSI-\d+):0*(\d+)$/))) return `Finding ${m[1]} #${m[2]}`;
    if ((m = id.match(/^DS:(PSI-\d+):(.+)$/))) return `Dataset ${m[1]} ${m[2].replace(/_/g, " ").slice(0, 28)}`;
    if ((m = id.match(/^DOC:(PSI-\d+):/))) return `Document ${m[1]}`;
    return id;
  }
  const chip = (id) => `<span class="psi-cite" title="${esc(id)}">${esc(short(id))}</span>`;

  function withCites(text) {
    // escape first, then turn [ID] into small chips
    return esc(text).replace(/\[([A-Za-z0-9_.:@+\-]+)\]/g, (_, id) => chip(id))
      .split(/\n{2,}/).map((p) => `<p>${p}</p>`).join("");
  }

  function bubble(m) {
    if (m.role === "user") return `<div class="psi-msg psi-msg--user">${esc(m.content)}</div>`;
    const d = m.data || {};
    const recs = (d.recommendations || []).map((r) =>
      `<li><strong>${esc(r.title)}</strong> ${esc(r.detail)} ${(r.citations || []).map(chip).join(" ")}</li>`).join("");
    const srcs = (d.sources || []).map((s) =>
      `<li class="${s.cited ? "is-cited" : ""}" title="${esc(s.excerpt)}">${chip(s.id)}
         <span class="psi-src-meta">${esc(s.type)}${s.investigation ? ` · ${esc(s.investigation)}` : ""}</span></li>`).join("");
    const al = d.alert;
    const alertBox = al ? `<div class="psi-alert psi-alert--${esc(al.level)}" role="${al.level === "high" ? "alert" : "status"}">
        <strong>${esc(al.title)}</strong><p>${esc(al.text)}</p>
        <p class="psi-rule">Data signal from the tests in the database, not a safety rating. ${esc(al.rule)}
          Always follow your mission's fire-safety procedures.</p></div>` : "";
    const sc = d.scenario;
    let scBox = "";
    if (sc) {
      const t = sc.close_tests || {};
      const pct = (x) => (x == null ? "n/a" : `${Math.round(x * 100)}%`);
      const cond = (sc.conditions || []).map((c) => `<li><span>${esc(c.label)}: <strong>${esc(c.yours_shown)}</strong></span>
          <span class="${c.inside_tested_range === false ? "psi-out" : ""}">${c.tested_shown ? `tested ${esc(c.tested_shown)}` : "never measured"}</span></li>`).join("");
      const trend = (sc.oxygen_trend || []).map((b) => `<li><span>${esc(b.oxygen_band)} O₂</span>
          <span class="psi-bar"><span style="width:${Math.round((b.kept_burning_rate || 0) * 100)}%"></span></span>
          <span>${pct(b.kept_burning_rate)} kept burning · ${pct(b.ignition_rate)} ignited · ${b.tests}</span></li>`).join("");
      scBox = `<details class="psi-scen" open><summary>What the close tests showed${sc.nearest_only ? " (nearest only)" : ""}</summary>
        <ul class="psi-kv">${cond}</ul>
        ${Object.keys(sc.close_tests_range || {}).length ? `<p class="psi-sub">Close tests span ${esc(Object.entries(sc.close_tests_range).map(([k, v]) => `${k} ${v}`).join("; "))}</p>` : ""}
        <p>${t.tests || 0} close tests · ${t.ignited ?? 0} ignited (${pct(t.ignition_rate)}) · ${t.self_extinguished ?? 0} went out on their own ·
           ${t.burned_until_fuel_cut ?? 0} burned until the fuel was cut${t.burn_time_s ? ` · burn time ${t.burn_time_s.min}–${t.burn_time_s.max} s` : ""}</p>
        ${trend ? `<p class="psi-sub">By oxygen level, at similar other conditions: flames that kept burning until the fuel was cut</p><ul class="psi-trend">${trend}</ul>` : ""}
        <p class="psi-sub">Setup: ${esc(Object.keys(sc.setups || {}).join("; ").replace(/_/g, " ") || "n/a")} · Fuel: ${esc(Object.keys(sc.fuels || {}).join(", "))}</p>
      </details>`;
    }
    const notes = [];
    if (d.removed_statements) notes.push(`${d.removed_statements} ${d.removed_statements === 1 ? "statement was" : "statements were"} removed because the database did not back ${d.removed_statements === 1 ? "it" : "them"}.`);
    if (d.source !== "qwen") notes.push("Written without the language model.");
    return `<div class="psi-msg psi-msg--bot">
      ${alertBox}
      ${withCites(m.content)}
      ${scBox}
      ${recs ? `<h3>Recommendations</h3><ol class="psi-recs">${recs}</ol>` : ""}
      ${srcs ? `<details class="psi-srcs"><summary>Sources (${d.sources.length})</summary><ul>${srcs}</ul></details>` : ""}
      ${notes.length ? `<p class="psi-note">${esc(notes.join(" "))}</p>` : ""}
    </div>`;
  }

  function render(pending) {
    if (!history.length && !pending) {
      const starters = STARTERS[page] || STARTERS.home;
      log.innerHTML = `<div class="psi-msg psi-msg--bot"><p>Ask about the experiments, conditions and findings in the database,
        or ask for a recommendation. I only answer from the database and cite every record I use.</p></div>
        <div class="psi-starters">${starters.map((s) => `<button type="button">${esc(s)}</button>`).join("")}</div>`;
      log.querySelectorAll(".psi-starters button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
      return;
    }
    log.innerHTML = history.map(bubble).join("") +
      (pending ? `<div class="psi-msg psi-msg--user">${esc(pending)}</div><div class="psi-msg psi-msg--bot psi-typing">Searching the database…</div>` : "");
    log.scrollTop = log.scrollHeight;
  }

  /* ------------------------------------------------------------- ask */
  async function ask(text) {
    const q = (text || "").trim();
    if (!q || send.disabled) return;
    input.value = "";
    send.disabled = true;
    render(q);
    let data;
    try {
      const r = await fetch("/api/assistant", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ message: q, context: { page, investigation_id: investigationId() },
                               history: history.map((m) => ({ role: m.role, content: m.content })).slice(-6) }),
      });
      try { data = await r.json(); } catch (_) { data = {}; }
      if (!r.ok) throw new Error(data.error || (r.status === 404 ? "The assistant is not connected yet (register /api/assistant in Flask)." : `Server error ${r.status}.`));
      history.push({ role: "user", content: q }, { role: "assistant", content: data.answer || "", data });
    } catch (err) {
      history.push({ role: "user", content: q },
                   { role: "assistant", content: err.message.includes("fetch") ? "Could not reach the server." : err.message, data: { source: "error" } });
    } finally {
      save();
      send.disabled = false;
      render();
      input.focus();
    }
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); } });
})();
