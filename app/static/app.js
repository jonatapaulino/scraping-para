const API = "/api/v1";
let docPage = 1, docPages = 1;
let secretariasCache = [];
let secsLoaded = false, folhaOpcoesLoaded = false;

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
    $("docs-24h").textContent =
      `${o.last_24h.documents_ok} ok · ${o.last_24h.documents_failed} falhas`;
    $("jobs-running").textContent = o.jobs.RUNNING ?? 0;
  } catch (e) {
    $("api-status").textContent = "API offline";
    $("api-status").className = "pill off";
  }
}

$("btn-requeue").addEventListener("click", async () => {
  $("btn-requeue").disabled = true;
  try {
    const r = await api("/dead-letters/requeue", { method: "POST" });
    toast(`${r.requeued} dead letter(s) reprocessada(s)`);
    loadOverview();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("btn-requeue").disabled = false;
  }
});

// ---------- Secretarias ----------

async function loadSecretarias() {
  secretariasCache = await api("/secretarias");
  $("secretaria-checks").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<label title="${s.tipo === 'crawl' ? s.url_base : 'fonte de API — use a seção Folha'}"><input type="checkbox" value="${s.sigla}" ${
              s.is_ativa && s.tipo === "crawl" ? "" : "disabled"
            }> ${s.sigla}${s.is_ativa ? "" : " (inativa)"}${s.tipo === "crawl" ? "" : " (API)"}</label>`
        )
        .join("")
    : '<span class="empty">nenhuma cadastrada</span>';
  $("secs-tbody").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<tr><td class="nowrap"><b>${s.sigla}</b></td><td class="url">${s.url_base}</td>` +
            `<td class="nowrap">${s.max_depth ?? "padrão"} <button class="mini" data-depth-id="${s.id}" data-depth="${s.max_depth ?? ""}" title="Editar profundidade">✎</button></td>` +
            `<td>${s.is_ativa ? "✅" : "—"}</td>` +
            `<td><button class="mini" data-toggle-id="${s.id}" data-ativa="${s.is_ativa}">${
              s.is_ativa ? "desativar" : "reativar"
            }</button></td>` +
            `<td class="nowrap">${s.tipo === "crawl" ? "" : '<span class="badge API">API</span>'}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="6" class="empty">vazio</td></tr>';
  const sel = $("f-sec");
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">Todas secretarias</option>' +
    secretariasCache.map((s) => `<option value="${s.id}">${s.sigla}</option>`).join("");
  sel.value = cur;
  secsLoaded = true;
}

$("secs-tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  try {
    if (btn.dataset.toggleId) {
      await api(`/secretarias/${btn.dataset.toggleId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_ativa: btn.dataset.ativa !== "true" }),
      });
      toast("Secretaria atualizada");
      loadSecretarias();
    } else if (btn.dataset.depthId) {
      const cur = btn.dataset.depth;
      const v = prompt("Profundidade (0-10; vazio = padrão do sistema)", cur);
      if (v === null) return;
      const n = v.trim() === "" ? null : parseInt(v, 10);
      if (n !== null && (isNaN(n) || n < 0 || n > 10))
        return toast("Valor inválido — use 0 a 10 ou deixe vazio", true);
      await api(`/secretarias/${btn.dataset.depthId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_depth: n }),
      });
      toast("Profundidade atualizada");
      loadSecretarias();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

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
        max_depth: Number.isNaN(parseInt($("max-depth").value, 10))
          ? null
          : parseInt($("max-depth").value, 10),
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

// ---------- Dados abertos (transparência) ----------

async function loadFolhaOpcoes() {
  try {
    const ds = $("folha-dataset").value;
    // "*" (todos os datasets): usa as opcoes da folha so como referencia
    // de periodo/orgaos — cada dataset resolve os proprios no backend
    const o = await api(`/transparencia/opcoes?dataset=${ds === "*" ? "folha" : ds}`);
    const snapshot = o.periodos.length === 0;
    const opts = o.periodos
      .map((p) => {
        const mes = p.mes ?? 1;
        const rotulo = p.mes ? `${p.ano}-${String(mes).padStart(2, "0")}` : `${p.ano}`;
        return `<option value="${p.ano}-${mes}">${rotulo}</option>`;
      })
      .join("");
    $("folha-de").innerHTML = snapshot ? '<option value="2025-1">—</option>' : opts;
    $("folha-ate").innerHTML = snapshot ? '<option value="2025-1">—</option>' : opts;
    $("folha-de").disabled = $("folha-ate").disabled = snapshot;
    if (o.periodos.length) {
      // default: ano corrente inteiro (ou o que houver)
      const ini = o.periodos.findIndex((p) => p.ano === o.periodos[o.periodos.length - 1].ano);
      $("folha-de").selectedIndex = Math.max(0, ini);
      $("folha-ate").selectedIndex = o.periodos.length - 1;
    }
    $("folha-orgaos-checks").innerHTML = o.orgaos.length
      ? o.orgaos
          .map(
            (g) =>
              `<label title="${g.orgao}"><input type="checkbox" value="${g.sigla}"> ${g.sigla}</label>`
          )
          .join("")
      : '<span class="empty">sem órgãos</span>';
    // reaplica o estado atual de "todos" (a lista acaba de ser recriada)
    const todosOn = $("folha-todos").checked;
    document.querySelectorAll("#folha-orgaos-checks input").forEach((c) => {
      c.disabled = todosOn;
      if (todosOn) c.checked = false;
    });
    folhaOpcoesLoaded = true;
  } catch (e) {
    $("folha-orgaos-checks").innerHTML = '<span class="empty">falha ao carregar opções</span>';
  }
}

$("folha-dataset").addEventListener("change", loadFolhaOpcoes);

$("folha-todos").addEventListener("change", () => {
  const on = $("folha-todos").checked;
  document.querySelectorAll("#folha-orgaos-checks input").forEach((c) => {
    c.disabled = on;
    if (on) c.checked = false;
  });
});

$("btn-folha").addEventListener("click", async () => {
  const todos = $("folha-todos").checked;
  const orgaos = todos
    ? null
    : [...document.querySelectorAll("#folha-orgaos-checks input:checked")].map((c) => c.value);
  if (!todos && !orgaos.length) return toast("Selecione ao menos um órgão (ou marque todos)", true);
  const [anoIni, mesIni] = $("folha-de").value.split("-").map(Number);
  const [anoFim, mesFim] = $("folha-ate").value.split("-").map(Number);
  $("btn-folha").disabled = true;
  try {
    const r = await api("/transparencia/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset: $("folha-dataset").value,
        ano_ini: anoIni, mes_ini: mesIni,
        ano_fim: anoFim, mes_fim: mesFim,
        orgaos, force: $("folha-force").checked,
      }),
    });
    if (r.job_ids) {
      $("folha-result").textContent = `${r.job_ids.length} jobs disparados (1 por dataset)`;
      toast(`${r.job_ids.length} coletas disparadas`);
    } else {
      $("folha-result").textContent = "job: " + r.job_id;
      toast("Coleta disparada");
    }
    loadJobs();
    loadOverview();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("btn-folha").disabled = false;
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
            `<td class="mono">${
              i.minio_path
                ? `<a href="${API}/documents/${i.id}/file" target="_blank" title="${i.minio_path}">${i.minio_path}</a>`
                : "—"
            }</td>` +
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
  // self-healing: o que falhar no boot (API ainda subindo) retenta aqui
  // ate conseguir uma vez — depois nao re-entra p/ nao apagar a selecao
  if (!secsLoaded) loadSecretarias().catch(() => {});
  if (!folhaOpcoesLoaded) loadFolhaOpcoes();
}

loadSecretarias().catch(() => {}).then(refreshAll);
loadFolhaOpcoes();
setInterval(() => { if ($("autorefresh").checked) refreshAll(); }, 5000);
