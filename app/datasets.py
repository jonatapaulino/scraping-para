"""Clientes dos datasets adicionais do Portal da Transparencia do Para.

Padrao observado nas APIs tematicas (Spring Boot):
  - endpoints de dados abertos devolvem JSON {"data": [...]} ou lista plana
  - /exportar tem captcha e nao e necessario — os dados vem paginados ou em
    um unico GET por competencia
  - params de pagina quando existem: pagina + quantidade (ou qtdRegistros)
Cada dataset expoe: list_periods(), list_orgaos(ano, mes), iter_rows(recorte, siglas).
`recorte` = {"ano","mes"} | {"ano"} | {"ano_ini","mes_ini","ano_fim","mes_fim"} | {}.
Sigla e a chave de filtro de orgao — onde a API nao tem sigla no row,
filtramos pelo nome completo do orgao."""

import json
import logging
import re
import time
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import quote

from app import config, http_client
from app.folha import _cached

log = logging.getLogger(__name__)

PAGE_SIZE = 1000


def _get_json(url, params=None):
    with http_client.new_client() as c:
        resp = c.get(url, params={k: v for k, v in (params or {}).items() if v is not None})
        resp.raise_for_status()
        return resp.json()


def _data(body) -> list:
    """Normaliza {'data': [...]} e lista plana."""
    if isinstance(body, list):
        return body
    return body.get("data") or []


def _paged(url, params=None, size: int = PAGE_SIZE,
           delay: Optional[float] = None,
           size_key: str = "quantidade") -> Iterator[dict]:
    """Pagina com pagina+size_key. Para em: pagina vazia/curta, total
    'qtd_registros' atingido, ou pagina repetida (API que ignora pagina
    e devolve sempre tudo — ex.: transferencia-recursos, suprimento)."""
    page = 1
    got = 0
    total = None
    primeira = None
    with http_client.new_client() as c:
        while True:
            p = {"pagina": page, size_key: size, **(params or {})}
            resp = c.get(url, params=p)
            resp.raise_for_status()
            body = resp.json()
            rows = _data(body)
            if total is None and isinstance(body, dict):
                total = body.get("qtd_registros")
            if not rows:
                return
            if page == 1:
                primeira = rows[0]
            elif rows[0] == primeira:
                return
            yield from rows
            got += len(rows)
            if len(rows) < size or (total is not None and got >= int(total)):
                return
            page += 1
            if delay:
                time.sleep(delay)


def _month_key(row, *fields):
    """'dd/mm/aaaa' -> (ano, mes). Retorna None se nao parsear."""
    for f in fields:
        v = row.get(f)
        if isinstance(v, str) and "/" in v:
            try:
                d, m, a = v.split("/")
                return int(a), int(m)
            except ValueError:
                continue
    return None


def _sel(row, field, siglas) -> bool:
    """True se o orgao do row (sigla ou nome) esta na selecao."""
    if not siglas:
        return True
    return str(row.get(field) or "").strip().upper() in {s.upper() for s in siglas}


def _periodos_mes(lista):
    return sorted(
        ({"ano": int(p["ano"]), "mes": int(p["mes"]),
          "mes_extenso": p.get("mes_extenso")}
         for p in lista),
        key=lambda p: (p["ano"], p["mes"]),
    )


def _periodos_ano(lista):
    return [{"ano": int(a)} for a in sorted(lista)]


_SIGLA_KEYS = ("sigla", "sigla_orgao", "siglaOrgao")
_NOME_KEYS = ("orgao", "nome", "nome_orgao", "nomeOrgao", "descricao")


def _first(g, keys):
    for k in keys:
        v = g.get(k)
        if v:
            return str(v).strip()
    return None


def _orgaos_padrao(lista, sigla_key=None, nome_key=None, pref_nome=False):
    """Normaliza lista de orgaos para [{'sigla','orgao'}]. O campo 'sigla'
    e o rotulo de filtro — sigla da API quando existe; pref_nome=True usa
    o nome completo (datasets cujo row carrega o nome, nao a sigla)."""
    out = []
    for g in lista:
        sigla = (g.get(sigla_key) if sigla_key else None) or _first(g, _SIGLA_KEYS)
        nome = (g.get(nome_key) if nome_key else None) or _first(g, _NOME_KEYS)
        label = (nome if pref_nome else sigla) or nome or sigla
        if label:
            out.append({"sigla": label, "orgao": nome or label})
    return out


