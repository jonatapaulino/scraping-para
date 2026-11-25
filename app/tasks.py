import csv
import hashlib
import io
import json
import logging
import os
from typing import Optional
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse, urlunparse

import httpx
import pika
from bs4 import BeautifulSoup
from celery import Task
from celery.signals import worker_ready
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError as SAOperationalError
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app import broker, coleta, config, drive, http_client, storage, transparencia
from app.celery_app import celery
from app.database import SessionLocal, init_db
from app.models import (
    ColetaFila,
    CrawledPage,
    DeadLetter,
    Documento,
    DownloadStatus,
    JobPending,
    JobStatus,
    ScrapingJob,
    Secretaria,
)

log = logging.getLogger(__name__)

SPOOL_MAX_BYTES = 64 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "#")

# urllib3: falhas de rede sao tao transientes quanto as de download — sem
# isso a task morria direto na DLX.
TRANSIENT_ERRORS = (
    httpx.HTTPError,
    SAOperationalError,
    ConnectionError,
    TimeoutError,
    Urllib3HTTPError,
    # resposta nao-JSON (ex.: pagina HTML de erro com status 200 — pods
    # derrubados no gateway) — transitiva por natureza
    json.JSONDecodeError,
)

DEAD_REQUEUEABLE = (
    "app.tasks.discover_page",
    "app.tasks.download_document",
    "app.tasks.discover_dataset",
    "app.tasks.fetch_dataset_mes",
    "app.tasks.coletar_fonte",
)


@worker_ready.connect
def _on_worker_ready(**_kwargs):
    # migracoes idempotentes tambem no worker: num rebuild ele pode subir antes
    # da API e gravar um valor novo (ex.: SEM_DADOS) que o banco ainda nao tem
    init_db(attempts=10, delay=3)
    storage.pronto(attempts=5, delay=3)
    conn = broker._connect()
    try:
        broker.declare_topology(conn.channel())
    finally:
        conn.close()


# ---------- helpers de job (contador em voo -> COMPLETED quando drena) ----------


def _inc_pending(session, job_id: uuid.UUID, n: int = 1) -> None:
    session.execute(
        update(JobPending)
        .where(JobPending.job_id == job_id)
        .values(pending=JobPending.pending + n)
    )


def finish_task_unit(session, job_id: uuid.UUID | None) -> None:
    """Decrementa o pendente; zerando, marca o job COMPLETED."""
    if job_id is None:
        return
    remaining = session.execute(
        update(JobPending)
        .where(JobPending.job_id == job_id)
        .values(pending=JobPending.pending - 1)
        .returning(JobPending.pending)
    ).scalar_one_or_none()
    if remaining is None:
        # job excluido enquanto a task estava na fila: nada a descontar (antes
        # dava 'No row was found' e a task ia para a fila de erros)
        log.info("Job %s nao existe mais — unidade ignorada", job_id)
        session.commit()
        return
    if remaining <= 0:
        session.execute(
            update(ScrapingJob)
            .where(ScrapingJob.id == job_id, ScrapingJob.status == JobStatus.RUNNING)
            .values(status=JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))
        )
    session.commit()


def _fecha_documento(session, documento_id: uuid.UUID, job_id: uuid.UUID | None, **values) -> None:
    """Grava o resultado final do documento e desconta a pendencia do job SO se
    ele ainda estava PENDING. Idempotente: reentrega da mensagem, reprocesso da
    fila de erros ou o reconciliador nao descontam o mesmo item duas vezes (antes
    o contador ficava negativo e o job aparecia concluido com trabalho rodando).
    O resultado e gravado mesmo se o reconciliador ja tinha marcado FAILED."""
    anterior = session.execute(
        select(Documento.status_download)
        .where(Documento.id == documento_id)
        .with_for_update()
    ).scalar_one_or_none()
    session.execute(update(Documento).where(Documento.id == documento_id).values(**values))
    if anterior == DownloadStatus.PENDING:
        finish_task_unit(session, job_id)  # faz o commit
    else:
        session.commit()


def _job_cancelado(job_id: uuid.UUID) -> bool:
    """True se o usuario cancelou o job pelo painel — as tasks restantes da fila
    viram no-op (nao baixam, nao enfileiram mais nada)."""
    with SessionLocal() as s:
        st = s.execute(
            select(ScrapingJob.status).where(ScrapingJob.id == job_id)
        ).scalar_one_or_none()
    return st == JobStatus.CANCELLED


def _effective_depth(secretaria, max_depth: int | None) -> int:
    """Precedencia: parametro do disparo > coluna da secretaria > default global."""
    if max_depth is not None:
        return max_depth
    if secretaria.max_depth is not None:
        return secretaria.max_depth
    return config.DISCOVERY_MAX_DEPTH


def start_job_for_secretaria(
    session, secretaria, force_full_scrape: bool, max_depth: int | None = None
) -> ScrapingJob:
    """Cria o job e enfileira a URL base na q.discovery. Chamado pela API e pelo beat."""
    job = ScrapingJob(secretaria_id=secretaria.id, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    session.add(JobPending(job_id=job.id, pending=1))
    session.commit()
    try:
        _enqueue_discover(
            secretaria.url_base, job.id, secretaria.id, 0, force_full_scrape,
            max_depth=_effective_depth(secretaria, max_depth),
        )
    except Exception:
        # Sem publish o job nunca drenaria — marca FAILED em vez de zumbi.
        log.exception("Falha ao enfileirar seed do job %s", job.id)
        session.execute(
            update(ScrapingJob)
            .where(ScrapingJob.id == job.id)
            .values(status=JobStatus.FAILED, completed_at=datetime.now(timezone.utc))
        )
        session.commit()
        raise
    return job


def _enqueue_discover(url, job_id, secretaria_id, depth, force, max_depth, render=False):
    discover_page.apply_async(
        args=[{
            "url": url,
            "job_id": str(job_id),
            "secretaria_id": str(secretaria_id),
            "depth": depth,
            "max_depth": max_depth,
            "render": render,
            "force": force,
        }]
    )


def _enqueue_download(url, job_id, secretaria_id, documento_id):
    download_document.apply_async(
        args=[{
            "url": url,
            "job_id": str(job_id),
            "secretaria_id": str(secretaria_id),
            "documento_id": str(documento_id),
        }]
    )


def _enqueue_compensate(session, job_id, enqueue_fn, on_fail):
    """Publica e, se a publicacao falhar, desfaz o incremento de pendente."""
    try:
        enqueue_fn()
    except Exception:
        log.exception("Falha ao enfileirar — compensando pending")
        on_fail()
        finish_task_unit(session, job_id)


class DeadLetterTask(Task):
    """Ao esgotar os retries, publica o payload na DLX para analise manual."""

    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        payload = args[0] if args else kwargs
        log.error("Task %s morta apos retries: %s", self.name, exc)
        dead = {"task": self.name, "payload": payload}
        if not broker.send_to_dead(dead, str(exc)):
            # Broker fora do ar — persiste local para nao perder o dead letter
            try:
                with SessionLocal() as s:
                    s.add(
                        DeadLetter(
                            task=self.name,
                            payload=json.dumps(payload, default=str),
                            reason=str(exc),
                        )
                    )
                    s.commit()
            except Exception:
                log.exception("Falha ao persistir dead letter local")
        job_id = (payload or {}).get("job_id") if isinstance(payload, dict) else None
        doc_id = (payload or {}).get("documento_id") if isinstance(payload, dict) else None
        try:
            with SessionLocal() as s:
                if doc_id:
                    _fecha_documento(
                        s, uuid.UUID(doc_id), job_id,
                        status_download=DownloadStatus.FAILED,
                    )
                elif job_id:
                    finish_task_unit(s, job_id)  # unidade de pagina (sem documento)
        except Exception:
            log.exception("Falha ao fechar unidade morta (job=%s doc=%s)", job_id, doc_id)


# ---------- extracao de links ----------

_TRACKING_RE = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$)", re.I)


