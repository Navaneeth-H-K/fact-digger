/* Fact Knowledge Layer — single-page inspector. Plain JS, talks to the JSON API. */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* keep statusText */ }
      throw new Error(`${response.status}: ${detail}`);
    }
    return response.status === 204 ? null : response.json();
  }

  const fmtNum = (value) => {
    if (value === null || value === undefined) return "";
    const abs = Math.abs(value);
    if (abs >= 1e9) return `${(value / 1e9).toFixed(2)} bn`;
    if (abs >= 1e6) return `${(value / 1e6).toFixed(2)} mn`;
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  };

  class PausedError extends Error {}

  const printedValue = (fact) => [fact.value_raw, fact.unit_raw, fact.scale_raw].filter(Boolean).join(" ");

  // ---------------------------------------------------------------- tabs
  function showTab(name) {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `panel-${name}`));
    const loaders = { documents: loadDocuments, facts: loadFacts, relations: loadRelations, failures: loadFailures, schema: loadSchema };
    if (loaders[name]) loaders[name]();
  }
  $("#tabs").addEventListener("click", (event) => {
    if (event.target.dataset.tab) showTab(event.target.dataset.tab);
  });

  // ---------------------------------------------------------------- health
  api("/health")
    .then((h) => { $("#health").textContent = `db ${h.database} · llm ${h.llm_mode} · storage ${h.storage}`; })
    .catch((e) => { $("#health").textContent = `health: ${e.message}`; });

  // ---------------------------------------------------------------- upload + processing loop
  async function sha256Hex(file) {
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function jobCard(file) {
    const card = document.createElement("div");
    card.className = "job";
    card.innerHTML = `<div class="job-title"><span>${esc(file.name)}</span><span class="job-pct">0%</span></div>
      <div class="job-status">preparing…</div><div class="bar"><span></span></div>`;
    $("#upload-jobs").prepend(card);
    return {
      set(status, pct, error = false) {
        $(".job-status", card).textContent = status;
        $(".job-pct", card).textContent = `${Math.round(pct)}%`;
        $(".bar > span", card).style.width = `${pct}%`;
        card.classList.toggle("error", error);
      },
    };
  }

  async function sendBytes(ticket, file) {
    const target = ticket.upload;
    if (target.method === "PUT") {
      const headers = { "Content-Type": "application/pdf" };
      if (target.token) headers.Authorization = `Bearer ${target.token}`;
      const response = await fetch(target.url, { method: "PUT", headers, body: file });
      if (!response.ok) throw new Error(`storage upload failed: ${response.status}`);
      return;
    }
    const form = new FormData();
    form.append("file", file, file.name);
    await api(target.url, { method: "POST", body: form });
  }

  async function processLoop(documentId, job) {
    for (;;) {
      const progress = await api(`/documents/${documentId}/process`, { method: "POST" });
      const total = Math.max(progress.total_pages, 1);
      const finished = progress.done + progress.failed + progress.skipped;
      job.set(
        `extracting: ${progress.done} done, ${progress.failed} failed, ${progress.skipped} skipped, ${progress.pending} pending` +
          (progress.last_errors.length ? ` · last error: ${progress.last_errors[0].slice(0, 80)}` : ""),
        20 + (70 * finished) / total,
      );
      if (progress.paused_reason) {
        job.set(`paused: ${progress.paused_reason}. Use Process on the Documents tab to resume later.`, 20 + (70 * finished) / total, true);
        throw new PausedError(progress.paused_reason);
      }
      if (progress.pending === 0) return progress;
      if (progress.processed_this_call === 0 && progress.estimated_calls_remaining === 0) return progress;
    }
  }

  async function linkLoop(job) {
    for (let i = 0; i < 50; i += 1) {
      const progress = await api("/link", { method: "POST" });
      if (job) job.set(`linking: ${progress.final} final, ${progress.pending_llm} awaiting adjudication`, 95);
      if (progress.paused_reason) {
        if (job) job.set(`linking paused: ${progress.paused_reason}. Run linking again later from the Documents tab.`, 95, true);
        return progress;
      }
      if (progress.pending_llm === 0 || progress.adjudicated_this_call === 0) return progress;
    }
    return null;
  }

  async function ingest(file) {
    const job = jobCard(file);
    try {
      job.set("hashing…", 2);
      const sha256 = await sha256Hex(file);
      const ticket = await api("/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size_bytes: file.size, sha256 }),
      });
      if (ticket.existing) {
        job.set("already in the layer (same content)", 100);
        return;
      }
      job.set("uploading…", 8);
      await sendBytes(ticket, file);
      job.set("inventorying pages…", 15);
      const document = await api(`/documents/${ticket.document_id}/finalize`, { method: "POST" });
      job.set(`${document.page_count} pages · extracting…`, 20);
      const progress = await processLoop(ticket.document_id, job);
      job.set("linking across documents…", 92);
      await linkLoop(job);
      job.set(`done: ${progress.done} pages extracted, ${progress.failed} failed. See Facts and Relations.`, 100);
      refreshDocumentOptions();
    } catch (error) {
      if (!(error instanceof PausedError)) job.set(`error: ${error.message}`, 100, true);
    }
  }

  const dropzone = $("#dropzone");
  const fileInput = $("#file-input");
  fileInput.addEventListener("change", () => { Array.from(fileInput.files).forEach(ingest); fileInput.value = ""; });
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
  dropzone.addEventListener("drop", (e) => Array.from(e.dataTransfer.files).filter((f) => f.type === "application/pdf" || f.name.endsWith(".pdf")).forEach(ingest));

  // ---------------------------------------------------------------- documents
  let documentsCache = [];
  async function refreshDocumentOptions() {
    documentsCache = await api("/documents");
    ["#facts-doc", "#relations-doc"].forEach((selector) => {
      const select = $(selector);
      const current = select.value;
      select.innerHTML = `<option value="">All documents</option>` + documentsCache.map((d) => `<option value="${d.id}">${esc(d.title || d.filename)}</option>`).join("");
      select.value = current;
    });
  }

  async function loadDocuments() {
    await refreshDocumentOptions();
    const rows = documentsCache.map((d) => {
      const pages = Object.entries(d.pages).map(([k, v]) => `${k} ${v}`).join(", ");
      return `<tr>
        <td><strong>${esc(d.title || d.filename)}</strong><div class="muted">${esc(d.filename)} · ${esc(d.publisher || "")} ${d.publication_date ? "· " + d.publication_date : ""}</div></td>
        <td><span class="badge ${d.status === "extracted" ? "ok" : "info"}">${d.status}</span></td>
        <td class="num">${d.page_count ?? ""}</td><td>${esc(pages)}</td><td class="num">${d.facts_count}</td>
        <td><button class="secondary" data-process="${d.id}">Process</button> <button class="secondary" data-delete="${d.id}">Delete</button></td>
      </tr>`;
    });
    $("#documents-table").innerHTML = rows.length
      ? `<div class="wrap"><table><thead><tr><th>Document</th><th>Status</th><th>Pages</th><th>Page status</th><th>Facts</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
      : `<div class="empty">No documents yet. Upload a PDF to begin.</div>`;
  }
  $("#documents-table").addEventListener("click", async (event) => {
    const processId = event.target.dataset.process;
    const deleteId = event.target.dataset.delete;
    if (processId) {
      event.target.disabled = true;
      const job = jobCard({ name: `reprocess ${processId.slice(0, 8)}` });
      showTab("upload");
      await processLoop(processId, job).then(() => linkLoop(job)).then(() => job.set("done", 100)).catch((e) => job.set(e.message, 100, true));
    }
    if (deleteId && window.confirm("Delete this document and its facts?")) {
      await api(`/documents/${deleteId}`, { method: "DELETE" });
      loadDocuments();
    }
  });
  $("#btn-refresh-docs").addEventListener("click", loadDocuments);
  $("#btn-link").addEventListener("click", async () => {
    const button = $("#btn-link");
    button.disabled = true;
    try {
      const progress = await linkLoop(null);
      button.textContent = progress ? `Linked: ${progress.total_relations} relations` : "Linking…";
    } finally {
      button.disabled = false;
      setTimeout(() => { button.textContent = "Run linking"; }, 4000);
    }
  });

  // ---------------------------------------------------------------- facts
  function evidenceBadges(fact) {
    const badges = [];
    if (fact.evidence_verified) badges.push(`<span class="badge ok">verified · ${fact.verify_method}</span>`);
    else if (fact.verify_method === "image_only") badges.push(`<span class="badge warn">visual evidence</span>`);
    else badges.push(`<span class="badge bad">unverified</span>`);
    if (fact.is_duplicate) badges.push(`<span class="badge">duplicate</span>`);
    (fact.validator_flags || []).forEach((flag) => badges.push(`<span class="badge warn">${esc(flag)}</span>`));
    if (fact.attributed_to) badges.push(`<span class="badge info">attributed to ${esc(fact.attributed_to)}</span>`);
    return badges.join("");
  }

  async function loadFacts() {
    const params = new URLSearchParams();
    if ($("#facts-doc").value) params.set("document_id", $("#facts-doc").value);
    if ($("#facts-q").value) params.set("q", $("#facts-q").value);
    if ($("#facts-verified").value) params.set("verified", $("#facts-verified").value);
    params.set("limit", "300");
    const facts = await api(`/facts?${params}`);
    const rows = facts.map((f) => `<tr class="clickable" data-fact='${esc(JSON.stringify(f))}'>
      <td>${esc(f.entity)}</td><td>${esc(f.attribute)}</td>
      <td class="num"><strong>${esc(printedValue(f))}</strong><div class="muted">${esc(fmtNum(f.value_num))} ${esc(f.unit)}</div></td>
      <td>${esc(f.period_raw || "")}<div class="muted">${esc(f.period_kind)} · ${esc(f.estimate_type)}</div></td>
      <td>${esc(f.document_filename || f.document_id)}<div class="muted">p. ${esc(f.page_label || f.page_index)}</div></td>
      <td>${evidenceBadges(f)}</td></tr>`);
    $("#facts-table").innerHTML = rows.length
      ? `<div class="wrap"><table><thead><tr><th>Entity</th><th>Attribute</th><th>Value</th><th>Period</th><th>Source</th><th>Evidence</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
      : `<div class="empty">No facts match.</div>`;
  }
  $("#btn-facts").addEventListener("click", loadFacts);
  $("#facts-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadFacts(); });
  $("#facts-table").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-fact]");
    if (row) showEvidence(JSON.parse(row.dataset.fact));
  });

  // ---------------------------------------------------------------- relations
  function relationFact(fact, side) {
    return `<div class="relation-fact" data-side="${side}">
      <div class="val">${esc(printedValue(fact))} <span class="muted">(${esc(fmtNum(fact.value_num))} ${esc(fact.unit)})</span></div>
      <div>${esc(fact.entity)} · ${esc(fact.attribute)}</div>
      <div class="src">${esc(fact.document_filename || fact.document_id)} · p. ${esc(fact.page_label || fact.page_index)} · ${esc(fact.period_raw || "period unknown")} · ${esc(fact.estimate_type)}${fact.measurement_basis ? " · " + esc(fact.measurement_basis) : ""}</div>
      <div class="quote">“${esc(fact.quote.slice(0, 220))}${fact.quote.length > 220 ? "…" : ""}”</div>
      <div>${evidenceBadges(fact)}</div>
    </div>`;
  }

  async function loadRelations() {
    const params = new URLSearchParams();
    if ($("#relations-verdict").value) params.set("verdict", $("#relations-verdict").value);
    if ($("#relations-doc").value) params.set("document_id", $("#relations-doc").value);
    if ($("#relations-attr").value) params.set("attribute", $("#relations-attr").value);
    params.set("limit", "400");
    const relations = await api(`/relations?${params}`);
    const order = ["contradicts", "unresolved", "superseded", "context_explained", "corroborates", "derived"];
    const groups = new Map(order.map((v) => [v, []]));
    relations.forEach((r) => { if (!groups.has(r.verdict)) groups.set(r.verdict, []); groups.get(r.verdict).push(r); });
    const html = Array.from(groups.entries()).filter(([, items]) => items.length).map(([verdict, items]) => `
      <div class="verdict-group"><h3>${esc(verdict.replace("_", " "))} <span class="muted">(${items.length})</span></h3>
      ${items.map((r) => `<div class="relation vd-${esc(r.verdict)}" data-relation='${esc(JSON.stringify({ a: r.fact_a, b: r.fact_b }))}'>
        <div class="relation-facts">${relationFact(r.fact_a, "a")}${relationFact(r.fact_b, "b")}</div>
        <div class="relation-meta">
          <span class="badge">${esc(r.method)}</span>
          ${r.dimension && r.dimension !== "none" ? `<span class="badge info">differs by ${esc(r.dimension)}</span>` : ""}
          <span class="badge">confidence ${(r.confidence * 100).toFixed(0)}%</span>
          ${r.status !== "final" ? `<span class="badge warn">${esc(r.status)}</span>` : ""}
          ${r.winner_fact_id ? `<span class="badge ok">current: ${r.winner_fact_id === r.fact_a.id ? "left" : "right"}</span>` : ""}
          <div class="explanation">${esc(r.explanation)}</div>
        </div></div>`).join("")}</div>`);
    $("#relations-list").innerHTML = html.length ? html.join("") : `<div class="empty">No relations yet. Process at least two documents, then run linking.</div>`;
  }
  $("#btn-relations").addEventListener("click", loadRelations);
  $("#relations-list").addEventListener("click", (event) => {
    const side = event.target.closest(".relation-fact");
    const card = event.target.closest(".relation[data-relation]");
    if (side && card) showEvidence(JSON.parse(card.dataset.relation)[side.dataset.side]);
  });

  // ---------------------------------------------------------------- failures
  async function loadFailures() {
    const params = new URLSearchParams();
    if ($("#failures-stage").value) params.set("stage", $("#failures-stage").value);
    const failures = await api(`/failures?${params}`);
    const rows = failures.map((f) => `<tr>
      <td><span class="badge">${esc(f.stage)}</span></td><td><span class="badge warn">${esc(f.kind)}</span></td>
      <td>${esc((f.document_id || "").slice(0, 8))}${f.page_index !== null ? ` · p${f.page_index}` : ""}${f.fact_id ? ` · fact ${f.fact_id}` : ""}</td>
      <td>${esc(f.message)}</td><td>${esc(f.handled)}</td></tr>`);
    $("#failures-table").innerHTML = rows.length
      ? `<div class="wrap"><table><thead><tr><th>Stage</th><th>Kind</th><th>Where</th><th>What happened</th><th>What the system did</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
      : `<div class="empty">No failures recorded.</div>`;
  }
  $("#btn-failures").addEventListener("click", loadFailures);

  // ---------------------------------------------------------------- schema
  async function loadSchema() {
    const entries = await api("/schema");
    const rows = entries.map((e) => `<tr><td><strong>${esc(e.display_name)}</strong><div class="muted">${esc(e.attribute_key)}</div></td>
      <td class="num">${e.count}</td><td>${esc(e.units.join(", "))}</td><td>${esc(e.entities.join(", "))}</td><td>${esc(e.estimate_types.join(", "))}</td></tr>`);
    $("#schema-table").innerHTML = rows.length
      ? `<div class="wrap"><table><thead><tr><th>Attribute</th><th>Facts</th><th>Units</th><th>Entities</th><th>Estimate types</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
      : `<div class="empty">Nothing discovered yet.</div>`;
  }

  // ---------------------------------------------------------------- evidence panel
  function highlight(text, quote) {
    if (!quote) return esc(text);
    const index = text.indexOf(quote);
    if (index >= 0) return `${esc(text.slice(0, index))}<mark>${esc(quote)}</mark>${esc(text.slice(index + quote.length))}`;
    const loose = quote.replace(/\s+/g, " ").trim().split(" ").slice(0, 4).join(" ");
    const looseIndex = loose ? text.replace(/\s+/g, " ").indexOf(loose) : -1;
    return looseIndex >= 0 ? esc(text) : esc(text);
  }

  async function showEvidence(fact) {
    const layout = $(".layout");
    const panel = $("#evidence");
    layout.classList.add("with-evidence");
    panel.classList.remove("hidden");
    $("#evidence-title").textContent = `${fact.entity} · ${fact.attribute}`;
    const body = $("#evidence-body");
    body.innerHTML = `<p class="muted">loading page…</p>`;
    const page = await api(`/documents/${fact.document_id}/pages/${fact.page_index}`);
    const imageUrl = `/documents/${fact.document_id}/pages/${fact.page_index}/image.jpg?width=900`;
    body.innerHTML = `
      <div>${evidenceBadges(fact)}</div>
      <dl class="kv">
        <dt>Value</dt><dd><strong>${esc(printedValue(fact))}</strong> → ${esc(fmtNum(fact.value_num))} ${esc(fact.unit)}</dd>
        <dt>Period</dt><dd>${esc(fact.period_raw || "unknown")} ${fact.period_start ? `(${fact.period_start} → ${fact.period_end})` : ""} · ${esc(fact.estimate_type)}</dd>
        <dt>Basis</dt><dd>${esc(fact.measurement_basis || "not stated")}</dd>
        <dt>Source</dt><dd>${esc(fact.document_filename || fact.document_id)} · page index ${fact.page_index} · printed ${esc(fact.page_label || "n/a")}</dd>
        <dt>Confidence</dt><dd>${(fact.confidence * 100).toFixed(0)}% (model) · quote match ${(fact.verify_score * 100).toFixed(0)}%</dd>
        ${fact.notes ? `<dt>Notes</dt><dd>${esc(fact.notes)}</dd>` : ""}
      </dl>
      <button class="secondary" id="btn-timeline">Show vintage timeline</button>
      <div class="timeline" id="timeline"></div>
      <h3>Page image</h3>
      <img src="${imageUrl}" alt="page ${fact.page_index}" loading="lazy" />
      <h3>Text layer${fact.quote_source === "image" ? " (quote was read from the image)" : ""}</h3>
      <pre>${highlight(page.text, fact.quote)}</pre>`;
    $("#btn-timeline").addEventListener("click", async () => {
      const timeline = await api(`/facts/timeline?entity_key=${encodeURIComponent(fact.entity_key)}&attribute_key=${encodeURIComponent(fact.attribute_key)}`);
      $("#timeline").innerHTML = timeline.entries.length
        ? timeline.entries.map((e) => `<div class="timeline-entry ${e.is_current ? "current" : ""}">
            <span class="muted">${esc(e.publication_date || "n.d.")}</span>
            <span>${esc(printedValue(e.fact))} · ${esc(e.fact.period_raw || e.period_key)} · ${esc(e.document_title || e.fact.document_filename)}
            ${e.is_current ? `<span class="badge ok">current</span>` : `<span class="badge">superseded by #${e.superseded_by}</span>`}</span>
          </div>`).join("")
        : `<div class="muted">No other vintages of this fact.</div>`;
    });
    const marked = $("mark", body);
    if (marked) marked.scrollIntoView({ block: "center" });
  }
  $("#evidence-close").addEventListener("click", () => {
    $("#evidence").classList.add("hidden");
    $(".layout").classList.remove("with-evidence");
  });

  refreshDocumentOptions().catch(() => {});
})();