# ============ EMPENHOS (api-notas-empenho) ============
# GET /notas-empenho?ano&mes devolve o MES INTEIRO num so JSON (~28k linhas).
EMP_BASE = config.EMPENHOS_API_BASE


def empenhos_periods():
    def _get():
        return _periodos_mes(_data(_get_json(f"{EMP_BASE}/notas-empenho/ano-mes")))
    return _cached("emp:periods", _get)


def empenhos_orgaos(ano, mes):
    def _get():
        return _orgaos_padrao(_data(_get_json(f"{EMP_BASE}/filtros/orgaos-empenho", {"ano": ano})))
    return _cached(f"emp:orgaos:{ano}", _get)


def empenhos_iter(recorte, siglas):
    # 'pagina' e obrigatorio na spec; 'mes' nao consta mas filtra de fato
    body = _get_json(
        f"{EMP_BASE}/notas-empenho",
        {"ano": recorte["ano"], "mes": recorte["mes"], "pagina": 1},
    )
    for row in _data(body):
        if _sel(row, "orgao", siglas):
            yield row


# ============ TRANSF. RECURSOS (api-transferencia-recursos) ============
# Paginado por ANO; param 'mes' e ignorado — filtramos dt_despesa no cliente.
TR_BASE = config.TRANSF_REC_API_BASE


def transf_rec_periods():
    def _get():
        return _periodos_mes(_data(_get_json(f"{TR_BASE}/transferencia-recursos/ano-mes")))
    return _cached("tr:periods", _get)


def transf_rec_orgaos(ano, mes):
    def _get():
        return _orgaos_padrao(_data(_get_json(f"{TR_BASE}/filtros/orgaos-empenho", {"ano": ano})))
    return _cached(f"tr:orgaos:{ano}", _get)


def transf_rec_iter(recorte, siglas):
    alvo = (recorte["ano"], recorte["mes"])
    for row in _paged(f"{TR_BASE}/transferencia-recursos",
                      params={"ano": recorte["ano"]},
                      delay=config.FOLHA_PAGE_DELAY_SECONDS):
        if _month_key(row, "dt_despesa") == alvo and _sel(row, "orgao", siglas):
            yield row


# ============ RECEITAS (api-receitas-publicas) ============
# Params FLAT (Spring @ModelAttribute), resposta = lista plana por orgao.
REC_BASE = config.RECEITAS_API_BASE


def receitas_periods():
    """A API so lista ANOS — mas aceita mesInicial/mesFinal no filtro,
    entao expandimos para meses (limitado ao mes corrente)."""
    def _get():
        anos = sorted(
            int(a["ano"] if isinstance(a, dict) else a)
            for a in _data(_get_json(f"{REC_BASE}/combos/anos"))
        )
        hoje = datetime.now()
        out = []
        for a in anos:
            mmax = 12 if a < hoje.year else hoje.month
            for m in range(1, mmax + 1):
                out.append({"ano": a, "mes": m})
        return out
    return _cached("rec:periods", _get)


def receitas_orgaos(ano, mes):
    def _get():
        raw = _data(_get_json(f"{REC_BASE}/combos/orgaos", {"ano": ano}))
        out = []
        for g in raw:
            label = g.get("siglaOrgao") or g.get("sigla") or g.get("nomeOrgao") or g.get("nome")
            if label:
                out.append({"sigla": label, "orgao": g.get("nomeOrgao") or g.get("nome") or label})
        return out
    return _cached(f"rec:orgaos:{ano}", _get)


def receitas_iter(recorte, siglas):
    body = _get_json(f"{REC_BASE}/receitas-publicas", {
        "ano": recorte["ano"],
        "mesInicial": recorte["mes"],
        "mesFinal": recorte["mes"],
    })
    for row in _data(body):
        # a lista de orgaos do combo vem com NOME; o row tem sigla+nome
        if _sel(row, "siglaOrgao", siglas) or _sel(row, "nomeOrgao", siglas):
            yield row


# ============ BENS MOVEIS (api-bens-moveis) ============
# /filtro/orgao paginado por (ano, mes, situacao A|I) — totais patrimoniais
# por orgao: quantitativo, valor_origem, valor_atual, depreciacao.
BM_BASE = config.BENS_MOVEIS_API_BASE


def bens_moveis_periods():
    def _get():
        return _periodos_mes(_data(_get_json(f"{BM_BASE}/bens-moveis/ano-mes")))
    return _cached("bm:periods", _get)


