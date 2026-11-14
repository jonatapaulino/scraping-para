"""Cliente da API de dados abertos do Portal da Transparencia do Para.

A interface publica (SPA em sistemas.pa.gov.br/portaltransparencia) consome
APIs REST tematicas. A de servidores expõe os dados paginados em JSON sem
captcha — o endpoint /exportar (botao do site) e o unico protegido e nao
e necessario: geramos o CSV a partir do JSON paginado."""

import logging
import time
from typing import Iterator, Optional

from app import config, http_client

log = logging.getLogger(__name__)

_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300  # segundos — opcoes mudam pouco; evita martelar a API


def _cached(key: str, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def list_periods() -> list[dict]:
    """Periodos publicados: [{'ano': 2025, 'mes': 1, 'mes_extenso': 'JANEIRO'}]."""
    def _get():
        with http_client.new_client() as c:
            resp = c.get(f"{config.FOLHA_API_BASE}/ano-mes")
            resp.raise_for_status()
            return resp.json()["data"]
    return _cached("periods", _get)


def list_orgaos(ano: int, mes: int) -> list[dict]:
    """Orgaos com folha publicada no periodo: [{'id_orgao', 'sigla', 'orgao'}]."""
    def _get():
        with http_client.new_client() as c:
            resp = c.get(
                f"{config.FOLHA_API_BASE}/orgaos/atualizado",
                params={"ano": ano, "mes": mes},
            )
            resp.raise_for_status()
            return resp.json()["data"]
    return _cached(f"orgaos:{ano}:{mes}", _get)


def iter_funcionarios(
    ano: int, mes: int, orgao: Optional[int] = None, page_size: Optional[int] = None
) -> Iterator[dict]:
    """Itera os registros da folha do periodo (paginas de `page_size`).

    `orgao` = id_orgao; None = todos. Para quando uma pagina vem vazia.
    Lanca httpx.HTTPError/HTTPStatusError em falhas — retriavel pela task."""
    size = page_size or config.FOLHA_PAGE_SIZE
    params = {"ano": ano, "mes": mes, "quantidade": size}
    if orgao is not None:
        params["orgao"] = orgao
    with http_client.new_client(timeout=config.HTTP_TIMEOUT_SECONDS * 2) as c:
        pagina = 1
        while True:
            resp = c.get(
                f"{config.FOLHA_API_BASE}/funcionarios/filtro",
                params={**params, "pagina": pagina},
            )
            resp.raise_for_status()
            rows = resp.json().get("data") or []
            if not rows:
                return
            yield from rows
            pagina += 1
            if len(rows) < size:
                return
            time.sleep(config.FOLHA_PAGE_DELAY_SECONDS)


def iter_rows(ano: int, mes: int, siglas: Optional[list[str]] = None) -> Iterator[dict]:
    """Adapter pro registry de datasets: sigla -> id do periodo -> pagina.
    Sigla e a chave estavel; id_orgao muda a cada competencia."""
    if siglas:
        resolved = resolve_orgaos(ano, mes, siglas)
        missing = [s for s, i in resolved.items() if i is None]
        if missing:
            log.warning("Folha %d/%02d: orgaos sem dados no periodo: %s", ano, mes, missing)
        fontes = [i for i in resolved.values() if i is not None]
    else:
        fontes = [None]  # sem filtro = todos os orgaos
    for oid in fontes:
        yield from iter_funcionarios(ano, mes, orgao=oid)


def resolve_orgaos(ano: int, mes: int, siglas: list[str]) -> dict[str, Optional[int]]:
    """Sigla -> id_orgao no periodo. O id MUDA entre periodos, entao
    resolvemos a cada mes; sigla e a chave estavel."""
    wanted = {s.upper() for s in siglas}
    return {
        s: next(
            (g["id_orgao"] for g in list_orgaos(ano, mes) if g["sigla"].upper() == s),
            None,
        )
        for s in sorted(wanted)
    }


def synthetic_url(ano: int, mes: int, orgaos: Optional[list[str]]) -> str:
    """Chave de dedup do documento gerado (mes+selecao de siglas)."""
    sel = "todos" if not orgaos else "-".join(sorted(s.upper() for s in orgaos))
    return f"transparencia://folha/{ano}/{mes:02d}?orgaos={sel}"


def selection_label(orgaos: Optional[list[str]]) -> str:
    """Sufixo do nome do arquivo: 'todos', as siglas (ate 3) ou
    'selecao-Norgaos' quando a lista e grande demais pro nome."""
    if not orgaos:
        return "todos"
    siglas = sorted(s.upper() for s in orgaos)
    if len(siglas) <= 3:
        return "-".join(siglas)
    return f"selecao-{len(siglas)}orgaos"
