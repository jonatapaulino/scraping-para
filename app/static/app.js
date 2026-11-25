const API = "/api/v1";
let docPage = 1, docPages = 1;
let secretariasCache = [];
let secsLoaded = false, folhaOpcoesLoaded = false;

const $ = (id) => document.getElementById(id);
// Escapa texto vindo de fora (URLs e nomes raspados, rótulos das APIs) antes de
// entrar em innerHTML — sem isso um link malicioso numa página raspada virava
// código rodando dentro do painel.
const esc = (t) =>
  String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (s) => (s ? new Date(s).toLocaleString("pt-BR") : "—");
const fmtBytes = (n) => {
  if (n == null) return "—";
  const u = [["TB", 1e12], ["GB", 1e9], ["MB", 1e6], ["KB", 1e3]].find(([, v]) => n >= v);
  return u ? (n / u[1]).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " " + u[0] : n + " B";
};
const ROTULOS = { RUNNING: "Em andamento", COMPLETED: "Concluído", FAILED: "Falhou", CANCELLED: "Cancelado", SUCCESS: "Sucesso", PENDING: "Pendente", SEM_DADOS: "Sem dados", DESCARTADO: "Descartado" };
const badge = (st) => `<span class="badge ${esc(st)}">${esc(ROTULOS[st] || st)}</span>`;
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

const num = (n) => (n == null || n < 0 ? "–" : Number(n).toLocaleString("pt-BR"));

async function loadOverview() {
  try {
    const r = await api("/resumo");
    $("versao").textContent = r.versao && r.versao !== "dev" ? "v" + r.versao : "versão local";
    $("api-status").textContent = "API conectada";
    $("api-status").className = "pill on";

    // Coleta agora: o que esta rodando e em que ponto
    const ag = r.agora, f = r.fila;
    const titulo = $("agora-titulo");
    titulo.classList.remove("ok");
    $("agora-barra").hidden = true;
    if (ag.length) {
      const a = ag.find((x) => x.nome === f.atual) || ag[0];
      titulo.textContent = a.nome + (ag.length > 1 ? `  (+${ag.length - 1} tarefa${ag.length > 2 ? "s" : ""})` : "");
      $("agora-detalhe").textContent = a.total > 1
        ? `${num(a.feitos)} de ${num(a.total)} partes · ${a.percentual ?? 0}%`
        : "em andamento — parte única grande (pode levar um tempo sem a barra andar)";
      if (a.total > 1) {
        $("agora-barra").hidden = false;
        $("agora-barra-fill").style.width = (a.percentual ?? 0) + "%";
      }
    } else if (f.aguardando) {
      titulo.textContent = "Iniciando a próxima coleta da fila…";
      $("agora-detalhe").textContent = "começa em até 1 minuto";
    } else {
      titulo.textContent = "✅ Nada rodando — tudo concluído";
      titulo.classList.add("ok");
      $("agora-detalhe").textContent = "pode pedir uma nova coleta";
    }

    // Fila de coletas
    if (!f.total) {
      $("fila-resumo").textContent = "vazia";
      $("fila-detalhe").textContent = "use “Pôr na fila” em Dados abertos";
    } else if (f.posicao_atual) {
      $("fila-resumo").textContent = `${f.posicao_atual}ª de ${f.total}`;
      $("fila-detalhe").textContent = `${f.terminadas} concluída(s) · ${f.aguardando} esperando` + (f.falharam ? ` · ${f.falharam} com falha` : "");
    } else {
      $("fila-resumo").textContent = f.aguardando ? `${f.aguardando} esperando` : `${f.terminadas} de ${f.total} ✓`;
      $("fila-detalhe").textContent = f.aguardando ? "" : "fila terminada" + (f.falharam ? ` · ${f.falharam} com falha` : "");
    }

    // Arquivos guardados e espaco
    const g = r.guardado;
    $("guardado-n").textContent = num(g.arquivos);
    $("guardado-tam").textContent = g.bytes == null ? "" : fmtBytes(g.bytes);
    $("espaco-livre").textContent = g.livre_bytes == null ? "–" : fmtBytes(g.livre_bytes);

    $("docs-24h").textContent = `${num(r.ultimas_24h.baixados)} baixados · ${num(r.ultimas_24h.falhas)} falhas`;
    $("docs-fail").textContent = num(r.falhas.total);
    const naFila = r.falhas.na_fila_de_erros;
    $("falhas-detalhe").textContent = naFila > 0 ? `${num(naFila)} esperando nova tentativa` : "";
    $("btn-falhas").hidden = !(r.falhas.total > 0 || naFila > 0);
    if (Date.now() - versaoVistaEm > 10 * 60 * 1000) verificaVersao(r.versao);

    // detalhes tecnicos
    const t = r.tecnico;
    $("q-discovery").textContent = num(t.fila_descoberta);
    $("q-download").textContent = num(t.fila_download);
    $("q-dead").textContent = num(t.fila_erros);
    $("jobs-running").textContent = num(t.tarefas_rodando);
    $("sem-dados").textContent = num(r.sem_dados);
  } catch (e) {
    $("api-status").textContent = "API desconectada";
    $("api-status").className = "pill off";
  }
}