def bens_moveis_orgaos(ano, mes):
    def _get():
        # os rows de filtro trazem o NOME do orgao, nao a sigla
        return _orgaos_padrao(
            _data(_get_json(f"{BM_BASE}/bens-moveis/orgaos", {"ano": ano, "mes": mes})),
            nome_key="nome_orgao", pref_nome=True,
        )
    return _cached(f"bm:orgaos:{ano}:{mes}", _get)


def bens_moveis_iter(recorte, siglas):
    for situacao in ("A", "I"):
        for row in _paged(
            f"{BM_BASE}/bens-moveis/filtro/orgao",
            params={"ano": recorte["ano"], "mes": recorte["mes"], "situacao": situacao},
            size=500, size_key="qtdRegistros",
            delay=config.FOLHA_PAGE_DELAY_SECONDS,
        ):
            if _sel(row, "orgao", siglas):
                yield {**row, "situacao": "Ativo" if situacao == "A" else "Inativo"}


# ============ DIARIAS (api-despesas-diarias) ============
# /filtro paginado ~1,4M linhas, SEM filtro de periodo — varremos e filtramos
# dt_diaria no cliente. Dataset de recorte unico (granularity='range'):
# um documento por job cobrindo todo o intervalo.
DIA_BASE = config.DIARIAS_API_BASE


def diarias_periods():
    def _get():
        return _periodos_mes(_data(_get_json(f"{DIA_BASE}/dados-diarios/ano-mes")))
    return _cached("dia:periods", _get)


def diarias_orgaos(ano, mes):
    def _get():
        return _orgaos_padrao(_data(_get_json(f"{DIA_BASE}/dados-diarios/orgao/atualizado", {"ano": ano})))
    return _cached(f"dia:orgaos:{ano}", _get)


def diarias_iter(recorte, siglas):
    ini = (recorte["ano_ini"], recorte["mes_ini"])
    fim = (recorte["ano_fim"], recorte["mes_fim"])
    page, old_pages = 1, 0
    with http_client.new_client() as c:
        while old_pages < 3:  # para cedo se a pagina inteira for mais antiga que o recorte
            resp = c.get(
                f"{DIA_BASE}/dados-diarios/filtro",
                params={"pagina": page, "quantidade": PAGE_SIZE},
            )
            resp.raise_for_status()
            rows = _data(resp.json())
            if not rows:
                return
            datas = [_month_key(r, "dt_diaria") for r in rows]
            for row, ym in zip(rows, datas):
                if ym is not None and ini <= ym <= fim and _sel(row, "orgao", siglas):
                    yield row
            validos = [d for d in datas if d is not None]
            if validos and max(validos) < ini:
                old_pages += 1
            else:
                old_pages = 0
            if len(rows) < PAGE_SIZE:
                return
            page += 1
            time.sleep(config.FOLHA_PAGE_DELAY_SECONDS)


# ============ SUPRIMENTO DE FUNDOS (api-suprimento-fundos) ============
# Ordemes bancarias por ano — paginado, sem mes (granularity='ano').
SUP_BASE = config.SUPRIMENTO_API_BASE


def suprimento_periods():
    def _get():
        return _periodos_ano(_data(_get_json(f"{SUP_BASE}/suprimento-fundos/anos")))
    return _cached("sup:periods", _get)


def suprimento_orgaos(ano, mes):
    def _get():
        # sem sigla na API — o row traz o nome completo do orgao
        return _orgaos_padrao(
            _data(_get_json(f"{SUP_BASE}/suprimento-fundos/orgaos", {"ano": ano})),
            nome_key="nome", pref_nome=True,
        )
    return _cached(f"sup:orgaos:{ano}", _get)


def suprimento_iter(recorte, siglas):
    for row in _paged(f"{SUP_BASE}/suprimento-fundos",
                      params={"ano": recorte["ano"]},
                      delay=config.FOLHA_PAGE_DELAY_SECONDS):
        if _sel(row, "orgao", siglas):
            yield row


# ============ OBRAS PUBLICAS (api-obras-publicas) ============
# Obras por ano (granularity='ano'); row traz orgaoGestorSigla.
OBR_BASE = config.OBRAS_API_BASE


def obras_periods():
    def _get():
        return _periodos_ano(_data(_get_json(f"{OBR_BASE}/obras-publicas/anos")))
    return _cached("obr:periods", _get)


