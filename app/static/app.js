const API = "/api/v1";
let docPage = 1, docPages = 1;
let secretariasCache = [];
let secsLoaded = false, folhaOpcoesLoaded = false;

const $ = (id) => document.getElementById(id);
const fmtDate = (s) => (s ? new Date(s).toLocaleString("pt-BR") : "—");
const fmtBytes = (n) =>
  n == null ? "—" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(1) + " KB" : n + " B";
const ROTULOS = { RUNNING: "Em andamento", COMPLETED: "Concluído", FAILED: "Falhou", CANCELLED: "Cancelado", SUCCESS: "Sucesso", PENDING: "Pendente" };
const badge = (st) => `<span class="badge ${st}">${ROTULOS[st] || st}</span>`;
const barra = (pct, feito) =>
  `<div class="barra-linha"><div class="bar"><div class="bar-fill${feito ? " done" : ""}" style="width:${pct}%"></div></div><span class="bar-pct">${pct}%</span></div>`;

async function api(path, opts) {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const d = body.detail;
    // 422 do FastAPI manda uma lista de erros de validacao
    const msg = Array.isArray(d) ? d.map((x) => (x.msg || "").replace(/^Value error, /, "")).join("; ") : d;
    throw new Error(msg || `HTTP ${r.status}`);
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
            `<label title="${s.tipo === 'crawl' || s.tipo === 'coleta' ? s.url_base : (s.sigla === 'IOEPA' ? 'Diário Oficial do Estado — escolha os anos ao lado' : 'fonte de API — use a seção Folha')}"><input type="checkbox" value="${s.sigla}" ${
              s.is_ativa && (s.tipo === "crawl" || s.tipo === "coleta" || s.sigla === "IOEPA") ? "" : "disabled"
            }> ${s.sigla}${s.is_ativa ? "" : " (inativa)"}${s.tipo === "crawl" ? "" : s.tipo === "coleta" ? " (coleta)" : s.sigla === "IOEPA" ? " (Diário Oficial)" : " (API)"}</label>`
        )
        .join("")
    : '<span class="empty">nenhuma cadastrada</span>';
  $("secs-tbody").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<tr><td class="nowrap"><b>${s.sigla}</b></td><td class="url">${s.url_base}${
              s.tipo === "crawl" ? ` <button class="mini" data-url-id="${s.id}" data-url="${s.url_base}" title="Editar URL">✎</button>`
              : s.tipo === "coleta" ? ` <button class="mini" data-coleta-id="${s.id}" title="Editar a configuração desta fonte (abre o formulário acima)">✎ config</button>` : ""
            }</td>` +
            `<td class="nowrap">${s.tipo === "coleta" ? "—" : `${s.max_depth ?? "padrão"} <button class="mini" data-depth-id="${s.id}" data-depth="${s.max_depth ?? ""}" title="Editar profundidade">✎</button>`}</td>` +
            `<td class="nowrap">${
              s.tipo === "coleta" ? "CSV" : s.tipo !== "crawl" ? "" :
              (s.pdf_filtro === null ? "padrão" : s.pdf_filtro === "" ? "todos" : `<code title="${s.pdf_filtro.replace(/"/g, "&quot;")}">regex</code>`)
            } ${s.tipo === "crawl" ? `<button class="mini" data-filtro-id="${s.id}" data-filtro="${(s.pdf_filtro ?? "").replace(/"/g, "&quot;")}" data-filtro-null="${s.pdf_filtro === null}" title="Editar filtro de PDFs">✎</button>` : ""}</td>` +
            `<td>${s.is_ativa ? "✅" : "—"}</td>` +
            `<td class="nowrap">${
              s.tipo === "crawl" || s.tipo === "coleta" || s.sigla === "IOEPA"
                ? `<input type="checkbox" data-agenda-id="${s.id}" ${s.agendada ? "checked" : ""} title="Raspar todo dia"> ` +
                  `<input type="time" data-hora-id="${s.id}" value="${s.agenda_hora || "06:00"}" ${s.agendada ? "" : "disabled"} style="width:6.5rem">`
                : "—"
            }</td>` +
            `<td><button class="mini" data-toggle-id="${s.id}" data-ativa="${s.is_ativa}">${
              s.is_ativa ? "desativar" : "reativar"
            }</button>${
              s.tipo === "crawl" || s.tipo === "coleta" ? ` <button class="mini" data-del-id="${s.id}" data-sigla="${s.sigla}" title="Excluir fonte e histórico (os arquivos baixados ficam)">excluir</button>` : ""
            }</td>` +
            `<td class="nowrap"><span class="badge ${s.tipo === "crawl" ? "PORTAL" : s.tipo === "coleta" ? "COLETA" : "API"}" title="${
              s.tipo === "crawl" ? "Portal: baixa os PDFs do site" : s.tipo === "coleta" ? "Coleta: API ou tabela, gera CSV"
              : s.sigla === "IOEPA" ? "Diário Oficial do Estado" : "Fonte de API (dados abertos)"
            }">${s.tipo === "crawl" ? "Portal" : s.tipo === "coleta" ? "Coleta" : s.sigla === "IOEPA" ? "Diário" : "API"}</span></td></tr>`
        )
        .join("")
    : '<tr><td colspan="8" class="empty">vazio</td></tr>';
  const sel = $("f-sec");
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">Todas secretarias</option>' +
    secretariasCache.map((s) => `<option value="${s.id}">${s.sigla}</option>`).join("");
  sel.value = cur;
  secsLoaded = true;
}