def normalize_url(url: str) -> str:
    """Canoniza para dedup: host/scheme minusculo, porta default fora,
    query params ordenados e sem trackers — sem decodificar valores
    (evita corromper '+' legitimo em querystrings malformadas)."""
    p = urlparse(url)
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    pairs = [
        kv for kv in p.query.split("&")
        if kv and not _TRACKING_RE.match(kv.split("=", 1)[0])
    ]
    return urlunparse(
        (scheme, netloc, p.path or "/", p.params, "&".join(sorted(pairs)), "")
    )


def _page_key(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def _claim_page(session, url: str, job_id: uuid.UUID) -> bool:
    """Tenta reclamar a pagina p/ este crawl. True = pode raspar (nova ou
    claim expirada); False = outro job/worker raspou dentro da janela."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.CRAWL_DEDUP_HOURS)
    claimed = session.execute(
        pg_insert(CrawledPage)
        .values(url_hash=_page_key(url), url=url, job_id=job_id)
        .on_conflict_do_update(
            index_elements=["url_hash"],
            set_={"crawled_at": func.now(), "job_id": job_id},
            where=CrawledPage.crawled_at < cutoff,
        )
        .returning(CrawledPage.url_hash)
    ).scalar_one_or_none()
    return claimed is not None


def extract_links(
    page_url: str, html: str, pdf_filtro: str | None = None
) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    docs, pages = [], []
    base_netloc = urlparse(normalize_url(page_url)).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(SKIP_SCHEMES):
            continue
        abs_url, _ = urldefrag(urljoin(page_url, href))
        if urlparse(abs_url).scheme not in ("http", "https"):
            continue
        abs_url = normalize_url(abs_url)
        # .pdf no path OU na query (ex: arquivo.php?arq=/doc.pdf) — magic bytes valida falso-positivo
        if ".pdf" in abs_url.lower():
            if _pdf_allow(abs_url, pdf_filtro):
                docs.append(abs_url)
            # fora da whitelist: descarta — PDF nao e pagina navegavel e
            # enfileirar como discover so queimaria uma task a toa
        elif urlparse(abs_url).netloc == base_netloc:
            pages.append(abs_url)
    return docs, pages


def _pdf_allow(url: str, filtro: str | None = None) -> bool:
    """Whitelist do crawl. `filtro` e o da fonte (None = padrao global
    CRAWL_PDF_ALLOW, so material financeiro; "" = baixa tudo)."""
    allow = config.CRAWL_PDF_ALLOW if filtro is None else filtro
    if not allow:
        return True
    return re.search(allow, unquote(url), re.IGNORECASE) is not None


def _filename_from_url(url: str) -> str:
    """Extrai o nome do PDF do path ou de um param da query; fallback por hash."""
    parsed = urlparse(url)
    candidates = [parsed.path] + [
        v for vals in parse_qs(parsed.query).values() for v in vals
    ]
    for cand in candidates:
        name = unquote(cand).rsplit("/", 1)[-1]
        if name.lower().endswith(".pdf"):
            return re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return f"doc-{hashlib.sha256(url.encode()).hexdigest()[:8]}.pdf"


# Pasta de ano no path: '2024', '2023-01' (Drupal), '2023_05', '2024.03.15'
_ANO_PASTA_RE = re.compile(r"^((?:19|20)\d{2})(?:[-_.]\d{1,2}){0,2}$")
_ANO_NOME_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _ano_do_documento(url: str, filename: str) -> str:
    for part in urlparse(url).path.split("/"):
        m = _ANO_PASTA_RE.match(unquote(part))
        if m:
            return m.group(1)
    m = _ANO_NOME_RE.search(filename)
    return m.group(1) if m else "unknown"


def _minio_path(sigla: str, url: str) -> str:
    """bucket/<sigla>/<ano>/<nome>__<tag>.pdf — a tag (hash da URL) garante um
    caminho por URL: dois PDFs com o mesmo nome no mesmo ano (ex.: relatorio
    mensal de nome fixo) nao se sobrescrevem mais. Deterministico: a mesma URL
    sempre cai no mesmo caminho (retries e workers paralelos nao colidem)."""
    filename = _filename_from_url(url)
    year = _ano_do_documento(url, filename)
    if not filename.startswith("doc-"):  # fallback ja e derivado do hash da URL
        stem, ext = os.path.splitext(filename)
        # nome curto: ha portais com nome de arquivo de 300+ caracteres (o
        # banco recusava e o Windows tem limite de ~260 no caminho inteiro).
        # A tag mantem o nome unico mesmo cortado.
        stem = stem[:100].rstrip("._-") or "doc"
        tag = hashlib.sha256(url.encode()).hexdigest()[:8]
        filename = f"{stem}__{tag}{ext or '.pdf'}"
    return f"{config.MINIO_BUCKET}/{sigla.lower()}/{year}/{filename}"


def _minio_path_antigo(sigla: str, url: str) -> str:
    """Caminho que a versao ANTERIOR gerava (sem o sufixo da URL) — os arquivos
    baixados antes da mudanca continuam com esse nome."""
    filename = _filename_from_url(url)
    year = "unknown"
    for part in urlparse(url).path.split("/"):
        if part.isdigit() and len(part) == 4:
            year = part
            break
    if year == "unknown":
        m = re.search(r"20\d{2}", filename)
        if m:
            year = m.group(0)
    return f"{config.MINIO_BUCKET}/{sigla.lower()}/{year}/{filename}"


def _cabecalho_remoto(url: str) -> tuple[int | None, datetime | None]:
    """Tamanho e data de modificacao informados pelo site, SEM baixar o arquivo
    (requisicao HEAD). (None, None) se o site nao informa ou recusa HEAD."""
    try:
        with http_client.new_client(timeout=15) as c:
            resp = c.head(url)
        if resp.status_code >= 400:
            return None, None
        tam = resp.headers.get("content-length")
        tam = int(tam) if tam and tam.isdigit() else None
        mod = resp.headers.get("last-modified")
        try:
            mod = parsedate_to_datetime(mod) if mod else None
            if mod is not None and mod.tzinfo is None:
                mod = mod.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            mod = None
        return tam, mod
    except httpx.HTTPError:
        return None, None


def _tamanho_guardado(path: str) -> int | None:
    """Tamanho do arquivo guardado ('raw-docs/<chave>'); None se nao existe."""
    return storage.tamanho(storage.chave(path))


def _ja_guardado(url, documento_id, job_id, sigla, caminho_proprio, tam_anterior, baixado_em) -> bool:
    """Evita baixar de novo um PDF que ja esta guardado. True = resolvido sem download.

    1. Re-download forcado de um doc que ja tem arquivo: se o site informa o
       MESMO tamanho e uma data de modificacao ANTERIOR ao nosso download, o
       arquivo nao mudou — so confirma.
    2. Doc novo no banco (ex.: fonte excluida e cadastrada de novo), mas o
       arquivo da mesma URL ja esta no armazenamento com o tamanho que o site
       informa — registra sem baixar.
    Sem os cabecalhos (site nao informa), baixa normalmente: na duvida, baixa."""
    if caminho_proprio:
        if not (tam_anterior and baixado_em):
            return False
        tam, mod = _cabecalho_remoto(url)
        if tam != tam_anterior or mod is None or mod > baixado_em:
            return False
        if _tamanho_guardado(caminho_proprio) != tam:
            return False  # arquivo sumiu do armazenamento: baixa de novo
        with SessionLocal() as s:
            _fecha_documento(s, documento_id, job_id, status_download=DownloadStatus.SUCCESS)
        log.info("Sem mudanca no site (tamanho/data iguais) — nao baixado: %s", url)
        return True

    # mais barato primeiro: so consulta o site se ja existe algo guardado
    candidatos = [
        (k, t) for k in dict.fromkeys((_minio_path(sigla, url), _minio_path_antigo(sigla, url)))
        if (t := _tamanho_guardado(k)) is not None
    ]
    if not candidatos:
        return False
    tam, _mod = _cabecalho_remoto(url)
    if tam is None:
        return False
    for key, t in candidatos:
        if t == tam:
            with SessionLocal() as s:
                _fecha_documento(
                    s, documento_id, job_id,
                    minio_path=key, tamanho_bytes=tam, magic_byte_valido=True,
                    status_download=DownloadStatus.SUCCESS,
                    downloaded_at=datetime.now(timezone.utc),
                )
            log.info("Ja estava guardado (%s) — nao baixado: %s", key, url)
            return True
    return False


def _fallback_url(url: str, base_url: str | None) -> str | None:
    """Mesmo path/query no host-base da secretaria — cobre links que apontam pra intranet."""
    if not base_url:
        return None
    parsed, base = urlparse(url), urlparse(base_url)
    if not parsed.netloc or parsed.netloc == base.netloc:
        return None
    return parsed._replace(scheme=base.scheme or parsed.scheme, netloc=base.netloc).geturl()


def _baixa_doc_pdf(dataset: str, doc: dict, seen: Optional[dict] = None) -> bool:
    """Baixa um PDF de anexo do dataset para a pasta de arquivos.
    doc: {'url', 'orgao', 'sub', 'nome'}. Pula se o arquivo ja existe ou
    se o conteudo ja foi gravado na mesma pasta (docs duplicados — ex.:
    termo e plano de trabalho apontando pro mesmo binario)."""
    pasta = f"{doc['orgao']}/{dataset}/documentos/{doc.get('sub') or '_avulso'}"
    rel = f"{pasta}/{doc['nome']}"
    if storage.existe(rel):
        return False
    with http_client.new_client() as c:
        resp = c.get(doc["url"])
        resp.raise_for_status()
        body = resp.content
    if not body.startswith(b"%PDF-"):
        log.warning("Doc %s nao e PDF (magic bytes) — ignorado", doc["url"])
        return False
    if seen is not None:
        sha = hashlib.sha256(body).hexdigest()
        hashes = seen.setdefault(pasta, set())
        if sha in hashes:
            return False
        hashes.add(sha)
    storage.salvar_bytes(body, rel)
    return True


def _stream_to_tmp(client: httpx.Client, url: str, tmp, sha) -> tuple[int, bytes, bool]:
    """Baixa `url` em tmp. Devolve (tamanho, primeiros bytes, magic_truncado)."""
    size = 0
    first = b""
    truncated = False
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes(256 * 1024):
            if len(first) < len(PDF_MAGIC):
                first += chunk[: len(PDF_MAGIC) - len(first)]
                if first != PDF_MAGIC[: len(first)]:
                    truncated = True
                    break
            sha.update(chunk)
            tmp.write(chunk)
            size += len(chunk)
    return size, first, truncated


class _Cancelado(Exception):
    """O usuario cancelou o job no meio de uma coleta longa."""


def _linhas_para_csv(linhas, tmp, job_id: uuid.UUID) -> tuple[int, int]:
    """Grava `linhas` (dicts) como CSV UTF-8 com BOM no arquivo binario `tmp`.

    As colunas sao a UNIAO de todas as linhas, na ordem em que aparecem (um
    campo que so surge na linha 5.000 nao e mais descartado). Como o
    cabecalho so e conhecido no fim, as linhas passam antes por um arquivo
    temporario (uma linha JSON cada). Cancelamento checado a cada 2000 linhas.
    Devolve (total_de_linhas, total_de_colunas)."""
    colunas: list[str] = []
    conhecidas: set[str] = set()
    total = 0
    with tempfile.TemporaryFile("w+", encoding="utf-8") as brutas:
        for linha in linhas:
            brutas.write(json.dumps(linha, ensure_ascii=False, default=str) + "\n")
            total += 1
            for k in linha:
                if k not in conhecidas:
                    conhecidas.add(k)
                    colunas.append(k)
            if total % 2000 == 0 and _job_cancelado(job_id):
                raise _Cancelado()
        if total == 0:
            return 0, 0
        brutas.seek(0)
        texto = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="", write_through=True)
        w = csv.DictWriter(texto, fieldnames=colunas, restval="")
        w.writeheader()
        for ln in brutas:
            w.writerow(json.loads(ln))
        texto.flush()
        texto.detach()  # solta o wrapper sem fechar `tmp`
    return total, len(colunas)


def _sha_e_tamanho(tmp) -> tuple[str, int]:
    tmp.seek(0)
    sha = hashlib.sha256()
    for chunk in iter(lambda: tmp.read(256 * 1024), b""):
        sha.update(chunk)
    size = tmp.tell()
    tmp.seek(0)
    return sha.hexdigest(), size


def _publica_arquivo(tmp, path: str, size: int, content_type: str) -> None:
    """Grava na pasta de arquivos (unica copia) + Drive, se ligado (best-effort)."""
    key = storage.chave(path)
    storage.salvar(tmp, key)
    drive.upload_file(tmp, key, content_type)


# ---------- tasks ----------


@celery.task(
    base=DeadLetterTask,
    bind=True,
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=config.RETRY_BASE_SECONDS,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=config.MAX_RETRIES,
)
def discover_page(self, payload: dict):
    url = payload["url"]
    job_id = uuid.UUID(payload["job_id"])
    secretaria_id = uuid.UUID(payload["secretaria_id"])
    depth = int(payload.get("depth", 0))
    max_depth = int(payload.get("max_depth", config.DISCOVERY_MAX_DEPTH))
    force = bool(payload.get("force", False))
    if _job_cancelado(job_id):
        log.info("Job %s cancelado — descartando %s", job_id, url)
        return
    log.info("Discovery %s (depth=%d/%d)", url, depth, max_depth)

    try:
        html = http_client.fetch_html(url, force_render=bool(payload.get("render", False)))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if 400 <= code < 500 and code not in (408, 429):
            log.warning("Pagina %d terminal: %s", code, url)
            with SessionLocal() as s:
                finish_task_unit(s, job_id)
            return
        raise
    with SessionLocal() as s:
        pdf_filtro = s.execute(
            select(Secretaria.pdf_filtro).where(Secretaria.id == secretaria_id)
        ).scalar_one_or_none()
    docs, pages = extract_links(url, html, pdf_filtro)

    doc_payloads, page_payloads = [], []
    with SessionLocal() as s:
        for doc_url in docs:
            doc_id = s.execute(
                pg_insert(Documento)
                .values(
                    secretaria_id=secretaria_id,
                    job_id=job_id,
                    url_origem=doc_url,
                    status_download=DownloadStatus.PENDING,
                )
                .on_conflict_do_nothing(index_elements=["url_origem"])
                .returning(Documento.id)
            ).scalar_one_or_none()
            if doc_id is None and force:
                # created_at = agora: senao o reconciliador ve o doc re-enfileirado
                # como orfao antigo (>DOC_ORPHAN_HOURS) e o mata no meio da fila
                doc_id = s.execute(
                    update(Documento)
                    .where(Documento.url_origem == doc_url)
                    .values(job_id=job_id, status_download=DownloadStatus.PENDING,
                            created_at=func.now())
                    .returning(Documento.id)
                ).scalar_one_or_none()
            if doc_id is None:
                continue
            _inc_pending(s, job_id)
            doc_payloads.append((doc_url, doc_id))

        if doc_payloads:
            s.execute(
                update(ScrapingJob)
                .where(ScrapingJob.id == job_id)
                .values(total_pdfs_encontrados=ScrapingJob.total_pdfs_encontrados + len(doc_payloads))
            )

        if depth < max_depth:
            for page_url in pages:
                if not force and not _claim_page(s, page_url, job_id):
                    continue
                _inc_pending(s, job_id)
                page_payloads.append(page_url)

        _claim_page(s, url, job_id)  # marca/atualiza a pagina atual
        finish_task_unit(s, job_id)
        s.commit()

        for doc_url, doc_id in doc_payloads:
            _enqueue_compensate(
                s,
                job_id,
                lambda u=doc_url, d=doc_id: _enqueue_download(u, job_id, secretaria_id, d),
                lambda u=doc_url: s.execute(
                    update(Documento)
                    .where(Documento.url_origem == u)
                    .values(status_download=DownloadStatus.FAILED)
                ),
            )
        for page_url in page_payloads:
            _enqueue_compensate(
                s,
                job_id,
                lambda u=page_url: _enqueue_discover(
                    u, job_id, secretaria_id, depth + 1, force, max_depth
                ),
                lambda: None,
            )

    log.info("Discovery %s -> %d pdfs, %d paginas", url, len(doc_payloads), len(page_payloads))
    time.sleep(config.DISCOVERY_DELAY_SECONDS)


@celery.task(
    base=DeadLetterTask,
    bind=True,
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=config.RETRY_BASE_SECONDS,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=config.MAX_RETRIES,
)
def download_document(self, payload: dict):
    url = payload["url"]
    job_id = uuid.UUID(payload["job_id"])
    documento_id = uuid.UUID(payload["documento_id"])

    if _job_cancelado(job_id):
        log.info("Job %s cancelado — descartando download %s", job_id, url)
        return

    with SessionLocal() as s:
        doc = s.get(Documento, documento_id)
        if doc is None:
            log.error("Documento %s nao encontrado", documento_id)
            finish_task_unit(s, job_id)
            return
        if doc.status_download == DownloadStatus.SUCCESS:
            # reentrega de uma task que ja concluiu: a pendencia ja foi descontada
            log.info("Documento ja baixado: %s", url)
            return
        sec = s.get(Secretaria, doc.secretaria_id)
        base_url = sec.url_base if sec else None
        sigla = sec.sigla if sec else "unknown"
        doc_secretaria_id = doc.secretaria_id
        # re-download (force) de um doc que ja gravou o proprio arquivo: mantem o
        # caminho. Docs dedup (hash NULL) apontam pro arquivo de OUTRO doc — nao
        # podem reusar o caminho, senao sobrescreveriam o arquivo alheio.
        caminho_proprio = (
            doc.minio_path
            if doc.minio_path and (doc.hash_sha256 or doc.minio_path == _minio_path(sigla, url))
            else None
        )
        tam_anterior, baixado_em = doc.tamanho_bytes, doc.downloaded_at

    # ---- atalhos: nao baixa de novo o que ja esta guardado ----
    if _ja_guardado(
        url, documento_id, job_id, sigla, caminho_proprio, tam_anterior, baixado_em
    ):
        return

    sha = hashlib.sha256()
    size = 0
    first = b""
    truncated_magic = False
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as tmp:
        try:
            with http_client.new_client() as c:
                try:
                    size, first, truncated_magic = _stream_to_tmp(c, url, tmp, sha)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    alt = _fallback_url(url, base_url) if 400 <= code < 500 else None
                    if alt is None:
                        raise
                    log.info("HTTP %d — tentando mesmo path no host-base: %s", code, alt)
                    tmp.seek(0)
                    tmp.truncate(0)
                    sha = hashlib.sha256()
                    size, first, truncated_magic = _stream_to_tmp(c, alt, tmp, sha)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500 and code not in (408, 429):
                with SessionLocal() as s:
                    _fecha_documento(
                        s, documento_id, job_id, status_download=DownloadStatus.FAILED
                    )
                log.warning("Download %d terminal (link quebrado): %s", code, url)
                return
            raise
        if truncated_magic or len(first) < len(PDF_MAGIC):
            with SessionLocal() as s:
                _fecha_documento(
                    s, documento_id, job_id,
                    magic_byte_valido=False, status_download=DownloadStatus.FAILED,
                )
            log.warning("Magic bytes invalidos (nao-PDF): %s", url)
            return

        tmp.flush()
        tmp.seek(0)
        digest = sha.hexdigest()

        with SessionLocal() as s:
            dupe = s.execute(
                select(Documento).where(
                    Documento.hash_sha256 == digest, Documento.id != documento_id
                )
            ).scalar_one_or_none()
            if dupe and dupe.secretaria_id == doc_secretaria_id:
                _fecha_documento(
                    s, documento_id, job_id,
                    magic_byte_valido=True,
                    minio_path=dupe.minio_path,
                    tamanho_bytes=size,
                    status_download=DownloadStatus.SUCCESS,
                    downloaded_at=datetime.now(timezone.utc),
                )
                log.info("Binario duplicado de %s — reusa %s", url, dupe.minio_path)
                return
        # Mesmo binario ja existe em OUTRA fonte: grava copia propria na pasta
        # desta fonte (senao a pasta dela fica sem o arquivo). hash_sha256 e
        # UNIQUE global, entao a copia fica sem hash (o dono do hash e o outro).
        guarda_hash = dupe is None

        path = caminho_proprio or _minio_path(sigla, url)

        _publica_arquivo(tmp, path, size, "application/pdf")

        with SessionLocal() as s:
            try:
                _fecha_documento(
                    s, documento_id, job_id,
                    hash_sha256=digest if guarda_hash else None,
                    minio_path=path,
                    tamanho_bytes=size,
                    magic_byte_valido=True,
                    status_download=DownloadStatus.SUCCESS,
                    downloaded_at=datetime.now(timezone.utc),
                )
            except IntegrityError:
                # Outro worker gravou o mesmo binario primeiro (UNIQUE hash_sha256):
                # vira dedup em vez de morrer na DLX.
                s.rollback()
                dupe = s.execute(
                    select(Documento).where(
                        Documento.hash_sha256 == digest, Documento.id != documento_id
                    )
                ).scalar_one_or_none()
                if dupe is None:
                    raise
                if dupe.secretaria_id != doc_secretaria_id:
                    # vencedor e de outra fonte: mantem a copia propria, sem hash
                    _fecha_documento(
                        s, documento_id, job_id,
                        minio_path=path, tamanho_bytes=size, magic_byte_valido=True,
                        status_download=DownloadStatus.SUCCESS,
                        downloaded_at=datetime.now(timezone.utc),
                    )
                    log.info("Download ok %s (copia; hash pertence a outra fonte)", url)
                    return
                _fecha_documento(
                    s, documento_id, job_id,
                    magic_byte_valido=True,
                    minio_path=dupe.minio_path,
                    tamanho_bytes=size,
                    status_download=DownloadStatus.SUCCESS,
                    downloaded_at=datetime.now(timezone.utc),
                )
                if path != dupe.minio_path:
                    storage.remover(storage.chave(path))  # copia que sobrou da corrida
                log.info("Upload duplicado descartado — reusa %s", dupe.minio_path)
                return
        log.info("Download ok %s (%d bytes) -> %s", url, size, storage.chave(path))

    time.sleep(config.DOWNLOAD_DELAY_SECONDS)


@celery.task
def dispatch_scheduled():
    """Disparado pelo Celery Beat: cria um job por secretaria ativa."""
    with SessionLocal() as s:
        ativas = s.execute(
            select(Secretaria).where(
                Secretaria.is_ativa.is_(True), Secretaria.tipo == "crawl",
                Secretaria.agendada.is_(False),
            )
        ).scalars().all()
        ok = 0
        for sec in ativas:
            try:
                start_job_for_secretaria(s, sec, force_full_scrape=False)
                ok += 1
            except Exception:
                log.exception("Beat: falha ao disparar job para %s", sec.sigla)
        log.info("Beat: %d/%d jobs disparados", ok, len(ativas))
        coletas = s.execute(
            select(Secretaria).where(
                Secretaria.is_ativa.is_(True), Secretaria.tipo == "coleta",
                Secretaria.agendada.is_(False),
            )
        ).scalars().all()
        for sec in coletas:
            try:
                start_coleta_job(s, sec)
            except Exception:
                log.exception("Beat: falha ao disparar coleta de %s", sec.sigla)
    return ok


def _slot_agenda(agora_utc: datetime, hora: str) -> datetime:
    """Horario de HOJE (em UTC) da agenda `hora` (HH:MM no fuso configurado)."""
    off = timedelta(hours=config.AGENDA_UTC_OFFSET_HORAS)
    local = agora_utc + off
    h, m = (int(x) for x in hora.split(":"))
    return local.replace(hour=h, minute=m, second=0, microsecond=0) - off


@celery.task
def dispatch_agendadas(agora: str | None = None):
    """Beat (a cada 5 min): dispara as fontes que o usuario agendou no painel.

    Uma fonte roda quando o horario de hoje ja passou, nenhuma tarefa dela
    comecou desde esse horario e a agenda ja estava ligada nele — assim, se o
    computador estava desligado na hora, ela roda ao voltar (uma vez so)."""
    agora_utc = datetime.fromisoformat(agora) if agora else datetime.now(timezone.utc)
    disparadas = 0
    with SessionLocal() as s:
        fontes = s.execute(
            select(Secretaria).where(
                Secretaria.agendada.is_(True),
                Secretaria.is_ativa.is_(True),
                Secretaria.tipo.in_(("crawl", "coleta"))
                | (Secretaria.sigla == "IOEPA"),  # Diario Oficial: fonte "virtual"
            )
        ).scalars().all()
        for sec in fontes:
            slot = _slot_agenda(agora_utc, sec.agenda_hora or "06:00")
            if agora_utc < slot:
                continue  # ainda nao deu a hora de hoje
            if sec.agendada_em and sec.agendada_em > slot:
                continue  # agenda ligada depois do horario de hoje: comeca amanha
            # o job do Diario fica sob a fonte TRANSPARENCIA (tipo 'diario_oficial')
            filtro_job = (
                ScrapingJob.tipo == "diario_oficial"
                if sec.sigla == "IOEPA"
                else ScrapingJob.secretaria_id == sec.id
            )
            ja_rodou = s.execute(
                select(func.count(ScrapingJob.id)).where(filtro_job, ScrapingJob.started_at >= slot)
            ).scalar_one()
            if ja_rodou:
                continue
            try:
                if sec.sigla == "IOEPA":
                    # so o ano corrente; edicoes ja baixadas sao puladas (force = ignora o dedup do periodo)
                    ano = (agora_utc + timedelta(hours=config.AGENDA_UTC_OFFSET_HORAS)).year
                    start_dataset_job(s, "diario_oficial", ano, 1, ano, 12, None, force=True)
                elif sec.tipo == "coleta":
                    start_coleta_job(s, sec)
                else:
                    start_job_for_secretaria(s, sec, force_full_scrape=False)
                disparadas += 1
                log.info("Agenda: %s disparada (horario %s)", sec.sigla, sec.agenda_hora)
            except Exception:
                log.exception("Agenda: falha ao disparar %s", sec.sigla)
    return disparadas


@celery.task
def dispatch_datasets_scheduled(grupo: str):
    """Beat: coleta automatica de dados abertos.

    grupo 'diario'  -> AUTO_DIARIO_DATASETS (ano corrente; PDFs ja baixados sao pulados)
    grupo 'mensais' -> AUTO_MENSAIS_DATASETS (mes corrente + anterior, p/ pegar a
                       competencia que acabou de ser publicada)
    force=True: o dedup por (dataset x periodo) bloquearia a re-coleta; os arquivos
    ja existentes sao pulados e o CSV so e regravado se o conteudo mudou."""
    agora = datetime.now(timezone.utc)
    ano, mes = agora.year, agora.month
    ant_ano, ant_mes = (ano, mes - 1) if mes > 1 else (ano - 1, 12)
    slugs = (
        config.AUTO_DIARIO_DATASETS if grupo == "diario" else config.AUTO_MENSAIS_DATASETS
    )
    # convenios/licitacoes/diario nao filtram por sigla dos 5 orgaos
    sem_filtro = {"diario_oficial", "transf_voluntarias", "licitacoes", "bens_imoveis"}
    ok = 0
    with SessionLocal() as s:
        for slug in slugs:
            spec = transparencia.DATASETS.get(slug)
            if spec is None:
                log.warning("Auto: dataset desconhecido %r", slug)
                continue
            orgaos = None if slug in sem_filtro else (config.AUTO_ORGAOS or None)
            if spec.granularity == "mes":
                ini = (ant_ano, ant_mes)
            elif spec.granularity == "ano":
                ini = (ano, 1)
            else:
                ini = (ano, mes)
            try:
                start_dataset_job(
                    s, slug, ini[0], ini[1], ano, mes, orgaos, force=True,
                )
                ok += 1
            except Exception:
                log.exception("Auto: falha ao disparar %s", slug)
    log.info("Auto (%s): %d/%d datasets disparados", grupo, ok, len(slugs))
    return ok


_FIM_DA_FILA = {
    JobStatus.COMPLETED: "CONCLUIDA",
    JobStatus.FAILED: "FALHOU",
    JobStatus.CANCELLED: "CANCELADA",
}


@celery.task
def avancar_fila():
    """Beat (a cada minuto) + logo apos o painel enfileirar: roda a fila de
    coletas UMA por vez. Se a atual terminou, registra o resultado e dispara a
    proxima. Fica no worker-control (1 processo): nunca duas ao mesmo tempo."""
    agora = datetime.now(timezone.utc)
    with SessionLocal() as s:
        atual = s.execute(
            select(ColetaFila).where(ColetaFila.status == "RODANDO").order_by(ColetaFila.id).limit(1)
        ).scalar_one_or_none()
        if atual is not None:
            job = s.get(ScrapingJob, atual.job_id) if atual.job_id else None
            if job is not None and job.status == JobStatus.RUNNING:
                return "aguardando a coleta atual"
            atual.status = _FIM_DA_FILA.get(job.status, "FALHOU") if job else "FALHOU"
            atual.concluida_em = agora
            s.commit()
            log.info("Fila: item %d (%s) terminou: %s", atual.id, atual.dataset, atual.status)

        while True:
            prox = s.execute(
                select(ColetaFila).where(ColetaFila.status == "AGUARDANDO")
                .order_by(ColetaFila.id).limit(1)
            ).scalar_one_or_none()
            if prox is None:
                return "fila vazia"
            orgaos = json.loads(prox.orgaos_json) if prox.orgaos_json else None
            if transparencia.get_spec(prox.dataset) is None:
                prox.status, prox.concluida_em = "FALHOU", agora
                prox.erro = f"dataset desconhecido: {prox.dataset}"
                s.commit()
                continue
            try:
                job = start_dataset_job(
                    s, prox.dataset, prox.ano_ini, prox.mes_ini, prox.ano_fim, prox.mes_fim,
                    orgaos, prox.force,
                )
            except Exception as exc:
                # nao trava a fila: marca este item e tenta o seguinte
                s.rollback()
                log.exception("Fila: falha ao iniciar item %d (%s)", prox.id, prox.dataset)
                prox.status, prox.erro, prox.concluida_em = "FALHOU", str(exc)[:500], agora
                s.commit()
                continue
            prox.status, prox.job_id, prox.iniciada_em = "RODANDO", job.id, agora
            s.commit()
            log.info("Fila: item %d (%s) iniciado — job %s", prox.id, prox.dataset, job.id)
            return f"iniciado {prox.dataset}"


@celery.task
def reconcile_stalled():
    """Fecha as janelas de trabalho orfao que o contador de pending nao cobre:

    - documentos PENDING antigos (task perdida entre commit e enqueue, ou
      mensagem descartada) -> FAILED + decrementa o pending do job;
    - jobs RUNNING antigos demais (unidades orfãs de pagina, sem linha em
      documento) -> FAILED, para nao ficarem zumbis no painel.
    """
    now = datetime.now(timezone.utc)
    orphans = 0
    doc_cutoff = now - timedelta(hours=config.DOC_ORPHAN_HOURS)
    job_cutoff = now - timedelta(hours=config.JOB_STALL_HOURS)

    with SessionLocal() as s:
        rows = s.execute(
            select(Documento.id, Documento.job_id)
            .where(
                Documento.status_download == DownloadStatus.PENDING,
                Documento.created_at < doc_cutoff,
            )
            .limit(500)
        ).all()
        for doc_id, job_id in rows:
            # condicional: se a task real fechou o doc nesse meio-tempo, nao
            # desconta de novo (evita contador negativo)
            fechado = s.execute(
                update(Documento)
                .where(Documento.id == doc_id, Documento.status_download == DownloadStatus.PENDING)
                .values(status_download=DownloadStatus.FAILED)
                .returning(Documento.id)
            ).scalar_one_or_none()
            if fechado is None:
                s.commit()
                continue
            finish_task_unit(s, job_id)
            orphans += 1

    with SessionLocal() as s:
        # Zumbi = RUNNING ha mais de JOB_STALL_HOURS E sem NENHUMA atividade
        # nesse periodo (documento criado/baixado, pagina navegada). Antes a
        # idade bastava, e coletas longas legitimas (ex.: Diario Oficial desde
        # 2008) eram marcadas FAILED no meio do trabalho.
        doc_recente = (
            select(Documento.id)
            .where(
                Documento.job_id == ScrapingJob.id,
                (Documento.created_at >= job_cutoff) | (Documento.downloaded_at >= job_cutoff),
            )
            .exists()
        )
        pagina_recente = (
            select(CrawledPage.url_hash)
            .where(CrawledPage.job_id == ScrapingJob.id, CrawledPage.crawled_at >= job_cutoff)
            .exists()
        )
        stalled = s.execute(
            update(ScrapingJob)
            .where(
                ScrapingJob.status == JobStatus.RUNNING,
                ScrapingJob.started_at < job_cutoff,
                ~doc_recente,
                ~pagina_recente,
            )
            .values(status=JobStatus.FAILED, completed_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        s.commit()

    if orphans or stalled:
        log.warning(
            "Reconcile: %d documentos orfaos -> FAILED, %d jobs zumbis -> FAILED",
            orphans, stalled,
        )
    return {"orphans": orphans, "stalled_jobs": stalled}


# ---------- folha de pagamento (API de dados abertos da transparencia) ----------

TRANSPARENCIA_SIGLA = "TRANSPARENCIA"
TRANSPARENCIA_URL = "https://www.sistemas.pa.gov.br/portaltransparencia/"


def get_or_create_transparencia(session) -> Secretaria:
    """Fonte virtual dos documentos de folha — aparece como uma secretaria
    (tipo 'api'), entao filtros e painel funcionam sem mudanca."""
    sec = session.execute(
        select(Secretaria).where(Secretaria.sigla == TRANSPARENCIA_SIGLA)
    ).scalar_one_or_none()
    if sec is None:
        sec = Secretaria(
            sigla=TRANSPARENCIA_SIGLA,
            url_base=TRANSPARENCIA_URL,
            is_ativa=True,
            tipo="api",
        )
        session.add(sec)
        session.commit()
    return sec


def start_dataset_job(
    session, dataset: str, ano_ini: int, mes_ini: int, ano_fim: int, mes_fim: int,
    orgaos: list[str] | None, force: bool = False,
) -> ScrapingJob:
    """Cria job de coleta num dataset da transparencia e enfileira a
    enumeracao dos meses no intervalo."""
    sec = get_or_create_transparencia(session)
    job = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING, tipo=dataset)
    session.add(job)
    session.flush()
    session.add(JobPending(job_id=job.id, pending=1))
    session.commit()
    try:
        discover_dataset.apply_async(args=[{
            "job_id": str(job.id),
            "secretaria_id": str(sec.id),
            "dataset": dataset,
            "ano_ini": ano_ini, "mes_ini": mes_ini,
            "ano_fim": ano_fim, "mes_fim": mes_fim,
            "orgaos": orgaos,
            "force": force,
        }])
    except Exception:
        log.exception("Falha ao enfileirar discover_dataset do job %s", job.id)
        session.execute(
            update(ScrapingJob)
            .where(ScrapingJob.id == job.id)
            .values(status=JobStatus.FAILED, completed_at=datetime.now(timezone.utc))
        )
        session.commit()
        raise
    return job


@celery.task(
    base=DeadLetterTask,
    bind=True,
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=config.RETRY_BASE_SECONDS,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=config.MAX_RETRIES,
)
def discover_dataset(self, payload: dict):
    """Enumera os meses publicados do dataset no intervalo; cada um vira
    um documento (CSV unico por competencia)."""
    dataset = payload["dataset"]
    spec = transparencia.DATASETS[dataset]
    job_id = uuid.UUID(payload["job_id"])
    secretaria_id = uuid.UUID(payload["secretaria_id"])
    orgaos = payload.get("orgaos")
    force = bool(payload.get("force"))
    ini = (int(payload["ano_ini"]), int(payload["mes_ini"]))
    fim = (int(payload["ano_fim"]), int(payload["mes_fim"]))

    if _job_cancelado(job_id):
        log.info("Job %s cancelado — descoberta de %s descartada", job_id, dataset)
        return

    recortes = transparencia.build_recortes(spec, ini, fim)

    # Unidade = (recorte, orgao). Selecao explicita => 1 doc por orgao.
    # "todos" => expande para cada orgao do periodo (1 arquivo por sigla).
    # Excecao: dataset 'range' e sempre um recorte unico (a API nao filtra
    # e cada orgao custaria uma varredura completa) — a selecao inteira vai
    # numa unica unidade ("A+B+C") e o fetch filtra client-side.
    sel = [str(s).upper() for s in orgaos] if orgaos else None

    def _labels(recorte: dict) -> list[str]:
        """Traduz a selecao pro rotulo do dataset (sigla vs nome oficial)."""
        try:
            mapa = {
                transparencia.orgao_slug(g["sigla"]): g["sigla"]
                for g in spec.list_orgaos(
                    recorte.get("ano", ini[0]), recorte.get("mes") or 0
                )
            }
            return [mapa.get(transparencia.orgao_slug(s), s) for s in sel]
        except Exception:
            return list(sel)

    units: list[tuple[dict, Optional[str]]] = []
    for recorte in recortes:
        if spec.granularity == "range":
            units.append(
                (recorte, "+".join(_labels(recorte)) if sel else None)
            )
            continue
        if sel:
            units += [(recorte, s) for s in _labels(recorte)]
            continue
        try:
            orgs = spec.list_orgaos(recorte.get("ano", ini[0]), recorte.get("mes") or 0)
        except Exception:
            orgs = []
        if orgs:
            units += [(recorte, g["sigla"]) for g in orgs]
        else:
            units.append((recorte, None))

    log.info(
        "Dataset %s job %s: %d unidades (%s) no intervalo %s..%s",
        dataset, job_id, len(units), spec.granularity, ini, fim,
    )

    new_units = []
    with SessionLocal() as s:
        for recorte, orgao in units:
            url = transparencia.synthetic_url(dataset, recorte, orgao)
            doc_id = s.execute(
                pg_insert(Documento)
                .values(
                    secretaria_id=secretaria_id,
                    job_id=job_id,
                    url_origem=url,
                    status_download=DownloadStatus.PENDING,
                )
                .on_conflict_do_nothing(index_elements=["url_origem"])
                .returning(Documento.id)
            ).scalar_one_or_none()
            if doc_id is None:
                # ja coletada antes: com 'recoletar' refaz tudo; sem ele, refaz SO
                # o que falhou (antes uma falha antiga ficava pulada para sempre).
                # SUCCESS e SEM_DADOS continuam pulados.
                # created_at = agora (ver discover_page): evita o reconciliador
                refazer = Documento.url_origem == url
                if not force:
                    refazer = refazer & (Documento.status_download == DownloadStatus.FAILED)
                doc_id = s.execute(
                    update(Documento)
                    .where(refazer)
                    .values(job_id=job_id, status_download=DownloadStatus.PENDING,
                            created_at=func.now())
                    .returning(Documento.id)
                ).scalar_one_or_none()
            if doc_id is None:
                continue
            _inc_pending(s, job_id)
            new_units.append((doc_id, recorte, orgao))

        if new_units:
            s.execute(
                update(ScrapingJob)
                .where(ScrapingJob.id == job_id)
                .values(total_pdfs_encontrados=ScrapingJob.total_pdfs_encontrados + len(new_units))
            )
        finish_task_unit(s, job_id)
        s.commit()

        for doc_id, recorte, orgao in new_units:
            _enqueue_compensate(
                s,
                job_id,
                lambda d=doc_id, r=recorte, o=orgao: fetch_dataset_mes.apply_async(
                    args=[{
                        "documento_id": str(d), "job_id": str(job_id),
                        "dataset": dataset, "recorte": r, "orgao": o,
                    }]
                ),
                lambda d=doc_id: s.execute(
                    update(Documento)
                    .where(Documento.id == d)
                    .values(status_download=DownloadStatus.FAILED)
                ),
            )


@celery.task(
    base=DeadLetterTask,
    bind=True,
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=config.RETRY_BASE_SECONDS,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=config.MAX_RETRIES,
)
def fetch_dataset_mes(self, payload: dict):
    """Coleta um mes do dataset na API da transparencia e grava um CSV
    unico em MinIO + downloads/<dataset>/ano/mes/."""
    dataset = payload["dataset"]
    spec = transparencia.DATASETS[dataset]
    job_id = uuid.UUID(payload["job_id"])
    documento_id = uuid.UUID(payload["documento_id"])
    recorte = {k: int(v) for k, v in payload.get("recorte", {}).items()}
    # rotulo do orgao — simples ou "A+B+C" (range com selecao); None = todos
    orgao = payload.get("orgao")
    siglas = orgao.split("+") if orgao else None
    rotulo = transparencia.recorte_label(recorte)

    if _job_cancelado(job_id):
        log.info("Job %s cancelado — coleta %s %s descartada", job_id, dataset, rotulo)
        return

    # Documentos PDF por orgao (datasets que publicam anexos — ex.:
    # termo/plano de trabalho dos convenios). Independe do dedup do CSV —
    # roda antes, pois cada doc vai pra <SIGLA>/<dataset>/documentos/.
    if spec.iter_docs:
        n_ok, seen = 0, {}
        for d in spec.iter_docs(recorte, siglas):
            # loop longo (centenas de PDFs, horas): cancelar no painel para aqui
            if _job_cancelado(job_id):
                log.info("Job %s cancelado durante os documentos de %s %s", job_id, dataset, rotulo)
                return
            # ate 3 tentativas: o servidor as vezes fecha a conexao no meio
            # do PDF; sem retry o doc ficava faltando e o progresso em 99,9%.
            for tentativa in range(1, 4):
                try:
                    if _baixa_doc_pdf(dataset, d, seen):
                        n_ok += 1
                    break
                except Exception:
                    if tentativa == 3:
                        log.exception("Doc %s falhou apos 3 tentativas", d.get("url"))
                    else:
                        log.warning("Doc %s falhou (tentativa %d/3); repetindo", d.get("url"), tentativa)
                        time.sleep(config.DOWNLOAD_DELAY_SECONDS * tentativa * 2)
            time.sleep(config.DOWNLOAD_DELAY_SECONDS)
        log.info("%s %s: %d documentos baixados", dataset, rotulo, n_ok)

    with SessionLocal() as s:
        doc = s.get(Documento, documento_id)
        if doc is None:
            finish_task_unit(s, job_id)
            return
        if doc.status_download == DownloadStatus.SUCCESS:
            return  # reentrega de task ja concluida: pendencia ja descontada
        hash_anterior, caminho_anterior = doc.hash_sha256, doc.minio_path

    def _mark(status: DownloadStatus):
        with SessionLocal() as s:
            _fecha_documento(s, documento_id, job_id, status_download=status)

    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as tmp:
        try:
            total_rows, _ = _linhas_para_csv(spec.iter_rows(recorte, siglas), tmp, job_id)
        except _Cancelado:
            log.info("Job %s cancelado durante a coleta de %s %s", job_id, dataset, rotulo)
            return

        if total_rows == 0:
            # nao e erro: o orgao simplesmente nao publicou nada no periodo
            _mark(DownloadStatus.SEM_DADOS)
            log.info("%s %s sem dados publicados", dataset, rotulo)
            return

        digest, size = _sha_e_tamanho(tmp)

        slug = transparencia.orgao_slug(orgao) if orgao else None
        label = slug or "todos"
        if orgao:
            # pasta por sigla canonica, igual ao crawl: downloads/SESPA/<dataset>/...
            base = f"{slug}/{dataset}/{rotulo}"
        else:
            base = f"{dataset}/{rotulo}"
        path = (
            f"{config.MINIO_BUCKET}/{base}/"
            f"{dataset}-{rotulo}-{label}.csv"
        )
        fechar = dict(
            minio_path=path, tamanho_bytes=size,
            status_download=DownloadStatus.SUCCESS,
            downloaded_at=datetime.now(timezone.utc),
        )
        if hash_anterior == digest and caminho_anterior == path:
            # re-coleta (force) com conteudo igual: o arquivo ja esta la
            with SessionLocal() as s:
                _fecha_documento(s, documento_id, job_id, hash_sha256=digest, **fechar)
            log.info("%s %s sem mudancas desde a ultima coleta", dataset, rotulo)
            return

        # Cada unidade (dataset x periodo x orgao) tem o PROPRIO arquivo: mesmo
        # com conteudo identico ao de outra unidade, grava na pasta dela (antes
        # so apontava pro arquivo alheio e a pasta do orgao ficava vazia).
        _publica_arquivo(tmp, path, size, "text/csv")

        with SessionLocal() as s:
            dono = s.execute(
                select(Documento.id).where(
                    Documento.hash_sha256 == digest, Documento.id != documento_id
                )
            ).scalar_one_or_none()
            try:
                # hash e UNIQUE global: se outra unidade ja e dona, fica sem hash
                _fecha_documento(
                    s, documento_id, job_id,
                    hash_sha256=None if dono else digest, **fechar,
                )
            except IntegrityError:
                s.rollback()  # outra unidade gravou o mesmo hash agora ha pouco
                _fecha_documento(s, documento_id, job_id, hash_sha256=None, **fechar)
        log.info(
            "%s %s ok: %d registros (%d bytes) -> %s",
            dataset, rotulo, total_rows, size, path,
        )

    time.sleep(config.DOWNLOAD_DELAY_SECONDS)


# ---------- fontes genericas (coleta configurada pelo painel) ----------


def start_coleta_job(session, secretaria) -> ScrapingJob:
    """Cria o job de uma fonte 'coleta' e enfileira a coleta."""
    job = ScrapingJob(secretaria_id=secretaria.id, status=JobStatus.RUNNING, tipo="coleta")
    session.add(job)
    session.flush()
    session.add(JobPending(job_id=job.id, pending=1))
    session.commit()
    try:
        coletar_fonte.apply_async(
            args=[{"job_id": str(job.id), "secretaria_id": str(secretaria.id)}]
        )
    except Exception:
        log.exception("Falha ao enfileirar coletar_fonte do job %s", job.id)
        session.execute(
            update(ScrapingJob)
            .where(ScrapingJob.id == job.id)
            .values(status=JobStatus.FAILED, completed_at=datetime.now(timezone.utc))
        )
        session.commit()
        raise
    return job


@celery.task(
    base=DeadLetterTask,
    bind=True,
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=config.RETRY_BASE_SECONDS,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=config.MAX_RETRIES,
)
def coletar_fonte(self, payload: dict):
    """Executa a coleta de uma fonte generica: busca JSON/tabela (com paginacao),
    junta tudo num CSV unico e grava em MinIO + downloads/<sigla>/<ano>/."""
    job_id = uuid.UUID(payload["job_id"])
    secretaria_id = uuid.UUID(payload["secretaria_id"])
    if _job_cancelado(job_id):
        log.info("Job %s cancelado — coleta descartada", job_id)
        return

    with SessionLocal() as s:
        sec = s.get(Secretaria, secretaria_id)
        if sec is None or not sec.coleta:
            log.error("Fonte %s sem configuracao de coleta", secretaria_id)
            finish_task_unit(s, job_id)
            return
        sigla, cfg = sec.sigla, coleta.ColetaConfig(**sec.coleta)
        s.execute(
            update(ScrapingJob).where(ScrapingJob.id == job_id).values(total_pdfs_encontrados=1)
        )
        s.commit()

    agora = datetime.now(timezone.utc)
    url_origem = f"coleta://{sigla}/{job_id}"

    def _registra(s, **values):
        """Cria o documento do resultado e desconta a pendencia — so se o insert
        aconteceu (reentrega da task nao desconta duas vezes)."""
        novo = s.execute(
            pg_insert(Documento)
            .values(secretaria_id=secretaria_id, job_id=job_id, url_origem=url_origem, **values)
            .on_conflict_do_nothing(index_elements=["url_origem"])
            .returning(Documento.id)
        ).scalar_one_or_none()
        if novo is not None:
            finish_task_unit(s, job_id)
        else:
            s.commit()

    def _falha(motivo: str):
        log.warning("Coleta %s falhou: %s", sigla, motivo)
        with SessionLocal() as s:
            _registra(s, status_download=DownloadStatus.FAILED)

    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as tmp:
        try:
            total, n_colunas = _linhas_para_csv(
                coleta.buscar_linhas(cfg, pausa=config.DISCOVERY_DELAY_SECONDS), tmp, job_id
            )
        except _Cancelado:
            log.info("Job %s cancelado durante a coleta", job_id)
            return
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500 and code not in (408, 429):
                return _falha(f"HTTP {code} em {exc.request.url}")
            raise
        except ValueError as exc:  # caminho/tabela inexistente (JSONDecodeError sobe p/ retry)
            if isinstance(exc, json.JSONDecodeError):
                raise
            return _falha(str(exc))
        if total == 0:
            log.info("Coleta %s: o site respondeu sem nenhum registro", sigla)
            with SessionLocal() as s:
                _registra(s, status_download=DownloadStatus.SEM_DADOS)
            return

        digest, size = _sha_e_tamanho(tmp)

        with SessionLocal() as s:
            dupe = s.execute(
                select(Documento).where(Documento.hash_sha256 == digest)
            ).scalar_one_or_none()
            if dupe and dupe.secretaria_id == secretaria_id:
                _registra(
                    s, minio_path=dupe.minio_path, tamanho_bytes=size,
                    status_download=DownloadStatus.SUCCESS, downloaded_at=agora,
                )
                log.info("Coleta %s identica a %s — nada novo", sigla, dupe.minio_path)
                return

        nome = f"{sigla.lower()}-{agora:%Y-%m-%d_%H%M%S}.csv"
        path = f"{config.MINIO_BUCKET}/{sigla.lower()}/{agora:%Y}/{nome}"
        _publica_arquivo(tmp, path, size, "text/csv")

        fechar = dict(
            minio_path=path, tamanho_bytes=size, magic_byte_valido=True,
            status_download=DownloadStatus.SUCCESS, downloaded_at=agora,
        )
        with SessionLocal() as s:
            try:
                # dupe de OUTRA fonte: copia propria nesta pasta, sem o hash (UNIQUE)
                _registra(s, hash_sha256=None if dupe else digest, **fechar)
            except IntegrityError:
                s.rollback()
                _registra(s, hash_sha256=None, **fechar)
        log.info("Coleta %s ok: %d registros, %d colunas -> %s", sigla, total, n_colunas, path)


def requeue_dead_letters(limit: int = 100) -> int:
    """Consome a q.dead e republica na fila original (novo ciclo de retries).

    Dead letters so contem o formato {'task', 'payload'} gravado pelo
    DeadLetterTask; payloads de tasks desconhecidas sao descartados."""
    try:
        conn = broker._connect()
    except pika.exceptions.AMQPError:
        log.exception("RabbitMQ indisponivel para requeue")
        return 0
    requeued = 0
    try:
        ch = conn.channel()
        for _ in range(limit):
            method, _props, body = ch.basic_get(queue=config.Q_DEAD, auto_ack=False)
            if method is None:
                break
            try:
                msg = json.loads(body)
                # Formato gravado por send_to_dead: {"payload": {"task": nome,
                # "payload": {...}}, "reason": "..."}
                inner = msg.get("payload")
                if not isinstance(inner, dict) or "task" not in inner:
                    inner = msg
                task_name, payload = inner.get("task"), inner.get("payload")
                if task_name in DEAD_REQUEUEABLE and isinstance(payload, dict):
                    celery.send_task(task_name, args=[payload])
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    requeued += 1
                else:
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    log.warning("Dead letter descartada (task desconhecida): %r", task_name)
            except json.JSONDecodeError:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                log.warning("Dead letter descartada (payload invalido)")
            except Exception:
                log.exception("Falha ao reprocessar dead letter — devolvendo a fila")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                break
    finally:
        conn.close()
    return requeued