def obras_orgaos(ano, mes):
    def _get():
        return _orgaos_padrao(_data(_get_json(f"{OBR_BASE}/obras-publicas/orgaos", {"ano": ano})))
    return _cached(f"obr:orgaos:{ano}", _get)


def obras_iter(recorte, siglas):
    for row in _paged(f"{OBR_BASE}/obras-publicas",
                      params={"ano": recorte["ano"]},
                      delay=config.FOLHA_PAGE_DELAY_SECONDS):
        if _sel(row, "orgaoGestorSigla", siglas):
            yield row


# ============ AREAS TEMATICAS (api-areas-tematicas) ============
# /por-orgaos?tipoAreaTematica=<DESCRICAO>&ano — agrega convenios/projeto
# por orgao e ano; tipo e a DESCRICAO (SAUDE/SANEAMENTO/EDUCACAO).
ARE_BASE = config.AREAS_API_BASE


def areas_periods():
    def _get():
        return _periodos_ano(_data(_get_json(f"{ARE_BASE}/areas-tematicas/anos")))
    return _cached("are:periods", _get)


def _areas_tipos():
    def _get():
        return [t["descricao"] for t in _data(_get_json(f"{ARE_BASE}/areas-tematicas/tipos"))]
    return _cached("are:tipos", _get)


def areas_orgaos(ano, mes):
    def _get():
        # por-orgaos devolve o nome do orgao no campo 'orgao'
        return _orgaos_padrao(
            _data(_get_json(f"{ARE_BASE}/areas-tematicas/orgaos", {"ano": ano})),
            pref_nome=True,
        )
    return _cached(f"are:orgaos:{ano}", _get)


def areas_iter(recorte, siglas):
    for tipo in _areas_tipos():
        body = _get_json(
            f"{ARE_BASE}/areas-tematicas/por-orgaos",
            {"tipoAreaTematica": tipo, "ano": recorte["ano"]},
        )
        rows = _data(body)
        if rows and isinstance(rows[0], list):
            rows = rows[0]  # alguns endpoints aninham: {"data": [[...]]}
        for row in rows:
            if _sel(row, "orgao", siglas):
                yield {**row, "area_tematica": tipo}


# ============ BENS IMOVEIS (api-bens-imoveis) ============
# Inventario patrimonial — snapshot sem periodo (granularity='snapshot');
# /filtros?situacao=A|I&qtdRegistros=N devolve ate N num so GET.
BIM_BASE = config.BENS_IMOVEIS_API_BASE


def bens_imoveis_orgaos(ano, mes):
    def _get():
        return _orgaos_padrao(_data(_get_json(f"{BIM_BASE}/filtros/orgaos")))
    return _cached("bim:orgaos", _get)


def bens_imoveis_iter(recorte, siglas):
    for situacao in ("A", "I"):
        body = _get_json(
            f"{BIM_BASE}/bens-imoveis/filtros", {"situacao": situacao, "qtdRegistros": 200000}
        )
        for row in _data(body):
            if _sel(row, "orgao", siglas):
                yield {**row, "situacao": "Ativo" if situacao == "A" else "Inativo"}


# ============ TRANSF. VOLUNTARIAS (api-transferencias-voluntarias) ============
# Instrumentos/convenios — snapshot paginado (granularity='snapshot').
TV_BASE = config.TRANSF_VOL_API_BASE


def transf_vol_orgaos(ano, mes):
    """Orgaos concedentes dos convenios — derivado dos proprios
    instrumentos (a API nao tem lista de orgaos dedicada)."""
    from app import transparencia

    def _get():
        out = {}
        for row in _paged(
            f"{TV_BASE}/transferencias-voluntarias/instrumentos",
            size=500,
        ):
            nome = row.get("nomeConcedente")
            if nome:
                out[transparencia.orgao_slug(nome)] = nome
        return [{"sigla": s, "orgao": n} for s, n in sorted(out.items())]

    return _cached("tv:orgaos", _get)


def _tv_match(siglas):
    """Set de siglas canonicas selecionadas, ou None (todos)."""
    from app import transparencia

    return {transparencia.orgao_slug(s) for s in siglas} if siglas else None


def transf_vol_iter(recorte, siglas):
    del recorte
    alvo = _tv_match(siglas)
    from app import transparencia

    for row in _paged(
        f"{TV_BASE}/transferencias-voluntarias/instrumentos",
        size=500,  # a API responde vazio acima de ~500
        delay=config.FOLHA_PAGE_DELAY_SECONDS,
    ):
        if alvo is None or transparencia.orgao_slug(row.get("nomeConcedente") or "") in alvo:
            yield row


