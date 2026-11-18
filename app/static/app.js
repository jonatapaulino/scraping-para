const API = "/api/v1";
let docPage = 1, docPages = 1;
let secretariasCache = [];
let secsLoaded = false, folhaOpcoesLoaded = false;

const $ = (id) => document.getElementById(id);
const fmtDate = (s) => (s ? new Date(s).toLocaleString("pt-BR") : "—");
const fmtBytes = (n) =>
  n == null ? "—" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(1) + " KB" : n + " B";
const ROTULOS = { RUNNING: "Em andamento", COMPLETED: "Concluído", FAILED: "Falhou", SUCCESS: "Sucesso", PENDING: "Pendente" };
const badge = (st) => `<span class="badge ${st}">${ROTULOS[st] || st}</span>`;
const barra = (pct, feito) =>
  `<div class="barra-linha"><div class="bar"><div class="bar-fill${feito ? " done" : ""}" style="width:${pct}%"></div></div><span class="bar-pct">${pct}%</span></div>`;

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

// ---------- Tema (automático / claro / escuro) ----------

(function iniciaTema() {
  const sel = $("tema");
  let escolha = "auto";
  try { escolha = localStorage.getItem("tema") || "auto"; } catch (e) {}
  sel.value = escolha;
  sel.addEventListener("change", () => {
    try { localStorage.setItem("tema", sel.value); } catch (e) {}
    aplicarTema(sel.value);
  });
  // no modo automático, acompanha a mudança do sistema operacional
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (sel.value === "auto") aplicarTema("auto");
  });
})();

// ---------- Anos do Diário Oficial (aparece só com IOEPA marcado) ----------

(function iniciaAnosDiario() {
  const atual = new Date().getFullYear();
  $("ano-ini").value = atual - 1;
  $("ano-fim").value = atual;
  $("secretaria-checks").addEventListener("change", () => {
    $("anos-diario").hidden = !document.querySelector('#secretaria-checks input[value="IOEPA"]:checked');
  });
})();

// ---------- Overview ----------