// ---------- Versao: avisa quando ha uma nova publicada ----------
let versaoVistaEm = 0;
async function verificaVersao(atual) {
  versaoVistaEm = Date.now();
  try {
    const v = await api("/versao");
    const el = $("versao");
    if (v.nova_disponivel) {
      el.outerHTML = `<a id="versao" class="pill nova" href="${esc(v.url)}" target="_blank" rel="noopener"
        title="Versão nova publicada. Para instalar, peça ao administrador do servidor: ./deploy/atualizar.sh">v${esc(v.atual)} → v${esc(v.ultima)} disponível</a>`;
    }
  } catch (e) { /* sem internet no servidor: segue sem o aviso */ }
}

// ---------- Falhas: ver o motivo, tentar de novo, descartar ----------
const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

async function loadFalhas() {
  const f = await api("/falhas");
  $("falhas-resumo").textContent = !f.total
    ? "Nenhuma falha. ✅"
    : `${num(f.total)} falha(s): ${num(f.refaziveis)} podem dar certo tentando de novo · ${num(f.permanentes)} são link quebrado ou não-PDF (não adianta tentar).`;
  $("falhas-refazer").disabled = !f.refaziveis && !(f.fila_erros > 0);
  const linhaPaginas = f.fila_erros > 0
    ? `<tr><td class="nowrap"><b>Portais</b></td><td>Páginas de portal que não responderam (esgotaram as tentativas automáticas)</td>` +
      `<td class="c" style="text-align:right">${num(f.fila_erros)}</td><td></td>` +
      `<td class="nowrap"><button class="mini" data-acao="paginas-refazer">tentar de novo</button> ` +
      `<button class="mini" data-acao="paginas-descartar">descartar</button></td></tr>`
    : "";
  $("falhas-descartar-perm").disabled = !f.permanentes;
  $("falhas-tbody").innerHTML = f.grupos.length ? f.grupos.map((g) => {
    const ex = g.exemplos[0];
    const dados = `data-fonte="${esc(g.fonte)}" data-motivo="${esc(g.motivo)}" data-perm="${g.permanente}"`;
    return `<tr><td class="nowrap"><b>${esc(g.fonte_nome)}</b></td>` +
      `<td>${esc(g.motivo_texto)}${g.permanente ? ' <span class="tag-perm">(não adianta tentar)</span>' : ""}</td>` +
      `<td class="c" style="text-align:right">${num(g.quantidade)}</td>` +
      `<td class="url" title="${esc(ex ? ex.url : "")}">${ex ? esc(ex.url) : ""}</td>` +
      `<td class="nowrap"><button class="mini" data-acao="refazer" ${dados}>tentar de novo</button> ` +
      `<button class="mini" data-acao="descartar" ${dados}>descartar</button></td></tr>`;
  }).join("") + linhaPaginas : (linhaPaginas || '<tr><td colspan="5" class="empty">sem falhas</td></tr>');
}