def _tv_instrumento_sigla():
    """Mapa idinstrumento -> sigla do concedente (cacheado)."""
    from app import transparencia

    def _get():
        out = {}
        for row in _paged(
            f"{TV_BASE}/transferencias-voluntarias/instrumentos", size=500
        ):
            nome = row.get("nomeConcedente")
            if row.get("idinstrumento") and nome:
                out[row["idinstrumento"]] = transparencia.orgao_slug(nome)
        return out

    return _cached("tv:instr_sigla", _get)


def transf_vol_licitacoes(recorte, siglas):
    """Licitacoes vinculadas aos convenios — sigla via idinstrumento."""
    del recorte
    alvo = _tv_match(siglas)
    mapa = _tv_instrumento_sigla()
    for row in _paged(
        f"{TV_BASE}/transferencias-voluntarias/licitacoes",
        size=500,
        delay=config.FOLHA_PAGE_DELAY_SECONDS,
    ):
        sigla = mapa.get(row.get("idinstrumento"))
        if sigla is None or (alvo and sigla not in alvo):
            continue
        yield {**row, "sigla_orgao": sigla}


def transf_vol_docs(recorte, siglas):
    """Documentos dos instrumentos (termo + plano de trabalho) — PDFs por
    ORGAO CONCEDENTE: cada convenio vai pra pasta da sigla do concedente
    (ex.: SEINFRA/transf_voluntarias/documentos/010-2017/termo.pdf)."""
    from app import transparencia

    del recorte
    alvo = _tv_match(siglas)
    for row in _paged(
        f"{TV_BASE}/transferencias-voluntarias/instrumentos",
        size=500,
        delay=config.FOLHA_PAGE_DELAY_SECONDS,
    ):
        concedente = row.get("nomeConcedente")
        if not concedente:
            continue
        slug = transparencia.orgao_slug(concedente)
        if alvo and slug not in alvo:
            continue
        numero = transparencia.orgao_slug(str(row.get("numeroAno") or row.get("idinstrumento")))
        for tipo, campo in (
            ("termo", "documentoTermoInstrumentoS3ID"),
            ("plano_trabalho", "documentoPlanoTrabalhoS3ID"),
        ):
            s3id = row.get(campo)
            if not s3id:
                continue
            yield {
                "url": (
                    f"{TV_BASE}/transferencias-voluntarias/instrumentos/"
                    f"documentos/{s3id}/pdf"
                ),
                "orgao": slug,
                "sub": numero,
                "nome": f"{tipo}.pdf",
            }


# ---------- PNCP — Portal Nacional de Contratacoes Publicas (Lei 14.133/21) ----------
#
# Fonte centralizada e obrigatoria de licitacoes/contratos. A API de consulta
# indexa por CNPJ do orgao (nao por sigla) — o mapa abaixo cobre o executivo
# estadual do PA; pra incluir outro orgao, basta descobrir o CNPJ e adicionar.
# Documentos (edital/ata/contrato PDF) ficam na pncp-api, servico separado que
# oscila — iter_docs e best-effort e nao derruba a unidade do CSV.

PNCP_CNPJS = {
    "DETRAN": "04822060000140",
    "JUCEPA": "04825329000142",
    "PRODEPA": "05059613000118",
    "SEDUC": "05054937000163",
    "SEFA": "05054903000179",
    "SEGUP": "05054952000101",
    "SEINFRA": "04953717000109",
    "SEMAS": "34921783000168",
    "SEOP": "03137985000190",
    "SESPA": "05054929000117",
}


def pncp_orgaos(ano: int, mes: int):
    """Universo do PNCP = orgaos com CNPJ mapeado."""
    return [{"sigla": s, "orgao": f"{s} — CNPJ {c}"} for s, c in PNCP_CNPJS.items()]


def pncp_periods():
    """Publicacao no PNCP obrigatoria a partir de 2023."""
    return [{"ano": a, "mes": None} for a in range(2023, datetime.now().year + 1)]