async function loadOverview() {
  try {
    const o = await api("/overview");
    $("api-status").textContent = "API conectada";
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
    $("api-status").textContent = "API desconectada";
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
            `<label title="${s.tipo === 'crawl' ? s.url_base : (s.sigla === 'IOEPA' ? 'Diário Oficial do Estado — escolha os anos ao lado' : 'fonte de API — use a seção Folha')}"><input type="checkbox" value="${s.sigla}" ${
              s.is_ativa && (s.tipo === "crawl" || s.sigla === "IOEPA") ? "" : "disabled"
            }> ${s.sigla}${s.is_ativa ? "" : " (inativa)"}${s.tipo === "crawl" ? "" : s.sigla === "IOEPA" ? " (Diário Oficial)" : " (API)"}</label>`
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
            `<td class="nowrap">${s.tipo === "crawl" ? "" : `<span class="badge API">${s.sigla === "IOEPA" ? "Diário" : "API"}</span>`}</td></tr>`
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
    const depth = parseInt($("sec-depth").value, 10);
    const sec = await api("/secretarias", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sigla: $("sec-sigla").value,
        url_base: $("sec-url").value,
        max_depth: Number.isNaN(depth) ? null : depth,
      }),
    });
    toast("Fonte cadastrada");
    if ($("sec-run").checked) {
      const r = await api("/jobs/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secretarias: [sec.sigla], force_full_scrape: false, max_depth: null }),
      });
      toast(`${r.job_ids.length} tarefa(s) iniciada(s) para ${sec.sigla}`);
      loadJobs();
      loadOverview();
    }
    $("sec-sigla").value = $("sec-url").value = $("sec-depth").value = "";
    $("sec-run").checked = false;
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
            `<td>${j.pdfs_descobertos > 0 ? barra(Math.min(100, Math.round(((j.pdfs_baixados_sucesso + j.pdfs_falhas) * 100) / j.pdfs_descobertos)), j.status === "COMPLETED") : "–"}</td>` +
            `<td>${j.pdfs_descobertos}</td><td>${j.pdfs_baixados_sucesso}</td>` +
            `<td>${j.pdfs_falhas}</td><td class="mono">${fmtDate(j.started_at)}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="8" class="empty">nenhuma tarefa ainda</td></tr>';
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
        ...(selecionadas.includes("IOEPA")
          ? { ano_ini: parseInt($("ano-ini").value, 10), ano_fim: parseInt($("ano-fim").value, 10) }
          : {}),
        force_full_scrape: $("force").checked,
        max_depth: Number.isNaN(parseInt($("max-depth").value, 10))
          ? null
          : parseInt($("max-depth").value, 10),
      }),
    });
    $("start-result").textContent = "tarefas: " + r.job_ids.join(", ");
    toast(`${r.job_ids.length} tarefa(s) iniciada(s)`);
    loadJobs();
    loadOverview();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("btn-start").disabled = false;
  }
});

// ---------- Dados abertos (transparência) ----------

// Lista de órgãos: SEMPRE a lista completa (base = despesas). O que o dataset
// escolhido não tem aparece em cinza e não dá para marcar; o que só ele tem
// (ex.: convênios) entra no fim. O valor enviado é a chave do próprio dataset.
const normOrg = (t) =>
  (t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toUpperCase().replace(/[^A-Z0-9]/g, "");

function montaOrgaos(base, doDataset) {
  const marcados = new Set(
    [...document.querySelectorAll("#folha-orgaos-checks input:checked")].map((c) => c.dataset.chave)
  );
  const aspas = (t) => String(t || "").replace(/"/g, "&quot;");
  const item = (valor, rotulo, titulo, disponivel, marcado) =>
    `<label class="${disponivel ? "" : "indisp"}" title="${aspas(titulo)}">` +
    `<input type="checkbox" data-chave="${aspas(rotulo)}" value="${aspas(valor)}" ` +
    `${disponivel ? "" : 'disabled data-indisp="1"'} ${marcado ? "checked" : ""}> ${rotulo}</label>`;
  const usados = new Set();
  // a lista base escreve "Nome do órgão-SIGLA": compara também sem o final "-SIGLA"
  const nomeSemSigla = (m) => {
    const nome = normOrg(m.orgao), sg = normOrg(m.sigla);
    return sg && nome.endsWith(sg) ? nome.slice(0, -sg.length) : nome;
  };
  const indiceDe = (m) =>
    doDataset.findIndex((e, i) =>
      !usados.has(i) &&
      [e.sigla, e.orgao].some(
        (v) =>
          normOrg(v) &&
          (normOrg(v) === normOrg(m.sigla) || normOrg(v) === normOrg(m.orgao) || normOrg(v) === nomeSemSigla(m))
      )
    );
  const linhas = base.map((m) => {
    const i = indiceDe(m);
    if (i < 0) return item("", m.sigla, `${m.orgao} — sem dados neste dataset`, false, false);
    usados.add(i);
    const e = doDataset[i];
    return item(e.sigla, m.sigla, e.orgao || m.orgao, true, marcados.has(m.sigla));
  });
  doDataset.forEach((e, i) => {
    if (!usados.has(i)) linhas.push(item(e.sigla, e.sigla, e.orgao, true, marcados.has(e.sigla)));
  });
  return linhas.join("");
}

async function loadFolhaOpcoes() {
  try {
    const ds = $("folha-dataset").value;
    // "*" (todos os datasets): usa as opcoes da folha so como referencia
    // de periodo/orgaos — cada dataset resolve os proprios no backend
    const o = await api(`/transparencia/opcoes?dataset=${ds === "*" ? "despesas" : ds}`);
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
    let base = o.orgaos;
    if (ds !== "*" && ds !== "despesas") {
      try {
        base = (await api("/transparencia/opcoes?dataset=despesas")).orgaos;
      } catch (e) {
        base = o.orgaos; // sem a lista base: mostra só os do dataset
      }
    }
    // Diário Oficial (IOEPA) fica sempre na lista; só é marcável no dataset dele
    if (!base.some((g) => g.sigla === "IOEPA")) {
      base = [...base, { sigla: "IOEPA", orgao: "Imprensa Oficial do Estado — Diário Oficial" }];
    }
    $("folha-orgaos-checks").innerHTML =
      base.length || o.orgaos.length ? montaOrgaos(base, o.orgaos) : '<span class="empty">sem órgãos</span>';
    // reaplica o estado atual de "todos" (a lista acaba de ser recriada)
    const todosOn = $("folha-todos").checked;
    document.querySelectorAll("#folha-orgaos-checks input").forEach((c) => {
      c.disabled = todosOn || c.dataset.indisp === "1";
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
    c.disabled = on || c.dataset.indisp === "1";
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

const fmtHora = (iso) =>
  iso
    ? new Date(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
    : "";

// tarefas terminadas ficam na lista até você dispensar (guardado neste navegador)
function tarefasDispensadas() {
  try {
    return new Set(JSON.parse(localStorage.getItem("tarefasDispensadas") || "[]"));
  } catch (e) {
    return new Set();
  }
}

function dispensarTarefa(id) {
  const d = tarefasDispensadas();
  d.add(id);
  try {
    localStorage.setItem("tarefasDispensadas", JSON.stringify([...d].slice(-200)));
  } catch (e) {}
}

async function loadProgressoTarefas() {
  try {
    const p = await api("/progresso/tarefas");
    const dispensadas = tarefasDispensadas();
    const tarefas = p.tarefas.filter((t) => t.situacao === "RUNNING" || !dispensadas.has(t.job_id));
    if (!tarefas.length) { $("prog-tarefas").hidden = true; return; }
    $("prog-tarefas").hidden = false;
    const g = p.geral;
    $("prog-geral").hidden = g.tarefas_rodando < 2;
    if (g.tarefas_rodando >= 2) {
      const pg = g.percentual ?? 0;
      $("prog-geral-txt").textContent = `Geral: ${g.tarefas_rodando} tarefas rodando — ${g.concluidos} de ${g.total} itens`;
      $("prog-geral-bar").style.width = pg + "%";
      $("prog-geral-pct").textContent = pg + "%";
    }
    $("prog-tarefas-lista").innerHTML = tarefas
      .map((t) => {
        const feito = t.situacao === "COMPLETED";
        const falhou = t.situacao === "FAILED";
        const falhas = t.falhas ? ` · ${t.falhas} falha(s)` : "";
        const quando = fmtHora(t.concluida_em);
        const texto = feito
          ? `✓ Download concluído${quando ? " em " + quando : ""} — ${t.texto}${falhas}`
          : falhou
            ? `✗ Interrompida${quando ? " em " + quando : ""} — ${t.texto}${falhas}`
            : `${t.texto}${falhas}`;
        const fechar = t.situacao === "RUNNING"
          ? ""
          : `<button class="mini" data-dispensar="${t.job_id}" title="Remover da lista">×</button>`;
        const pct = feito && t.percentual == null ? 100 : t.percentual;
        return `<div class="prog-item"><div class="prog-resumo"><span><b>${t.fonte}</b> <span class="hint">${t.descricao}</span></span>` +
          `<span class="nowrap">${badge(t.situacao)} ${fechar}</span></div>` +
          `${pct == null ? "" : barra(pct, feito)}<div class="hint">${texto}</div></div>`;
      })
      .join("");
  } catch (err) {
    $("prog-tarefas").hidden = true;
  }
}

$("prog-tarefas-lista").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-dispensar]");
  if (!b) return;
  dispensarTarefa(b.dataset.dispensar);
  loadProgressoTarefas();
});

function refreshAll() {
  loadOverview();
  loadProgressoTarefas();
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