// agenda diária: caixa liga/desliga, campo de hora muda o horário
$("secs-tbody").addEventListener("change", async (e) => {
  const t = e.target;
  const id = t.dataset.agendaId || t.dataset.horaId;
  if (!id) return;
  const hora = document.querySelector(`input[data-hora-id="${id}"]`);
  const patch = (body) =>
    api(`/secretarias/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  try {
    if (t.dataset.agendaId) {
      await patch(t.checked ? { agendada: true, agenda_hora: hora.value || "06:00" } : { agendada: false });
      hora.disabled = !t.checked;
      toast(t.checked ? `Agendada: todo dia às ${hora.value || "06:00"}` : "Agenda desligada");
    } else {
      if (!t.value) return;
      await patch({ agenda_hora: t.value });
      toast(`Horário alterado: todo dia às ${t.value}`);
    }
    loadSecretarias();
  } catch (err) {
    toast(err.message, true);
    loadSecretarias();
  }
});

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
    } else if (btn.dataset.coletaId) {
      editarColeta(btn.dataset.coletaId);
    } else if (btn.dataset.urlId) {
      const v = prompt("Nova URL base", btn.dataset.url);
      if (v === null || !v.trim() || v.trim() === btn.dataset.url) return;
      await api(`/secretarias/${btn.dataset.urlId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url_base: v.trim() }),
      });
      toast("URL atualizada");
      loadSecretarias();
    } else if (btn.dataset.filtroId) {
      const atual = btn.dataset.filtroNull === "true" ? "" : (btn.dataset.filtro === "" ? "*" : btn.dataset.filtro);
      const v = prompt("Quais PDFs baixar?\n• vazio = padrão do sistema (só financeiros: editais, contratos…)\n• * = todos os PDFs do site\n• ou uma regex, ex.: remunera|inativ", atual);
      if (v === null) return;
      await api(`/secretarias/${btn.dataset.filtroId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pdf_filtro: v.trim() === "" ? null : v.trim() }),
      });
      toast("Filtro atualizado — vale a partir da próxima tarefa");
      loadSecretarias();
    } else if (btn.dataset.delId) {
      if (!confirm(`Excluir ${btn.dataset.sigla}? Remove a fonte e o histórico dela do painel. Os arquivos já baixados NÃO são apagados.`)) return;
      const r = await api(`/secretarias/${btn.dataset.delId}`, { method: "DELETE" });
      toast(`${r.excluida} excluída (${r.documentos_removidos} registros removidos)`);
      loadSecretarias(); loadJobs(); loadDocuments();
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
        pdf_filtro: $("sec-filtro").value.trim() === "" ? null : $("sec-filtro").value.trim(),
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
    $("sec-sigla").value = $("sec-url").value = $("sec-depth").value = $("sec-filtro").value = "";
    $("sec-run").checked = false;
    loadSecretarias();
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------- Fonte de dados genérica ----------

let colEditandoId = null;

function colConfig() {
  const modo = $("col-modo").value;
  return {
    url: $("col-url").value.trim(),
    modo,
    caminho: $("col-caminho").value.trim(),
    max_paginas: parseInt($("col-max").value, 10) || 1,
    pagina_inicial: Number.isNaN(parseInt($("col-ini").value, 10)) ? 1 : parseInt($("col-ini").value, 10),
    renderizar: modo === "tabela_html" && $("col-render").checked,
    cabecalhos: $("col-cab").value.trim(),
  };
}

function colAjustaModo() {
  const html = $("col-modo").value === "tabela_html";
  $("col-render-label").hidden = !html;
  $("col-caminho").placeholder = html
    ? "tabela (opcional) — seletor CSS, ex.: table.lista · vazio = a maior tabela da página"
    : "onde estão os dados (opcional) — ex.: dados.itens · vazio = acha sozinho";
}
$("col-modo").addEventListener("change", colAjustaModo);
colAjustaModo();

function colLimpa() {
  colEditandoId = null;
  $("col-form").reset();
  $("col-sigla").disabled = false;
  $("col-cancelar-edicao").hidden = true;
  $("col-estado").textContent = "";
  $("col-preview").innerHTML = "";
  colAjustaModo();
}
$("col-cancelar-edicao").addEventListener("click", colLimpa);

function editarColeta(id) {
  const s = secretariasCache.find((x) => x.id === id);
  if (!s || !s.coleta) return;
  const c = s.coleta;
  colEditandoId = id;
  $("col-sigla").value = s.sigla;
  $("col-sigla").disabled = true;
  $("col-modo").value = c.modo;
  $("col-url").value = c.url;
  $("col-caminho").value = c.caminho || "";
  $("col-max").value = c.max_paginas;
  $("col-ini").value = c.pagina_inicial;
  $("col-render").checked = !!c.renderizar;
  $("col-cab").value = c.cabecalhos || "";
  $("col-cancelar-edicao").hidden = false;
  $("col-estado").textContent = `editando ${s.sigla}`;
  colAjustaModo();
  $("col-form").scrollIntoView({ behavior: "smooth", block: "center" });
}

const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

$("col-testar").addEventListener("click", async () => {
  if (!$("col-url").value.trim()) return toast("Informe a URL", true);
  $("col-testar").disabled = true;
  $("col-preview").innerHTML = "buscando a primeira página…";
  try {
    const r = await api("/coletas/testar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(colConfig()),
    });
    if (!r.total_na_pagina) {
      $("col-preview").innerHTML = "⚠ O site respondeu, mas não encontrei nenhum registro. Ajuste o campo “onde estão os dados”.";
    } else {
      $("col-preview").innerHTML =
        `<div>✅ ${r.total_na_pagina} registro(s) na 1ª página, ${r.colunas.length} coluna(s). Prévia:</div>` +
        `<div class="prev-wrap"><table><thead><tr>${r.colunas.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
        r.linhas.map((l) => `<tr>${r.colunas.map((c) => `<td title="${esc(l[c])}">${esc(l[c])}</td>`).join("")}</tr>`).join("") +
        "</tbody></table></div>";
    }
  } catch (err) {
    $("col-preview").textContent = "";
    toast(err.message, true);
  } finally {
    $("col-testar").disabled = false;
  }
});

