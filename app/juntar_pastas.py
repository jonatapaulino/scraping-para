"""Junta as pastas de orgao duplicadas criadas antes da correcao do orgao_slug
(ex.: IDEFLORBIO + IDEFLOR_-_BIO -> IDEFLOR-BIO; 'FUND. SANTA CASA' ->
FUND_SANTA_CASA; nomes com acento quebrado como AG_NCIA_...).

Para cada pasta de 1o nivel em MAIUSCULAS (pastas de orgao dos datasets),
calcula o nome novo com o mesmo orgao_slug que o sistema usa agora e move os
arquivos para la, na pasta de downloads, atualizando o banco
(documento.minio_path). O rotulo antigo dentro do nome do CSV tambem e trocado.

Seguro: so SIMULA, a menos que receba --aplicar. Nunca sobrescreve: se o
destino ja existe com conteudo DIFERENTE, o arquivo fica onde esta e entra no
relatorio de conflitos. Destino identico = so remove a copia antiga.

Uso (com o sistema no ar):
  docker compose exec worker-download python -m app.juntar_pastas            # simula
  docker compose exec worker-download python -m app.juntar_pastas --aplicar  # executa
"""
import argparse
import hashlib
import re
import sys
from collections import defaultdict

from sqlalchemy import select, update

from app import config, storage, transparencia
from app.database import SessionLocal
from app.models import Documento
from app.routers import _parse_unidade


def _sha(key: str) -> str:
    h = hashlib.sha256()
    with storage.abrir(key) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _quebrado(nome: str) -> str:
    """Reproduz o nome que a versao antiga gerava: letra acentuada virava '_'
    (e some na chave) e o nome era cortado em 50 caracteres."""
    s = re.sub(r"[^A-Z0-9._-]+", "_", str(nome).upper()).strip("_")[:50]
    return re.sub(r"[^A-Z0-9]", "", s)


def _mapa_legado() -> dict[str, str]:
    """chave do nome quebrado -> sigla oficial (so as sem ambiguidade)."""
    vistos: dict[str, set] = defaultdict(set)
    for nome, sigla in transparencia.nomes_oficiais():
        for rotulo in [nome, *(k for k, v in transparencia.APELIDOS.items() if v == sigla)]:
            if rotulo:
                vistos[_quebrado(rotulo)].add(sigla)
    return {k: next(iter(v)) for k, v in vistos.items() if len(v) == 1}


def _limpa_vazias() -> None:
    import os

    raiz = str(storage.raiz())
    for dirpath, _dirs, _files in os.walk(raiz, topdown=False):
        if dirpath != raiz:
            try:
                os.rmdir(dirpath)  # so remove se estiver vazia
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="executa (sem isso, so simula)")
    aplicar = ap.parse_args().aplicar

    if not transparencia._sigla_canon():
        print("ERRO: a API de orgaos (despesas/empenhos) nao respondeu — sem ela nao da "
              "para saber a sigla oficial. Tente de novo em instantes.")
        return 2

    # rotulo ORIGINAL do orgao de cada arquivo (com acentos), tirado da chave do
    # documento — o nome da pasta antiga as vezes ja perdeu o acento (AG_NCIA)
    rotulo_de: dict[str, str] = {}
    with SessionLocal() as s:
        for url, mp in s.execute(
            select(Documento.url_origem, Documento.minio_path).where(
                Documento.url_origem.like("transparencia://%"),
                Documento.minio_path.is_not(None),
            )
        ):
            _, org = _parse_unidade(url)
            if org and "+" not in org and "=" not in org:
                rotulo_de[storage.chave(mp)] = org

    legado = _mapa_legado()
    planos: dict[str, list[tuple[str, str]]] = defaultdict(list)  # pasta -> [(chave, nova)]
    for key in storage.listar():
        topo, _, resto = key.partition("/")
        if topo != topo.upper() or not resto:  # crawl (sigla minuscula)/datasets sem orgao
            continue
        rotulo = rotulo_de.get(key)
        if rotulo is None:
            # sem registro no banco (ex.: PDFs anexos): tenta reconhecer o nome
            # antigo com acento quebrado (DEPARTAMENTO_DE_TR_NSITO_... -> DETRAN)
            rotulo = legado.get(_quebrado(topo), topo)
        novo_topo = transparencia.orgao_slug(rotulo)
        pasta, _, nome = resto.rpartition("/")
        # CSV dos datasets: <dataset>-<recorte>-<ROTULO>.csv -> troca o rotulo
        partes = pasta.split("/")
        if nome.lower().endswith(".csv") and len(partes) == 2:
            prefixo = f"{partes[0]}-{partes[1]}-"
            if nome.startswith(prefixo):
                nome = f"{prefixo}{novo_topo}.csv"
        novo_key = f"{novo_topo}/{pasta}/{nome}" if pasta else f"{novo_topo}/{nome}"
        if novo_key != key:
            planos[topo].append((key, novo_key))

    if not planos:
        print("Nada a juntar: todas as pastas de orgao ja estao no nome padrao.")
        return 0

    destinos = defaultdict(set)
    for topo, itens in planos.items():
        for _, novo_key in itens:
            destinos[novo_key.split("/", 1)[0]].add(topo)
    print(f"{'APLICANDO' if aplicar else 'SIMULACAO (nada sera alterado)'} — "
          f"{len(planos)} pastas -> {len(destinos)} destinos\n")
    for novo, antigos in sorted(destinos.items()):
        print(f"  {novo:<40} <- {', '.join(sorted(antigos))}")
    print()

    movidos = removidos = 0
    conflitos = []
    origem_de: dict[str, str] = {}
    for topo, itens in sorted(planos.items()):
        for key, novo_key in itens:
            # destino ja ocupado: na pasta, ou (na simulacao) por outro arquivo
            # que este mesmo plano moveria antes para la
            ref = novo_key if storage.existe(novo_key) else origem_de.get(novo_key)
            if ref:
                if _sha(ref) != _sha(key):
                    conflitos.append((key, novo_key))
                    continue
                acao = "identico ao destino — copia antiga removida"
                removidos += 1
            else:
                acao = "movido"
                movidos += 1
                origem_de[novo_key] = key
            if aplicar:
                if acao == "movido":
                    storage.mover(key, novo_key)
                else:
                    storage.remover(key)
                with SessionLocal() as s:
                    s.execute(
                        update(Documento)
                        .where(Documento.minio_path == f"{config.MINIO_BUCKET}/{key}")
                        .values(minio_path=f"{config.MINIO_BUCKET}/{novo_key}")
                    )
                    s.commit()
            print(f"  [{acao}] {key} -> {novo_key}")

    if aplicar:
        _limpa_vazias()

    print(f"\nResumo: {movidos} movidos, {removidos} copias identicas removidas, "
          f"{len(conflitos)} conflitos (mantidos onde estao).")
    for antigo, novo in conflitos:
        print(f"  CONFLITO: {antigo}  (destino {novo} ja existe com conteudo diferente)")
    if not aplicar:
        print("\nNada foi alterado. Para executar: python -m app.juntar_pastas --aplicar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