function abreFalhas() {
  $("falhas-box").hidden = false;
  loadFalhas().catch((e) => toast(e.message, true));
  $("falhas-box").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function refazerFalhas(corpo, descricao) {
  try {
    const r = await postJson("/falhas/refazer", corpo);
    toast(r.refeitos
      ? `${r.refeitos} ${descricao} em nova tentativa — acompanhe em “Coleta agora”`
      : "Nada para tentar de novo" + (r.paginas_reenviadas ? ` (${r.paginas_reenviadas} página(s) de portal reenviada(s))` : ""));
    loadFalhas(); loadOverview(); loadProgressoTarefas();
  } catch (err) { toast(err.message, true); }
}

async function descartarFalhas(corpo, pergunta) {
  if (!confirm(pergunta + "\n\nElas saem da lista e o sistema não tenta mais. (O registro fica guardado para o sistema não baixar o mesmo link quebrado de novo.)")) return;
  try {
    const r = await postJson("/falhas/descartar", corpo);
    toast(`${r.descartadas} falha(s) descartada(s)`);
    loadFalhas(); loadOverview();
  } catch (err) { toast(err.message, true); }
}

$("btn-falhas").addEventListener("click", abreFalhas);
$("falhas-fechar").addEventListener("click", () => { $("falhas-box").hidden = true; });
$("falhas-refazer").addEventListener("click", () => refazerFalhas({}, "falha(s)"));
$("falhas-descartar-perm").addEventListener("click", () =>
  descartarFalhas({ so_permanentes: true }, "Descartar todos os links quebrados e arquivos que não são PDF?"));
$("falhas-tbody").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-acao]");
  if (!b) return;
  if (b.dataset.acao === "paginas-refazer") return refazerFalhas({ fonte: "__nenhuma__" }, "item(ns)");
  if (b.dataset.acao === "paginas-descartar") {
    if (!confirm("Descartar as páginas de portal que não responderam?\n\nO sistema para de tentá-las agora; elas voltam a ser visitadas na próxima varredura do portal.")) return;
    postJson("/falhas/descartar-paginas").then((r) => { toast(`${r.descartadas} página(s) descartada(s)`); loadFalhas(); loadOverview(); })
      .catch((err) => toast(err.message, true));
    return;
  }
  const corpo = { fonte: b.dataset.fonte, motivo: b.dataset.motivo, incluir_permanentes: b.dataset.perm === "true" };
  if (b.dataset.acao === "refazer") refazerFalhas(corpo, "item(ns)");
  else descartarFalhas(corpo, `Descartar estas falhas de ${b.closest("tr").querySelector("b").textContent}?`);
});

// ---------- Secretarias ----------

// profundidade em palavras (0 = só a página cadastrada)
function rotuloProfundidade(n) {
  if (n === null || n === undefined) return "padrão";
  if (n === 0) return "só a página";
  return n === 1 ? "1 nível" : `${n} níveis`;
}