$("col-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("col-salvar").disabled = true;
  try {
    const cfg = colConfig();
    let sec;
    if (colEditandoId) {
      sec = await api(`/coletas/${colEditandoId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      toast("Fonte atualizada");
    } else {
      sec = await api("/coletas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...cfg, sigla: $("col-sigla").value }),
      });
      toast("Fonte cadastrada");
    }
    if ($("col-run").checked) {
      const r = await api("/jobs/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secretarias: [sec.sigla], force_full_scrape: false, max_depth: null }),
      });
      toast(`Coleta de ${sec.sigla} iniciada`);
      loadJobs(); loadOverview();
    }
    colLimpa();
    loadSecretarias();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("col-salvar").disabled = false;
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
            `<td>${j.pdfs_falhas}</td><td class="mono">${fmtDate(j.started_at)}</td>` +
            `<td class="nowrap">${
              j.status === "RUNNING"
                ? `<button class="mini" data-cancel="${j.job_id}" title="Interrompe a tarefa (o que já foi baixado fica)">cancelar</button> `
                : ""
            }${
              j.pdfs_baixados_sucesso > 0
                ? `<a class="mini" href="${API}/jobs/${j.job_id}/download" title="Baixa todos os arquivos desta tarefa em um .zip">⬇ zip</a>`
                : ""
            }</td></tr>`
        )
        .join("")
    : '<tr><td colspan="9" class="empty">nenhuma tarefa ainda</td></tr>';
}

$("jobs-tbody").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-cancel]");
  if (!b) return;
  if (!confirm("Cancelar esta tarefa? O que já foi baixado é mantido.")) return;
  b.disabled = true;
  try {
    const r = await api(`/jobs/${b.dataset.cancel}/cancel`, { method: "POST" });
    toast(`Tarefa cancelada (${r.pendentes_removidos} pendente(s) descartado(s))`);
    loadJobs(); loadOverview(); loadProgressoTarefas();
  } catch (err) {
    toast(err.message, true);
    b.disabled = false;
  }
});

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
                ? `<a href="${API}/documents/${i.id}/file" target="_blank" title="${i.minio_path}">${i.minio_path}</a> <a class="mini" href="${API}/documents/${i.id}/file?download=true" title="Baixar arquivo">⬇</a>`
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
          ? `<button class="mini" data-cancel="${t.job_id}" title="Interrompe a tarefa">cancelar</button>`
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

$("prog-tarefas-lista").addEventListener("click", async (e) => {
  const c = e.target.closest("button[data-cancel]");
  if (c) {
    if (!confirm("Cancelar esta tarefa? O que já foi baixado é mantido.")) return;
    try {
      const r = await api(`/jobs/${c.dataset.cancel}/cancel`, { method: "POST" });
      toast(`Tarefa cancelada (${r.pendentes_removidos} pendente(s) descartado(s))`);
      loadJobs(); loadOverview(); loadProgressoTarefas();
    } catch (err) { toast(err.message, true); }
    return;
  }
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
