(() => {
  const ENDPOINT = document.getElementById("investigationForm").getAttribute("action");
  const MAX_FILE_MB = 50;
  const MAX_KEYWORDS = 15;
  const ALLOWED = ["csv", "xlsx", "xls", "pdf", "txt", "zip", "json"];
  const ROLES = [
    ["experimental_data", "Experimental data"],
    ["simulation_data", "Simulation data"],
    ["report", "Report or paper"],
    ["data_dictionary", "Data dictionary"],
    ["metadata", "Metadata (ISA, etc.)"],
    ["other", "Other"],
  ];
  const DEFAULT_ROLE = { csv: "experimental_data", xlsx: "experimental_data", xls: "experimental_data",
                         pdf: "report", zip: "metadata", txt: "other", json: "other" };

  const $ = (id) => document.getElementById(id);
  const form = $("investigationForm");
  const files = [];      // {file, role, id}
  const keywords = [];
  let nextId = 1;

  const ext = (name) => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");
  const fmtSize = (b) => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ---------------- keywords ---------------- */
  const kwInput = $("keywordInput");
  const chipBox = $("chipInput");
  chipBox.addEventListener("click", (e) => { if (e.target === chipBox) kwInput.focus(); });

  function addKeyword(raw) {
    const k = raw.trim().replace(/\s+/g, " ");
    if (!k) return;
    if (keywords.length >= MAX_KEYWORDS) { kwInput.value = ""; return; }
    if (keywords.some((x) => x.toLowerCase() === k.toLowerCase())) { kwInput.value = ""; return; }
    keywords.push(k);
    kwInput.value = "";
    renderKeywords();
    setValid("keywords", true);
  }
  function renderKeywords() {
    chipBox.querySelectorAll(".chip").forEach((c) => c.remove());
    keywords.forEach((k, i) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.innerHTML = `${esc(k)}<button type="button" aria-label="Remove ${esc(k)}">×</button>`;
      chip.querySelector("button").onclick = () => { keywords.splice(i, 1); renderKeywords(); kwInput.focus(); };
      chipBox.insertBefore(chip, kwInput);
    });
    kwInput.placeholder = keywords.length ? "" : "cool flames, n-butane, microgravity";
  }
  kwInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addKeyword(kwInput.value); }
    else if (e.key === "Backspace" && !kwInput.value && keywords.length) { keywords.pop(); renderKeywords(); }
  });
  kwInput.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (text.includes(",")) { e.preventDefault(); text.split(",").forEach(addKeyword); }
  });
  kwInput.addEventListener("blur", () => addKeyword(kwInput.value));

  /* ---------------- description ---------------- */
  const desc = $("description");
  desc.addEventListener("input", () => { $("descCounter").textContent = `${desc.value.length} / 4000`; });

  /* ---------------- files ---------------- */
  const chamber = $("chamber");
  const fileInput = $("fileInput");

  function addFiles(list) {
    const errors = [];
    for (const f of list) {
      const e = ext(f.name);
      if (!ALLOWED.includes(e)) { errors.push(`${f.name}: format not supported. Use CSV, Excel, PDF, TXT, ZIP or JSON.`); continue; }
      if (f.size > MAX_FILE_MB * 1048576) { errors.push(`${f.name} is ${fmtSize(f.size)}; the limit is ${MAX_FILE_MB} MB.`); continue; }
      if (f.size === 0) { errors.push(`${f.name} is empty.`); continue; }
      if (files.some((x) => x.file.name === f.name && x.file.size === f.size)) { errors.push(`${f.name} is already in the list.`); continue; }
      files.push({ file: f, role: DEFAULT_ROLE[e] || "other", id: nextId++ });
    }
    $("fileErrors").textContent = errors.join(" ");
    renderFiles();
  }

  function renderFiles() {
    const ul = $("fileList");
    ul.innerHTML = "";
    files.forEach((item) => {
      const e = ext(item.file.name);
      const kind = ["csv", "xlsx", "xls"].includes(e) ? "data" : e === "pdf" ? "doc" : "";
      const li = document.createElement("li");
      li.className = "file";
      li.innerHTML = `
        <span class="ext ${kind}" aria-hidden="true">${esc(e.toUpperCase())}</span>
        <div style="min-width:0">
          <div class="file-name" title="${esc(item.file.name)}">${esc(item.file.name)}</div>
          <div class="file-meta">${fmtSize(item.file.size)}${e === "csv" ? ' <button type="button" class="preview-btn" aria-expanded="false">Preview first rows</button>' : ""}</div>
        </div>
        <select aria-label="Document type for ${esc(item.file.name)}">
          ${ROLES.map(([v, l]) => `<option value="${v}"${v === item.role ? " selected" : ""}>${l}</option>`).join("")}
        </select>
        <button type="button" class="remove" aria-label="Remove ${esc(item.file.name)}">×</button>`;
      li.querySelector("select").onchange = (ev) => { item.role = ev.target.value; };
      li.querySelector(".remove").onclick = () => {
        files.splice(files.indexOf(item), 1);
        renderFiles();
      };
      const pb = li.querySelector(".preview-btn");
      if (pb) pb.onclick = () => togglePreview(li, item, pb);
      ul.appendChild(li);
    });
    chamber.classList.toggle("has-files", files.length > 0);
    $("chamberTitle").textContent = files.length ? "Add more files" : "Drop your files here";
    updateSummary();
  }

  function parseCsvLines(text, maxRows) {
    const rows = [];
    let row = [], cell = "", quoted = false;
    for (let i = 0; i < text.length && rows.length < maxRows; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; }
        else if (c === '"') quoted = false;
        else cell += c;
      } else if (c === '"') quoted = true;
      else if (c === ",") { row.push(cell); cell = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); cell = "";
        if (row.some((x) => x.trim() !== "")) rows.push(row);
        row = [];
      } else cell += c;
    }
    if (rows.length < maxRows && (cell || row.length)) { row.push(cell); rows.push(row); }
    return rows;
  }

  async function togglePreview(li, item, btn) {
    const open = li.querySelector(".preview");
    if (open) { open.remove(); btn.textContent = "Preview first rows"; btn.setAttribute("aria-expanded", "false"); return; }
    const text = await item.file.slice(0, 64 * 1024).text();
    const rows = parseCsvLines(text.replace(/^﻿/, ""), 6);
    const div = document.createElement("div");
    div.className = "preview";
    if (!rows.length) {
      div.innerHTML = '<p class="file-meta" style="padding:.5rem .6rem;margin:0">No rows found.</p>';
    } else {
      const [head, ...body] = rows;
      div.innerHTML = `<table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
        <tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }
    li.appendChild(div);
    btn.textContent = "Hide preview";
    btn.setAttribute("aria-expanded", "true");
  }

  fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
  let dragDepth = 0;
  ["dragenter", "dragover"].forEach((t) => chamber.addEventListener(t, (e) => { e.preventDefault(); if (t === "dragenter") dragDepth++; chamber.classList.add("is-over"); }));
  chamber.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; chamber.classList.remove("is-over"); } });
  chamber.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; chamber.classList.remove("is-over"); addFiles(e.dataTransfer.files); });
  // keep the browser from opening a file dropped outside the zone
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  function updateSummary() {
    if (!files.length) { $("summary").textContent = "No documents"; return; }
    const total = files.reduce((s, x) => s + x.file.size, 0);
    $("summary").innerHTML = `<strong>${files.length} ${files.length === 1 ? "document" : "documents"}</strong>, ${fmtSize(total)} total`;
  }

  /* ---------------- validation ---------------- */
  function setValid(name, ok) {
    const f = form.querySelector(`[data-field="${name}"]`);
    if (f) f.classList.toggle("invalid", !ok);
    return ok;
  }
  function validate() {
    const id = $("investigation_id").value.trim();
    const doi = $("doi").value.trim();
    const checks = [
      setValid("title", $("title").value.trim().length >= 5),
      setValid("investigation_id", !id || /^PSI-\d+$/i.test(id)),
      setValid("description", desc.value.trim().length >= 30),
      setValid("keywords", keywords.length > 0),
      setValid("doi", !doi || /^10\.\d{4,9}\/\S+$/.test(doi) || /^https?:\/\/\S+$/.test(doi)),
    ];
    const filesOk = files.length > 0;
    if (!filesOk) $("fileErrors").textContent = "Add at least one document.";
    const ok = checks.every(Boolean) && filesOk;
    if (!ok) {
      const first = form.querySelector(".invalid input, .invalid textarea");
      if (first) first.focus(); else if (!filesOk) chamber.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return ok;
  }
  ["title", "investigation_id", "description", "doi"].forEach((n) => {
    $(n).addEventListener("input", () => form.querySelector(`[data-field="${n}"]`).classList.remove("invalid"));
  });

  /* ---------------- submit ---------------- */
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    addKeyword(kwInput.value);
    $("formMessage").textContent = "";
    if (!validate()) { $("formMessage").textContent = "Fix the highlighted fields before submitting."; return; }

    const data = new FormData();
    data.append("title", $("title").value.trim());
    data.append("investigation_id", $("investigation_id").value.trim().toUpperCase());
    data.append("description", desc.value.trim());
    data.append("keywords", JSON.stringify(keywords));
    data.append("gravity_environment", $("gravity_environment").value);
    data.append("doi", $("doi").value.trim());
    data.append("file_roles", JSON.stringify(files.map((x) => x.role)));
    files.forEach((x) => data.append("files", x.file, x.file.name));

    const xhr = new XMLHttpRequest();
    const bar = $("submitBar");
    const btn = $("submitBtn");
    xhr.open("POST", ENDPOINT);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) $("progressFill").style.width = `${Math.round((ev.loaded / ev.total) * 100)}%`;
    };
    xhr.onload = () => {
      bar.classList.remove("uploading");
      btn.disabled = false; btn.textContent = "Submit investigation";
      let body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* non-JSON response */ }
      if (xhr.status >= 200 && xhr.status < 300) return showDone(body);
      const messages = {
        404: `The server has no ${ENDPOINT} route yet. Add it in Flask.`,
        405: `The ${ENDPOINT} route does not accept POST. Check its methods in Flask.`,
        413: "The files exceed the server's upload limit (MAX_CONTENT_LENGTH in Flask).",
      };
      $("formMessage").textContent = body.error || messages[xhr.status] || `The server returned error ${xhr.status}. Try again.`;
    };
    xhr.onerror = () => {
      bar.classList.remove("uploading");
      btn.disabled = false; btn.textContent = "Submit investigation";
      $("formMessage").textContent = "Could not reach the server. Check that Flask is running and try again.";
    };
    $("progressFill").style.width = "0%";
    bar.classList.add("uploading");
    btn.disabled = true; btn.textContent = "Submitting…";
    xhr.send(data);
  });

  function showDone(body) {
    const rows = [
      ["Title", $("title").value.trim()],
      ["ID", body.investigation_id || $("investigation_id").value.trim() || "Assigned during review"],
      ["Documents", `${files.length}`],
      ["Status", body.status === "needs_review" || !body.status ? "Pending review" : body.status],
    ];
    $("doneDetails").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("formView").hidden = true;
    $("doneView").hidden = false;
    $("doneView").focus();
  }

  $("againBtn").onclick = () => {
    form.reset();
    files.length = 0; keywords.length = 0;
    renderFiles(); renderKeywords();
    $("descCounter").textContent = "0 / 4000";
    $("fileErrors").textContent = ""; $("formMessage").textContent = "";
    form.querySelectorAll(".invalid").forEach((f) => f.classList.remove("invalid"));
    $("doneView").hidden = true;
    $("formView").hidden = false;
    $("title").focus();
  };
})();