async function loadSecretarias() {
  secretariasCache = await api("/secretarias");
  $("secretaria-checks").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<label title="${esc(s.tipo === 'crawl' || s.tipo === 'coleta' ? s.url_base : (s.sigla === 'IOEPA' ? 'Diário Oficial do Estado — escolha os anos ao lado' : 'fonte de API — use a seção Folha'))}"><input type="checkbox" value="${esc(s.sigla)}" ${
              s.is_ativa && (s.tipo === "crawl" || s.tipo === "coleta" || s.sigla === "IOEPA") ? "" : "disabled"
            }> ${esc(s.sigla)}${s.is_ativa ? "" : " (inativa)"}${s.tipo === "crawl" ? "" : s.tipo === "coleta" ? " (coleta)" : s.sigla === "IOEPA" ? " (Diário Oficial)" : " (API)"}</label>`
        )
        .join("")
    : '<span class="empty">nenhuma cadastrada</span>';
  $("secs-tbody").innerHTML = secretariasCache.length
    ? secretariasCache
        .map(
          (s) =>
            `<tr class="${s.is_ativa ? "" : "inativa"}"><td class="nowrap"><b>${esc(s.sigla)}</b></td>` +
            `<td class="c nowrap"><span class="badge ${s.tipo === "crawl" ? "PORTAL" : s.tipo === "coleta" ? "COLETA" : "API"}" title="${
              s.tipo === "crawl" ? "Portal: baixa os PDFs do site" : s.tipo === "coleta" ? "Coleta: API ou tabela, gera CSV"
              : s.sigla === "IOEPA" ? "Diário Oficial do Estado" : "Fonte de API (dados abertos)"
            }">${s.tipo === "crawl" ? "Portal" : s.tipo === "coleta" ? "Coleta" : s.sigla === "IOEPA" ? "Diário" : "API"}</span></td>` +
            `<td class="url" title="${esc(s.url_base)}">${esc(s.url_base)}${
              s.tipo === "crawl" ? ` <button class="mini" data-url-id="${esc(s.id)}" data-url="${esc(s.url_base)}" title="Editar URL">✎</button>`
              : s.tipo === "coleta" ? ` <button class="mini" data-coleta-id="${s.id}" title="Editar a configuração desta fonte (abre o formulário acima)">✎ config</button>` : ""
            }</td>` +
            `<td class="c nowrap">${s.tipo === "crawl" ? `${rotuloProfundidade(s.max_depth)} <button class="mini" data-depth-id="${s.id}" data-depth="${s.max_depth ?? ""}" title="Editar profundidade">✎</button>` : '<span class="muted">—</span>'}</td>` +
            `<td class="c nowrap">${
              s.tipo === "coleta" ? "CSV" : s.tipo !== "crawl" ? '<span class="muted">—</span>' :
              `${s.pdf_filtro === null ? "padrão" : s.pdf_filtro === "" ? "todos" : `<code title="${esc(s.pdf_filtro)}">regex</code>`} <button class="mini" data-filtro-id="${esc(s.id)}" data-filtro="${esc(s.pdf_filtro ?? "")}" data-filtro-null="${s.pdf_filtro === null}" title="Editar filtro de PDFs">✎</button>`
            }</td>` +
            `<td class="nowrap">${
              s.tipo === "crawl" || s.tipo === "coleta" || s.sigla === "IOEPA"
                ? `<input type="checkbox" data-agenda-id="${s.id}" ${s.agendada ? "checked" : ""} title="Raspar todo dia"> ` +
                  `<input type="time" data-hora-id="${s.id}" value="${esc(s.agenda_hora || "06:00")}" ${s.agendada ? "" : "disabled"} style="width:6.5rem">`
                : '<span class="muted">—</span>'
            }</td>` +
            `<td class="c nowrap"><span class="badge ${s.is_ativa ? "COMPLETED" : "CANCELLED"}">${s.is_ativa ? "Ativa" : "Inativa"}</span></td>` +
            `<td class="nowrap"><button class="mini" data-toggle-id="${s.id}" data-ativa="${s.is_ativa}">${
              s.is_ativa ? "desativar" : "reativar"
            }</button>${
              s.tipo === "crawl" || s.tipo === "coleta" ? ` <button class="mini" data-del-id="${s.id}" data-sigla="${esc(s.sigla)}" title="Excluir fonte e histórico (os arquivos baixados ficam)">excluir</button>` : ""
            }</td></tr>`
        )
        .join("")
    : '<tr><td colspan="8" class="empty">vazio</td></tr>';
  const sel = $("f-sec");
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">Todas secretarias</option>' +
    secretariasCache.map((s) => `<option value="${esc(s.id)}">${esc(s.sigla)}</option>`).join("");
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
      const v = prompt("Profundidade: quantos cliques o robô anda a partir da URL cadastrada até achar os PDFs.\n• 0 = só a própria página\n• 1 = ela e as páginas que ela linka\n• 2, 3… = mais níveis (mais lento)\n• vazio = padrão do sistema", cur);
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
            `<td class="nowrap">${esc(j.secretaria)}</td><td class="nowrap">${badge(j.status)}</td>` +
            `<td>${j.pdfs_descobertos > 0 ? barra(Math.min(100, Math.round(((j.pdfs_baixados_sucesso + j.pdfs_falhas + (j.sem_dados || 0)) * 100) / j.pdfs_descobertos)), j.status === "COMPLETED") : "–"}</td>` +
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
  const item = (valor, rotulo, titulo, disponivel, marcado) =>
    `<label class="${disponivel ? "" : "indisp"}" title="${esc(titulo)}">` +
    `<input type="checkbox" data-chave="${esc(rotulo)}" value="${esc(valor)}" ` +
    `${disponivel ? "" : 'disabled data-indisp="1"'} ${marcado ? "checked" : ""}> ${esc(rotulo)}</label>`;
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

