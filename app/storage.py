"""Armazenamento dos arquivos baixados: UMA copia, so na pasta de downloads.

Antes cada arquivo era gravado duas vezes (MinIO + downloads/), dobrando o
espaco em disco. Agora a pasta (ARQUIVOS_DIR, no host: DOWNLOADS_DIR) e o
unico lugar.

Chave = caminho relativo dentro da pasta ('SESPA/folha/2025-01/x.csv'). No
banco, documento.minio_path guarda 'raw-docs/<chave>' (formato antigo mantido
para nao migrar o historico) — use chave() para converter.
"""
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from app import config

log = logging.getLogger(__name__)


def raiz() -> Path:
    return Path(config.ARQUIVOS_DIR)


def chave(minio_path: str) -> str:
    """'raw-docs/a/b.pdf' -> 'a/b.pdf'."""
    return minio_path.split("/", 1)[1] if "/" in minio_path else minio_path


def caminho(key: str) -> Path:
    """Caminho no disco — recusa chaves que escapem da pasta ('../')."""
    base = raiz().resolve()
    p = (base / key).resolve()
    if base != p and base not in p.parents:
        raise ValueError(f"Caminho fora da pasta de arquivos: {key}")
    return p


def pronto(attempts: int = 1, delay: float = 2.0) -> bool:
    """A pasta existe e aceita escrita."""
    import time

    for i in range(attempts):
        try:
            raiz().mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=raiz(), prefix=".teste-"):
                pass
            return True
        except OSError as exc:
            if i == attempts - 1:
                log.error("Pasta de arquivos indisponivel (%s): %s", raiz(), exc)
                return False
            time.sleep(delay)
    return False


def salvar(origem, key: str) -> None:
    """Grava `origem` (arquivo binario aberto) em <pasta>/<key>. Escrita atomica:
    grava num temporario ao lado e renomeia — um arquivo pela metade (queda de
    energia, disco cheio) nunca fica com o nome final."""
    destino = caminho(key)
    destino.parent.mkdir(parents=True, exist_ok=True)
    origem.seek(0)
    fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=".parcial-")
    try:
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(origem, f, 1024 * 1024)
        # mkstemp cria com permissao 600 (so o dono le): os arquivos baixados
        # precisam ser legiveis por quem acessa a pasta (SSH, pagina de arquivos)
        os.chmod(tmp, 0o644)
        os.replace(tmp, destino)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    origem.seek(0)


def salvar_bytes(dados: bytes, key: str) -> None:
    import io

    salvar(io.BytesIO(dados), key)


def corrige_permissoes() -> int:
    """Torna legiveis (644/755) os arquivos e pastas gravados antes da correcao,
    que ficaram so para o dono (600). A pasta oculta .drive (autorizacao do Google)
    fica de fora: ela deve continuar restrita. Devolve quantos itens mudaram."""
    n = 0
    for dirpath, dirs, files in os.walk(raiz()):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for d in dirs:
            p = os.path.join(dirpath, d)
            try:
                if os.stat(p).st_mode & 0o755 != 0o755:
                    os.chmod(p, 0o755)
                    n += 1
            except OSError:
                pass
        for nome in files:
            if nome.startswith("."):
                continue
            p = os.path.join(dirpath, nome)
            try:
                if os.stat(p).st_mode & 0o644 != 0o644:
                    os.chmod(p, 0o644)
                    n += 1
            except OSError:
                pass
    return n


def tamanho(key: str) -> int | None:
    try:
        return caminho(key).stat().st_size
    except (OSError, ValueError):
        return None


def existe(key: str) -> bool:
    return tamanho(key) is not None


def abrir(key: str):
    return open(caminho(key), "rb")


def remover(key: str) -> None:
    try:
        caminho(key).unlink()
    except (OSError, ValueError):
        pass


def mover(de: str, para: str) -> None:
    destino = caminho(para)
    destino.parent.mkdir(parents=True, exist_ok=True)
    os.replace(caminho(de), destino)


def resumo() -> dict:
    """Quantidade e tamanho total dos arquivos guardados + espaco livre no disco."""
    n = total = 0
    for dirpath, dirs, files in os.walk(raiz()):
        dirs[:] = [d for d in dirs if not d.startswith(("_manutencao", "."))]
        for nome in files:
            if nome.startswith("."):
                continue
            try:
                total += os.stat(os.path.join(dirpath, nome)).st_size
                n += 1
            except OSError:
                pass
    try:
        livre = shutil.disk_usage(raiz()).free
    except OSError:
        livre = None
    return {"arquivos": n, "bytes": total, "livre_bytes": livre}


def listar(prefixo: str = "") -> Iterator[str]:
    """Chaves (caminhos relativos, com '/') de todos os arquivos sob `prefixo`.
    Ignora temporarios de escrita ('.parcial-*') e a pasta _manutencao."""
    base = raiz()
    inicio = caminho(prefixo) if prefixo else base
    if not inicio.exists():
        return
    for dirpath, dirs, files in os.walk(inicio):
        dirs[:] = [d for d in dirs if not d.startswith(("_manutencao", "."))]
        for nome in files:
            if nome.startswith((".parcial-", ".teste-")):
                continue
            yield Path(dirpath, nome).relative_to(base).as_posix()
