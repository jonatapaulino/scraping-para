"""Cliente da API de despesas publicas do Portal da Transparencia do Para.

GET /despesas/?ano&mesInicio&mesFim devolve a execucao orcamentaria por
orgao (orcado → empenhado → liquidado → pago) em uma unica resposta —
sem paginacao. O /exportar do site tem captcha e nao e necessario."""

import logging
from typing import Iterator, Optional

from app import config, http_client
from app.folha import _cached

log = logging.getLogger(__name__)


def list_periods() -> list[dict]:
    """[{'ano','mes','mes_extenso'}] — a API devolve decrescente;
    normalizamos para crescente."""
    def _get():
        with http_client.new_client() as c:
            resp = c.get(f"{config.DESPESAS_API_BASE}/ano-mes")
            resp.raise_for_status()
            return sorted(resp.json()["data"], key=lambda p: (p["ano"], p["mes"]))
    return _cached("despesas:periods", _get)


def list_orgaos(ano: int) -> list[dict]:
    """Orgaos com despesa no ano: [{'id_orgao','sigla','orgao'}].
    NB: o universo de siglas difere da folha (PMPA, POLICIA CIVIL...)"""
    def _get():
        with http_client.new_client() as c:
            resp = c.get(
                f"{config.DESPESAS_API_BASE}/orgaos/atualizado", params={"ano": ano}
            )
            resp.raise_for_status()
            return resp.json()["data"]
    return _cached(f"despesas:orgaos:{ano}", _get)


def list_orgaos_mes(ano: int, mes: int) -> list[dict]:
    """Adapter pro registry — o endpoint de orgaos so leva o ano."""
    return list_orgaos(ano)


def iter_rows(ano: int, mes: int, siglas: Optional[list[str]] = None) -> Iterator[dict]:
    """Execucao orcamentaria do mes — um GET so. Filtra por sigla_orgao
    quando `siglas` e informado."""
    wanted = {s.upper() for s in siglas} if siglas else None
    with http_client.new_client(timeout=config.HTTP_TIMEOUT_SECONDS * 2) as c:
        resp = c.get(
            f"{config.DESPESAS_API_BASE}/",
            params={"ano": ano, "mesInicio": mes, "mesFim": mes},
        )
        resp.raise_for_status()
        for row in resp.json().get("data") or []:
            if wanted is None or str(row.get("sigla_orgao") or "").upper() in wanted:
                yield row