// periodo escolhido pela pessoa (de/até) — sobrevive a troca de dataset e às recargas
const periodoEscolhido = { de: null, ate: null };
$("folha-de").addEventListener("change", () => { periodoEscolhido.de = $("folha-de").value; });
$("folha-ate").addEventListener("change", () => { periodoEscolhido.ate = $("folha-ate").value; });

// Reaplica o valor escolhido; se o dataset novo for anual (sem o mes), usa o
// mesmo ano: o primeiro mes/ano para "de", o ultimo para "até"
function restauraPeriodo(sel, valor, qual) {
  if (!valor) return;
  const ops = [...sel.options].map((o) => o.value);
  if (ops.includes(valor)) { sel.value = valor; return; }
  const ano = valor.split("-")[0];
  const doAno = ops.filter((v) => v.split("-")[0] === ano);
  if (doAno.length) sel.value = qual === "primeiro" ? doAno[0] : doAno[doAno.length - 1];
}

const rotuloPeriodo = (v) => {
  const [a, m] = (v || "").split("-");
  return m ? `${a}-${m.padStart(2, "0")}` : a;
};

// Texto da confirmacao: mostra exatamente o que vai ser pedido
function resumoColeta(acao, orgaos) {
  const ds = $("folha-dataset");
  const semPeriodo = $("folha-de").disabled;
  const per = semPeriodo ? "sem período (inventário atual)"
    : `${rotuloPeriodo($("folha-de").value)} até ${rotuloPeriodo($("folha-ate").value)}`;
  const orgs = orgaos ? `${orgaos.length} órgão(s): ${orgaos.slice(0, 8).join(", ")}${orgaos.length > 8 ? "…" : ""}` : "todos os órgãos";
  return `${acao}?\n\n• Dataset: ${ds.options[ds.selectedIndex].text}\n• Período: ${per}\n• Órgãos: ${orgs}` +
    `${$("folha-force").checked ? "\n• Recoletar: sim (consulta tudo de novo)" : ""}`;
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
        return `<option value="${esc(p.ano)}-${esc(mes)}">${esc(rotulo)}</option>`;
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
      // ...mas o periodo que VOCE escolheu tem prioridade: antes, trocar o dataset
      // (ou a recarga automatica) voltava tudo para o ano corrente sem avisar
      restauraPeriodo($("folha-de"), periodoEscolhido.de, "primeiro");
      restauraPeriodo($("folha-ate"), periodoEscolhido.ate, "ultimo");
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
  if (!confirm(resumoColeta("Coletar agora", orgaos))) return;
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

// ---------- Fila automática (uma coleta por vez) ----------

const FILA_ROTULO = {
  AGUARDANDO: ["Na fila", "PENDING"], RODANDO: ["Rodando", "RUNNING"], CONCLUIDA: ["Concluída", "COMPLETED"],
  FALHOU: ["Falhou", "FAILED"], CANCELADA: ["Cancelada", "CANCELLED"],
};

async function loadFila() {
  const itens = await api("/fila");
  $("fila-box").hidden = !itens.length;
  let pos = 0;
  $("fila-tbody").innerHTML = itens
    .map((i) => {
      const [rot, cls] = FILA_ROTULO[i.status] || [i.status, ""];
      const ordem = i.status === "AGUARDANDO" ? `${++pos}º` : "";
      return `<tr><td class="nowrap">${esc(ordem)}</td><td>${esc(i.nome)}${i.recoletar ? ' <span class="hint">(recoletar)</span>' : ""}</td>` +
        `<td class="nowrap">${esc(i.periodo)}</td><td>${esc(i.orgaos)}</td>` +
        `<td class="nowrap"><span class="badge ${cls}" title="${esc(i.erro || "")}">${esc(rot)}</span></td>` +
        `<td>${i.status === "RODANDO" ? '<span class="hint">veja o progresso no topo</span>'
          : `<button class="mini" data-fila-del="${esc(i.id)}" title="Tirar da fila">×</button>`}</td></tr>`;
    })
    .join("");
}

$("btn-fila").addEventListener("click", async () => {
  const todos = $("folha-todos").checked;
  const orgaos = todos
    ? null
    : [...document.querySelectorAll("#folha-orgaos-checks input:checked")].map((c) => c.value);
  if (!todos && !orgaos.length) return toast("Selecione ao menos um órgão (ou marque todos)", true);
  const [anoIni, mesIni] = $("folha-de").value.split("-").map(Number);
  const [anoFim, mesFim] = $("folha-ate").value.split("-").map(Number);
  if (!confirm(resumoColeta("Pôr na fila (uma por vez)", orgaos))) return;
  $("btn-fila").disabled = true;
  try {
    const r = await api("/fila", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset: $("folha-dataset").value,
        ano_ini: anoIni, mes_ini: mesIni, ano_fim: anoFim, mes_fim: mesFim,
        orgaos, force: $("folha-force").checked,
      }),
    });
    toast(`${r.adicionados} coleta(s) na fila — rodam uma de cada vez, sozinhas`);
    loadFila();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("btn-fila").disabled = false;
  }
});

