"""Registro de datasets de dados abertos do Portal da Transparencia.

Cada dataset expõe a mesma interface:
  list_periods() -> [{'ano','mes'?}] crescente (vazio para snapshot)
  list_orgaos(ano, mes) -> [{'sigla','orgao'}]  (sigla ou nome — rotulo de filtro)
  iter_rows(recorte, siglas|None) -> linhas do recorte

granularity:
  'mes'      — um documento por (ano, mes)
  'ano'      — um documento por ano
  'range'    — um documento unico cobrindo o intervalo pedido
  'snapshot' — um documento unico, sem periodo

recorte carregado ate o fetch: {"ano","mes"} | {"ano"} |
{"ano_ini","mes_ini","ano_fim","mes_fim"} | {}.
Sigla (ou nome, conforme o dataset) e a chave estavel de filtro de orgao —
ids mudam por competencia e por API."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, Optional

from app import datasets, despesas, folha


@dataclass(frozen=True)
class DatasetSpec:
    slug: str
    granularity: str  # 'mes' | 'ano' | 'range' | 'snapshot'
    list_periods: Callable[[], list[dict]]
    list_orgaos: Callable[[int, int], list[dict]]
    iter_rows: Callable[[dict, Optional[list[str]]], Iterator[dict]]
    # datasets que tambem publicam documentos PDF por orgao
    iter_docs: Optional[Callable[[dict, Optional[list[str]]], Iterator[dict]]] = None
    # False = fora de "todos os datasets" (volume grande / fonte nao financeira)
    incluir_em_todos: bool = True


def _mes_iters(iter_ano_mes):
    """Adapta iter(ano, mes, siglas) para iter_rows(recorte, siglas)."""
    return lambda recorte, siglas: iter_ano_mes(recorte["ano"], recorte["mes"], siglas)


DATASETS: dict[str, DatasetSpec] = {
    "folha": DatasetSpec("folha", "mes", folha.list_periods, folha.list_orgaos,
                         _mes_iters(folha.iter_rows)),
    "despesas": DatasetSpec("despesas", "mes", despesas.list_periods, despesas.list_orgaos_mes,
                            _mes_iters(despesas.iter_rows)),
    "empenhos": DatasetSpec("empenhos", "mes", datasets.empenhos_periods, datasets.empenhos_orgaos,
                            datasets.empenhos_iter),
    "transf_recursos": DatasetSpec("transf_recursos", "mes", datasets.transf_rec_periods,
                                   datasets.transf_rec_orgaos, datasets.transf_rec_iter),
    "receitas": DatasetSpec("receitas", "mes", datasets.receitas_periods, datasets.receitas_orgaos,
                            datasets.receitas_iter),
    "bens_moveis": DatasetSpec("bens_moveis", "mes", datasets.bens_moveis_periods,
                               datasets.bens_moveis_orgaos, datasets.bens_moveis_iter),
    "diarias": DatasetSpec("diarias", "range", datasets.diarias_periods, datasets.diarias_orgaos,
                           datasets.diarias_iter),
    "suprimento": DatasetSpec("suprimento", "ano", datasets.suprimento_periods,
                              datasets.suprimento_orgaos, datasets.suprimento_iter),
    "obras": DatasetSpec("obras", "ano", datasets.obras_periods, datasets.obras_orgaos,
                         datasets.obras_iter),
    "areas": DatasetSpec("areas", "ano", datasets.areas_periods, datasets.areas_orgaos,
                         datasets.areas_iter),
    "bens_imoveis": DatasetSpec("bens_imoveis", "snapshot", lambda: [],
                                datasets.bens_imoveis_orgaos, datasets.bens_imoveis_iter),
    "transf_voluntarias": DatasetSpec("transf_voluntarias", "snapshot", lambda: [],
                                      datasets.transf_vol_orgaos, datasets.transf_vol_iter,
                                      datasets.transf_vol_docs),
    "licitacoes": DatasetSpec("licitacoes", "snapshot", lambda: [],
                              datasets.transf_vol_orgaos,
                              datasets.transf_vol_licitacoes),
    "pncp_contratos": DatasetSpec("pncp_contratos", "ano", datasets.pncp_periods,
                                  datasets.pncp_orgaos, datasets.pncp_contratos_iter,
                                  datasets.pncp_contratos_docs),
    "diario_oficial": DatasetSpec("diario_oficial", "ano", datasets.diario_periods,
                                  datasets.diario_orgaos, datasets.diario_iter,
                                  datasets.diario_docs, incluir_em_todos=False),
}


def get_spec(dataset: str) -> DatasetSpec | None:
    return DATASETS.get(dataset)


def build_recortes(spec: DatasetSpec, ini, fim) -> list[dict]:
    """Unidades de coleta do job conforme a granularidade."""
    if spec.granularity == "snapshot":
        return [{}]
    if spec.granularity == "range":
        return [{
            "ano_ini": ini[0], "mes_ini": ini[1],
            "ano_fim": fim[0], "mes_fim": fim[1],
        }]
    periodos = spec.list_periods()
    if spec.granularity == "ano":
        return [
            {"ano": p["ano"]} for p in periodos
            if ini[0] <= p["ano"] <= fim[0]
        ]
    return [
        {"ano": p["ano"], "mes": p["mes"]} for p in periodos
        if ini <= (p["ano"], p["mes"]) <= fim
    ]


def synthetic_url(dataset: str, recorte: dict, orgao: Optional[str]) -> str:
    """Chave de dedup do documento gerado (dataset + recorte + orgao).
    orgao=None => unico arquivo do recorte (dataset sem orgaos / varredura
    unica); senao, um documento por orgao."""
    if "mes" in recorte:
        chave = f"{recorte['ano']}/{recorte['mes']:02d}"
    elif "ano" in recorte:
        chave = str(recorte["ano"])
    elif "ano_ini" in recorte:
        chave = (
            f"{recorte['ano_ini']}-{recorte['mes_ini']:02d}"
            f"_{recorte['ano_fim']}-{recorte['mes_fim']:02d}"
        )
    else:
        chave = "snapshot"
    sufixo = f"/{orgao}" if orgao else ""
    return f"transparencia://{dataset}/{chave}{sufixo}"


def _norm(txt: str) -> str:
    """Normaliza para comparacao de nomes entre APIs (sem acento/case/espaco)."""
    import unicodedata

    return re.sub(
        r"\s+", " ",
        "".join(
            c for c in unicodedata.normalize("NFKD", str(txt).upper())
            if not unicodedata.combining(c)
        ),
    ).strip()


def nomes_oficiais() -> list[tuple[str, str]]:
    """[(nome oficial, sigla)] da lista de orgaos das despesas/empenhos — o
    campo 'orgao' vem como 'NOME OFICIAL-SIGLA' e e o unico que expoe o par
    (nome oficial + sigla) igual ao dos outros datasets. Vazio se a API caiu."""
    import httpx

    from app import config

    for base in (
        f"{config.DESPESAS_API_BASE}/orgaos/atualizado",
        f"{config.EMPENHOS_API_BASE}/filtros/orgaos-empenho",
    ):
        try:
            data = httpx.get(base, params={"ano": datetime.now().year}, timeout=20).json()
            rows = data.get("data", data) if isinstance(data, dict) else data
        except Exception:
            continue
        out = []
        for g in rows:
            sigla = g.get("sigla")
            if not sigla:
                continue
            nome = g.get("orgao") or ""
            if "-" in nome:
                nome = nome.rsplit("-", 1)[0]  # 'NOME OFICIAL-SIGLA' -> nome
            out.append((nome.strip(), sigla))
        if out:
            return out
    return []


def _build_sigla_canon() -> dict:
    out = {}
    for nome, sigla in nomes_oficiais():
        out.setdefault(_chave(sigla), sigla)
        if nome:
            out.setdefault(_chave(nome), sigla)
    return out


def _chave(txt: str) -> str:
    """Chave de comparacao: sem acento, maiuscula, so letras e numeros —
    'IDEFLOR-BIO', 'IDEFLORBIO' e 'Ideflor - Bio' viram a mesma chave."""
    return re.sub(r"[^A-Z0-9]", "", _norm(txt))


def _nome_seguro(txt: str) -> str:
    """Nome de pasta: sem acento (antes 'AGÊNCIA' virava 'AG_NCIA'), sem
    pontos/espacos ('FUND. SANTA CASA' -> 'FUND_SANTA_CASA')."""
    s = re.sub(r"[^A-Z0-9-]+", "_", _norm(txt))
    s = re.sub(r"_*-_*", "-", s)  # 'IDEFLOR - BIO' -> 'IDEFLOR-BIO', nao 'IDEFLOR_-_BIO'
    return re.sub(r"_+", "_", s).strip("_-")[:50] or "SEM_NOME"


def _sigla_canon() -> dict:
    """Mapa cacheado por processo de nome-oficial/sigla -> sigla canonica.
    Mapa vazio (API fora do ar) NAO fica no cache: antes ele valia por 5 min
    e os arquivos desse intervalo iam para pastas com o nome por extenso."""
    mapa = folha._cached("canon:siglas", _build_sigla_canon)
    if not mapa:
        folha._descarta("canon:siglas")
    return mapa


# Mesmo orgao com rotulos diferentes em APIs diferentes (sigla de um lado, nome
# por extenso ou outra sigla do outro). Destino = sigla oficial da API de
# despesas (a lista base do painel). So equivalencias inequivocas — rotulos
# ambiguos (ex.: 'Gabinete do Governador', SEJUDH x SEJU x SEIRDH, que sao
# secretarias de epocas diferentes) ficam de fora de proposito.
# Para acrescentar: 'rotulo como vem na API': 'SIGLA OFICIAL'.
APELIDOS = {
    "AGÊNCIA ESTADUAL DE REGULAÇÃO E CONTROLE DE SERVIÇOS PÚBLICOS DO ESTADO DO PARÁ": "ARCON",
    "CASA CIVIL": "GAB. GOV - CASA CIV",
    "CASA CIVIL DA GOVERNADORIA": "GAB. GOV - CASA CIV",
    "GABGOV (CASA CIVIL)": "GAB. GOV - CASA CIV",
    "CASA MILIT": "CASA MILITAR",
    "GABGOV (CASA MILITAR)": "CASA MILITAR",
    "GAB/VICE": "GAB. VICE-GOVERNADO",
    "GABINETE DO VICE-GOVERNADOR": "GAB. VICE-GOVERNADO",
    "DEFENSORIA PÚBLICA DO ESTADO DO PARÁ": "DEFPUB",
    "EMP.TEC.DA INFORMACAO E COMUNICACAO DO ESTADO DO PARA.": "PRODEPA",
    "EMPRESA ASSISTÊNCIA TÉCNICA E EXTENSÃO RURAL DO ESTADO DO PARÁ": "EMATER",
    "FSCMP": "FUND. SANTA CASA",
    "FSCMPA": "FUND. SANTA CASA",
    "INSTITUTO DE DESENVOLVIMENTO FLORESTAL E DA BIODIVERSIDADE DO ESTADO DO PARÁ": "IDEFLOR-BIO",
    "INSTITUTO_DE_DESENVOLVIMENTO_FLORESTAL_E_DA_BIODIV": "IDEFLOR-BIO",
    "NÚCLEO DE GERENCIAMENTO DE TRANSPORTE METROPOLITANO": "NGTM",
    "NÚCLEO DE GERENCIAMENTO DO PROGRAMA DE MICROCRÉDITO": "NGPMCREDCID",
    "NGPM": "NGPMCREDCID",
    "CREDCIDADÃO": "NGPMCREDCID",
    "PARATUR - ÓRGÃO EM EXTINÇÃO": "PARATUR-EXT",
    "PC": "POLICIA CIVIL",
    "PCPA": "POLICIA CIVIL",
    "POLÍCIA CIENTÍFICA DO PARÁ": "PCEPA",
    "POLÍCIA MILITAR DO ESTADO DO PARÁ": "PMPA",
    "SECRETARIA DE ESTADO DE DESENVOLVIMENTO URBANO E OBRAS PÚBLICAS": "SEDOP",
    "SECRETARIA DE ESTADO DE SEGURANÇA PÚBLICA": "SEGUP",
    "SECRETARIA DE ESTADO DE AGRICULTURA FAMILIAR": "SEAF",
    "TRIBUNAL DE CONTAS DO ESTADO": "TCE",
    "TRIBUNAL DE JUSTIÇA DO ESTADO": "TJPA",
    "TRIBUNAL_DE_JUSTI_A_DO_ESTADO": "TJPA",  # nome com acento ja quebrado (versao antiga)
}


def _apelidos() -> dict:
    return {_chave(k): v for k, v in APELIDOS.items()}


_APELIDOS_CHAVE = None


def orgao_slug(label: str) -> str:
    """Nome de diretorio do orgao: sigla canonica quando conhecida
    (ex.: 'Secretaria de Estado de Saúde Pública' -> 'SESPA'); senao,
    nome seguro do proprio rotulo. Sempre passa por _nome_seguro — a sigla
    da API tambem vem com espacos/pontos ('GAB. GOV - CASA CIV')."""
    global _APELIDOS_CHAVE
    if _APELIDOS_CHAVE is None:
        _APELIDOS_CHAVE = _apelidos()
    chave = _chave(label)
    alvo = _APELIDOS_CHAVE.get(chave)
    if alvo:
        chave = _chave(alvo)
    sigla = _sigla_canon().get(chave) or alvo
    return _nome_seguro(sigla or str(label))


def recorte_label(recorte: dict) -> str:
    """Rotulo do recorte para o nome do arquivo."""
    if "mes" in recorte:
        return f"{recorte['ano']}-{recorte['mes']:02d}"
    if "ano" in recorte:
        return str(recorte["ano"])
    if "ano_ini" in recorte:
        return (
            f"{recorte['ano_ini']}-{recorte['mes_ini']:02d}"
            f"_a_{recorte['ano_fim']}-{recorte['mes_fim']:02d}"
        )
    return "snapshot"
