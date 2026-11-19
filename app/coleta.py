"""Coleta generica de dados de qualquer site, configurada pelo painel (sem codigo).

Dois modos:
- ``json``: a URL devolve JSON (API) — a lista de registros e achada sozinha
  ou apontada por ``caminho`` (ex.: ``dados.itens``).
- ``tabela_html``: a pagina tem uma <table> — vira linhas/colunas (a tabela
  com mais linhas, ou a escolhida por ``caminho`` = seletor CSS).

Paginacao: ponha ``{page}`` na URL (ex.: ``https://x/api?pagina={page}``);
a coleta anda pagina a pagina ate vir vazia/repetida ou chegar em ``max_paginas``.
"""
import hashlib
import json
import re
import time
from typing import Iterator, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

from app import http_client


class ColetaConfig(BaseModel):
    url: str = Field(max_length=2000)
    modo: Literal["json", "tabela_html"] = "json"
    # json: caminho ate a lista ("dados.itens"; vazio = autodetecta)
    # tabela_html: seletor CSS da tabela (vazio = a tabela com mais linhas)
    caminho: str = Field(default="", max_length=255)
    max_paginas: int = Field(default=1, ge=1, le=5000)
    pagina_inicial: int = Field(default=1, ge=0, le=1_000_000)
    renderizar: bool = False  # tabela_html: carrega via Chrome headless (paginas com JS)
    cabecalhos: str = Field(default="", max_length=2000)  # "Nome: valor" por linha

    @field_validator("url")
    @classmethod
    def _url_ok(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("URL invalida — use http(s)://host/caminho")
        return v

    @field_validator("caminho", "cabecalhos")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


def _cabecalhos(cfg: ColetaConfig) -> dict:
    out = {}
    for linha in cfg.cabecalhos.splitlines():
        nome, sep, valor = linha.partition(":")
        if sep and nome.strip():
            out[nome.strip()] = valor.strip()
    return out


# ---------- extracao ----------


def _achata(obj: dict, prefixo: str = "") -> dict:
    out = {}
    for k, v in obj.items():
        chave = f"{prefixo}{k}"
        if isinstance(v, dict):
            out.update(_achata(v, chave + "."))
        elif isinstance(v, list):
            out[chave] = json.dumps(v, ensure_ascii=False)
        else:
            out[chave] = v
    return out


def _registros(lista: list) -> list[dict]:
    return [_achata(x) if isinstance(x, dict) else {"valor": x} for x in lista]


def linhas_json(data, caminho: str = "") -> list[dict]:
    if caminho:
        atual = data
        for parte in caminho.split("."):
            if isinstance(atual, dict) and parte in atual:
                atual = atual[parte]
            elif isinstance(atual, list) and parte.isdigit() and int(parte) < len(atual):
                atual = atual[int(parte)]
            else:
                raise ValueError(f"Caminho '{caminho}' nao existe na resposta (parou em '{parte}')")
        data = atual
    if isinstance(data, list):
        return _registros(data)
    if isinstance(data, dict):
        if caminho:
            return _registros([data])
        # autodetecta: primeira lista de objetos (busca em largura)
        fila = [data]
        while fila:
            d = fila.pop(0)
            for v in d.values():
                if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
                    return _registros(v)
            fila.extend(v for v in d.values() if isinstance(v, dict))
        return _registros([data])  # resposta unica: 1 registro
    raise ValueError("A resposta JSON nao e uma lista nem um objeto")


def _unicos(nomes: list[str]) -> list[str]:
    vistos: dict[str, int] = {}
    out = []
    for i, n in enumerate(nomes, 1):
        n = n or f"coluna_{i}"
        vistos[n] = vistos.get(n, 0) + 1
        out.append(n if vistos[n] == 1 else f"{n}_{vistos[n]}")
    return out


def linhas_tabela(html: str, seletor: str = "", base_url: str = "") -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    if seletor:
        achados = soup.select(seletor)
        tabelas = [e if e.name == "table" else e.find("table") for e in achados]
        tabelas = [t for t in tabelas if t is not None]
    else:
        tabelas = soup.find_all("table")
    if not tabelas:
        raise ValueError(
            f"Nenhuma tabela encontrada{f' com o seletor {seletor!r}' if seletor else ''} — "
            "se a pagina carrega os dados por JavaScript, marque 'página com JavaScript'"
        )
    tabela = max(tabelas, key=lambda t: len(t.find_all("tr")))
    cabecalho, linhas = None, []
    for tr in tabela.find_all("tr"):
        cels = tr.find_all(["th", "td"])
        textos = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in cels]
        if not any(textos):
            continue
        if cabecalho is None:
            cabecalho = _unicos(textos)
            continue
        linha = dict(zip(cabecalho, textos))
        for nome, c in zip(cabecalho, cels):
            a = c.find("a", href=True)
            if a and not a["href"].lower().startswith(("javascript:", "#", "mailto:")):
                linha[f"{nome} (link)"] = urljoin(base_url, a["href"])
        linhas.append(linha)
    return linhas


def _pagina(cfg: ColetaConfig, url: str) -> list[dict]:
    if cfg.modo == "tabela_html" and cfg.renderizar:
        return linhas_tabela(http_client.render(url), cfg.caminho, url)
    with http_client.new_client() as c:
        c.headers.update(_cabecalhos(cfg))
        resp = c.get(url)
        resp.raise_for_status()
        if cfg.modo == "json":
            return linhas_json(resp.json(), cfg.caminho)
        return linhas_tabela(resp.text, cfg.caminho, str(resp.url))


def buscar_linhas(cfg: ColetaConfig, max_paginas: int | None = None, pausa: float = 0) -> Iterator[dict]:
    paginado = "{page}" in cfg.url
    n = min(cfg.max_paginas, max_paginas or cfg.max_paginas) if paginado else 1
    anterior = None
    for i in range(n):
        linhas = _pagina(cfg, cfg.url.replace("{page}", str(cfg.pagina_inicial + i)))
        if not linhas:
            break
        assinatura = hashlib.sha1(
            json.dumps(linhas[:5], sort_keys=True, default=str).encode()
        ).hexdigest()
        if assinatura == anterior:
            break  # o site repete a ultima pagina em vez de vir vazio
        anterior = assinatura
        yield from linhas
        if paginado and i < n - 1 and pausa:
            time.sleep(pausa)


def testar(cfg: ColetaConfig, amostra: int = 15) -> dict:
    """Pre-visualizacao: busca so a 1a pagina e devolve colunas + primeiras linhas."""
    linhas = list(buscar_linhas(cfg, max_paginas=1))
    colunas: list[str] = []
    for r in linhas:
        for k in r:
            if k not in colunas:
                colunas.append(k)
    return {"colunas": colunas, "linhas": linhas[:amostra], "total_na_pagina": len(linhas)}