$("fila-tbody").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-fila-del]");
  if (!b) return;
  try {
    await api(`/fila/${b.dataset.filaDel}`, { method: "DELETE" });
    loadFila();
  } catch (err) { toast(err.message, true); }
});

$("fila-esvaziar").addEventListener("click", async () => {
  if (!confirm("Tirar da fila tudo o que ainda não começou?\n(A coleta que está rodando continua — para ela, use cancelar no progresso.)")) return;
  try {
    const r = await api("/fila/esvaziar", { method: "POST" });
    toast(`${r.removidos} coleta(s) tirada(s) da fila`);
    loadFila();
  } catch (err) { toast(err.message, true); }
});

$("fila-limpar").addEventListener("click", async () => {
  try {
    const r = await api("/fila/limpar", { method: "POST" });
    toast(`${r.removidos} item(ns) removido(s) da lista`);
    loadFila();
  } catch (err) { toast(err.message, true); }
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
            `<tr><td class="url" title="${esc(i.url_origem)}">${esc(i.url_origem)}</td>` +
            `<td class="nowrap">${badge(i.status_download)}</td>` +
            `<td>${i.magic_byte_valido === null ? "—" : i.magic_byte_valido ? "✅" : "❌"}</td>` +
            `<td>${fmtBytes(i.tamanho_bytes)}</td>` +
            `<td class="mono" title="${esc(i.hash_sha256)}">${i.hash_sha256 ? esc(i.hash_sha256.slice(0, 10)) + "…" : "—"}</td>` +
            `<td class="mono">${
              i.minio_path
                ? `<a href="${API}/documents/${esc(i.id)}/file" target="_blank" rel="noopener" title="${esc(i.minio_path)}">${esc(i.minio_path)}</a> <a class="mini" href="${API}/documents/${esc(i.id)}/file?download=true" title="Baixar arquivo">⬇</a>`
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
        const falhas = (t.falhas ? ` · ${t.falhas} falha(s)` : "") + (t.sem_dados ? ` · ${t.sem_dados} sem dados` : "");
        const quando = fmtHora(t.concluida_em);
        const texto = feito
          ? `✓ Download concluído${quando ? " em " + quando : ""} — ${t.texto}${falhas}`
          : falhou
            ? `✗ Interrompida${quando ? " em " + quando : ""} — ${t.texto}${falhas}`
            : `${t.texto}${falhas}`;
        const fechar = t.situacao === "RUNNING"
          ? `<button class="mini" data-cancel="${esc(t.job_id)}" title="Interrompe a tarefa">cancelar</button>`
          : `<button class="mini" data-dispensar="${esc(t.job_id)}" title="Remover da lista">×</button>`;
        const pct = feito && t.percentual == null ? 100 : t.percentual;
        return `<div class="prog-item"><div class="prog-resumo"><span><b>${esc(t.fonte)}</b> <span class="hint">${esc(t.descricao)}</span></span>` +
          `<span class="nowrap">${badge(t.situacao)} ${fechar}</span></div>` +
          `${pct == null ? "" : barra(Number(pct) || 0, feito)}<div class="hint">${esc(texto)}</div></div>`;
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
  loadFila().catch(() => {});
  loadDrive();
  // self-healing: o que falhar no boot (API ainda subindo) retenta aqui
  // ate conseguir uma vez — depois nao re-entra p/ nao apagar a selecao
  if (!secsLoaded) loadSecretarias().catch(() => {});
  if (!folhaOpcoesLoaded) loadFolhaOpcoes();
}

loadSecretarias().catch(() => {}).then(refreshAll);
loadFolhaOpcoes();
// aba em segundo plano nao consulta (cada aba aberta multiplicava a carga);
// ao voltar para a aba, atualiza na hora
setInterval(() => { if ($("autorefresh").checked && !document.hidden) refreshAll(); }, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && $("autorefresh").checked) refreshAll();
});

// ---------- Google Drive: enviar os arquivos guardados ----------
let driveVistoEm = 0;
async function loadDrive(forcar) {
  if (!forcar && Date.now() - driveVistoEm < 30000) return;
  driveVistoEm = Date.now();
  try {
    const d = await api("/drive");
    const ult = d.ultimo;
    let estado = d.conectado ? "✅ Conta Google conectada." : "⚠ Conta Google ainda não conectada — abra “Conectar conta Google” abaixo.";
    if (ult) estado += ` Último envio: ${ult.situacao === "RUNNING" ? "em andamento" : ROTULOS[ult.situacao] || ult.situacao} — ${ult.texto}`;
    $("drive-estado").textContent = estado;
    $("drive-enviar").disabled = !d.conectado || (ult && ult.situacao === "RUNNING");
    $("drive-desconectar").hidden = !d.conectado;
    if (!d.conectado) $("drive-conexao").open = true;
    const sel = $("drive-origem"), atual = sel.value;
    sel.innerHTML = '<option value="">todos os arquivos</option>' +
      d.pastas.map((p) => `<option value="${esc(p)}">só a pasta ${esc(p)}</option>`).join("");
    sel.value = d.pastas.includes(atual) ? atual : "";
  } catch (e) {
    $("drive-estado").textContent = "Não consegui consultar o Drive agora.";
  }
}

(function () {
  try { $("drive-link").value = localStorage.getItem("driveLink") || ""; } catch (e) {}
})();

$("drive-enviar").addEventListener("click", async () => {
  const link = $("drive-link").value.trim();
  if (!link) return toast("Cole o link da pasta do Drive", true);
  const sub = $("drive-origem").value;
  if (!confirm(`Enviar ${sub ? "a pasta " + sub : "todos os arquivos guardados"} para esta pasta do Drive?\n\n${link}\n\nSó vai o que ainda não estiver lá. O andamento aparece em “Coleta agora”.`)) return;
  $("drive-enviar").disabled = true;
  try {
    await api("/drive/enviar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ link, subpasta: sub }) });
    try { localStorage.setItem("driveLink", link); } catch (e) {}
    toast("Envio para o Drive iniciado — acompanhe em “Coleta agora”");
    loadDrive(true); loadOverview(); loadProgressoTarefas();
  } catch (err) {
    toast(err.message, true);
    $("drive-enviar").disabled = false;
  }
});

$("drive-conectar").addEventListener("click", async () => {
  const token = $("drive-token").value.trim();
  if (!token) return toast("Cole o código do rclone authorize", true);
  try {
    await api("/drive/conectar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
    $("drive-token").value = "";
    $("drive-conexao").open = false;
    toast("Conta Google conectada ✅");
    loadDrive(true);
  } catch (err) { toast(err.message, true); }
});

$("drive-desconectar").addEventListener("click", async () => {
  if (!confirm("Desconectar a conta Google? Os arquivos que já estão no Drive continuam lá.")) return;
  try { await api("/drive/desconectar", { method: "POST" }); toast("Conta desconectada"); loadDrive(true); }
  catch (err) { toast(err.message, true); }
});
