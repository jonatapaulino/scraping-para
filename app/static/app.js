const API = "/api/v1";
let docPage = 1, docPages = 1;
let secretariasCache = [];

const $ = (id) => document.getElementById(id);
const fmtDate = (s) => (s ? new Date(s).toLocaleString("pt-BR") : "—");
const fmtBytes = (n) =>
  n == null ? "—" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(1) + " KB" : n + " B";
const badge = (st) => `<span class="badge ${st}">${st}</span>`;

async function api(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.status === 204 ? null : r.json();
}

function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " error" : "");
  el.textContent = msg;
  $("toast").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// ---------- Overview ----------

async function loadOverview() {
  try {
    const o = await api("/overview");
    $("api-status").textContent = "API online";
    $("api-status").className = "pill on";
    $("q-discovery").textContent = o.queues["q.discovery"] ?? "–";
    $("q-download").textContent = o.queues["q.download"] ?? "–";
    $("q-dead").textContent = o.queues["q.dead"] ?? "–";
    $("docs-ok").textContent = o.documents.SUCCESS ?? 0;
    $("docs-fail").textContent = o.documents.FAILED ?? 0;
    $("jobs-running").textContent = o.jobs.RUNNING ?? 0;
  } catch (e) {
    $("api-status").textContent = "API offline";
    $("api-status").className = "pill off";
  }
}

// ---------- Secretarias ----------

async function loadSecretarias() {
  secretariasCache = await api("/secretarias");
  $("secretaria-checks").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<label title="${s.url_base}"><input type="checkbox" value="${s.sigla}" ${
              s.is_ativa ? "" : "disabled"
            }> ${s.sigla}${s.is_ativa ? "" : " (inativa)"}</label>`
        )
        .join("")
    : '<span class="empty">nenhuma cadastrada</span>';
  $("secs-tbody").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<tr><td class="nowrap"><b>${s.sigla}</b></td><td class="url">${s.url_base}</td><td>${
              s.is_ativa ? "✅" : "—"
            }</td></tr>`
        )
        .join("")
    : '<tr><td colspan="3" class="empty">vazio</td></tr>';
  const sel = $("f-sec");
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">Todas secretarias</option>' +
    secretariasCache.map((s) => `<option value="${s.id}">${s.sigla}</option>`).join("");
  sel.value = cur;
}

$("sec-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/secretarias", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sigla: $("sec-sigla").value, url_base: $("sec-url").value }),
    });
    toast("Secretaria cadastrada");
    $("sec-sigla").value = $("sec-url").value = "";
    loadSecretarias();
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------- Jobs ----------

async function loadJobs() {
  const jobs = await api("/jobs?limit=15");
  $("jobs-tbody").innerHTML = jobs.length
    ? jobs
        .map(
          (j) =>
            `<tr><td class="mono" title="${j.job_id}">${j.job_id.slice(0, 8)}…</td>` +
            `<td class="nowrap">${j.secretaria}</td><td class="nowrap">${badge(j.status)}</td>` +
            `<td>${j.pdfs_descobertos}</td><td>${j.pdfs_baixados_sucesso}</td>` +
            `<td>${j.pdfs_falhas}</td><td class="mono">${fmtDate(j.started_at)}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="7" class="empty">nenhum job ainda</td></tr>';
}

$("btn-start").addEventListener("click", async () => {
  const selecionadas = [...document.querySelectorAll("#secretaria-checks input:checked")].map(
    (c) => c.value
  );
  if (!selecionadas.length) return toast("Selecione ao menos uma secretaria", true);
  $("btn-start").disabled = true;
  try {
    const r = await api("/jobs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        secretarias: selecionadas,
        force_full_scrape: $("force").checked,
        max_depth: parseInt($("max-depth").value || "2", 10),
      }),
    });
    $("start-result").textContent = "jobs: " + r.job_ids.join(", ");
    toast(`${r.job_ids.length} job(s) disparado(s)`);
    loadJobs();
    loadOverview();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("btn-start").disabled = false;
  }
});

// ---------- Documentos ----------

async function loadDocuments() {
  const params = new URLSearchParams({ page: docPage, size: 12 });
  if ($("f-sec").value) params.set("secretaria_id", $("f-sec").value);
  if ($("f-status").value) params.set("status_download", $("f-status").value);
  const d = await api("/documents?" + params);
  docPages = d.pages || 1;
  $("docs-tbody").innerHTML = d.items.length
    ? d.items
        .map(
          (i) =>
            `<tr><td class="url" title="${i.url_origem}">${i.url_origem}</td>` +
            `<td class="nowrap">${badge(i.status_download)}</td>` +
            `<td>${i.magic_byte_valido === null ? "—" : i.magic_byte_valido ? "✅" : "❌"}</td>` +
            `<td>${fmtBytes(i.tamanho_bytes)}</td>` +
            `<td class="mono" title="${i.hash_sha256 || ""}">${i.hash_sha256 ? i.hash_sha256.slice(0, 10) + "…" : "—"}</td>` +
            `<td class="mono">${i.minio_path || "—"}</td>` +
            `<td class="mono">${fmtDate(i.created_at)}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="7" class="empty">sem documentos</td></tr>';
  $("pg-info").textContent = `pág. ${d.page}/${d.pages} · ${d.total} docs`;
  $("pg-prev").disabled = docPage <= 1;
  $("pg-next").disabled = docPage >= docPages;
}

$("pg-prev").addEventListener("click", () => { if (docPage > 1) { docPage--; loadDocuments(); } });
$("pg-next").addEventListener("click", () => { if (docPage < docPages) { docPage++; loadDocuments(); } });
$("f-sec").addEventListener("change", () => { docPage = 1; loadDocuments(); });
$("f-status").addEventListener("change", () => { docPage = 1; loadDocuments(); });

// ---------- Loop ----------

function refreshAll() {
  loadOverview();
  loadJobs();
  loadDocuments();
}

loadSecretarias().then(refreshAll);
setInterval(() => { if ($("autorefresh").checked) refreshAll(); }, 5000);