def _pncp_contratos_list(ano: int, cnpj: str) -> list[dict]:
    """Contratos do orgao no ano — cacheado p/ iter_docs+iter_rows usarem 1 fetch.
    PNCP responde 204 (corpo vazio) quando o orgao nao tem contrato no
    periodo — trata como lista vazia em vez de deixar o json() quebrar."""

    def _fetch():
        rows, pagina = [], 1
        with http_client.new_client() as c:
            while True:
                resp = c.get(
                    f"{config.PNCP_API_BASE}/contratos",
                    params={
                        "dataInicial": f"{ano}0101",
                        "dataFinal": f"{ano}1231",
                        "cnpjOrgao": cnpj,
                        "pagina": pagina,
                        "tamanhoPagina": 200,
                    },
                )
                if resp.status_code == 204 or not resp.content:
                    return rows
                resp.raise_for_status()
                d = resp.json()
                rows += _data(d)
                if pagina >= d.get("totalPaginas", 1):
                    return rows
                pagina += 1
                time.sleep(config.FOLHA_PAGE_DELAY_SECONDS)

    return _cached(f"pncp:contratos:{ano}:{cnpj}", _fetch)


def _flat_contrato(row: dict, sigla: str) -> dict:
    oe, uo = row.get("orgaoEntidade") or {}, row.get("unidadeOrgao") or {}
    return {
        "orgao": sigla,
        "cnpj_orgao": oe.get("cnpj"),
        "razao_orgao": oe.get("razaoSocial"),
        "unidade": uo.get("nomeUnidade"),
        "municipio": uo.get("municipioNome"),
        "processo": row.get("processo"),
        "ano_contrato": row.get("anoContrato"),
        "sequencial": row.get("sequencialContrato"),
        "objeto": row.get("objetoContrato"),
        "fornecedor": row.get("nomeRazaoSocialFornecedor"),
        "ni_fornecedor": row.get("niFornecedor"),
        "tipo_pessoa": row.get("tipoPessoa"),
        "valor_global": row.get("valorGlobal"),
        "valor_parcela": row.get("valorParcela"),
        "categoria": (row.get("categoriaProcesso") or {}).get("nome"),
        "data_assinatura": row.get("dataAssinatura"),
        "vigencia_inicio": row.get("dataVigenciaInicio"),
        "vigencia_fim": row.get("dataVigenciaFim"),
        "publicacao_pncp": row.get("dataPublicacaoPncp"),
        "compra": row.get("numeroControlePncpCompra"),
    }


def pncp_contratos_iter(recorte: dict, siglas: list | None):
    ano = recorte["ano"]
    for sigla in siglas or []:
        cnpj = PNCP_CNPJS.get(str(sigla).upper())
        if not cnpj:
            continue
        for row in _pncp_contratos_list(ano, cnpj):
            yield _flat_contrato(row, sigla)


def _compra_ref(contrato: dict):
    """'05054937000163-1-000031/2025' -> (cnpj, ano, sequencial)."""
    try:
        ref = str(contrato.get("numeroControlePncpCompra") or "")
        cnpj = ref.split("-")[0]
        seq, ano = ref.split("-")[-1].split("/")
        return cnpj, int(ano), int(seq)
    except (IndexError, ValueError):
        return None


def pncp_contratos_docs(recorte: dict, siglas: list | None):
    """Arquivos da contratacao vinculada a cada contrato (edital, ata,
    contrato). Uma compra pode gerar varios contratos — dedup por ref.
    Falha na pncp-api de uma compra = warn + segue (best-effort)."""
    ano = recorte["ano"]
    vistos = set()
    for sigla in siglas or []:
        cnpj = PNCP_CNPJS.get(str(sigla).upper())
        if not cnpj:
            continue
        for ctr in _pncp_contratos_list(ano, cnpj):
            ref = _compra_ref(ctr)
            if not ref or ref in vistos:
                continue
            vistos.add(ref)
            ccnpj, cano, cseq = ref
            base = f"{config.PNCP_FILES_BASE}/orgaos/{ccnpj}/compras/{cano}/{cseq}"
            try:
                body = _get_json(f"{base}/arquivos")
                arquivos = _data(body)
            except Exception as e:
                log.warning("pncp-api arquivos falhou p/ %s: %s", ref, e)
                continue
            proc = re.sub(
                r"[^\w.-]+", "_",
                str(ctr.get("processo") or f"{cano}-{cseq}"),
            ).strip("_")
            for a in arquivos:
                titulo = a.get("titulo") or f"arquivo_{a.get('sequencialDocumento')}"
                if "." not in titulo:
                    titulo += ".pdf"
                yield {
                    "url": f"{base}/arquivos/{a['sequencialDocumento']}",
                    "orgao": sigla,
                    "sub": proc,
                    "nome": titulo,
                }
            time.sleep(config.FOLHA_PAGE_DELAY_SECONDS)
