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

from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from app import datasets, despesas, folha


@dataclass(frozen=True)
class DatasetSpec:
    slug: str
    granularity: str  # 'mes' | 'ano' | 'range' | 'snapshot'
    list_periods: Callable[[], list[dict]]
    list_orgaos: Callable[[int, int], list[dict]]
    iter_rows: Callable[[dict, Optional[list[str]]], Iterator[dict]]


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
    "transf_voluntarias": DatasetSpec("transf_voluntarias", "snapshot", lambda: [], lambda a, m: [],
                                      datasets.transf_vol_iter),
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


def orgao_slug(label: str) -> str:
    """Nome de diretorio seguro para a sigla/nome do orgao."""
    import re

    return re.sub(r"[^A-Z0-9._-]+", "_", str(label).upper()).strip("_")[:50]


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
