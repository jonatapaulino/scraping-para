import csv
import hashlib
import io
import json
import logging
import os
from typing import Optional
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse, urlunparse

import httpx
import pika
from bs4 import BeautifulSoup
from celery import Task
from celery.signals import worker_ready
from minio.error import S3Error
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError as SAOperationalError
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app import broker, config, drive, http_client, storage, transparencia
from app.celery_app import celery
from app.database import SessionLocal
from app.models import (
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

# S3Error/urllib3: falhas de rede contra o MinIO local sao tao transientes
# quanto falhas de download — sem isso a task morria direto na DLX.
TRANSIENT_ERRORS = (
    httpx.HTTPError,
    SAOperationalError,
    ConnectionError,
    TimeoutError,
    S3Error,
    Urllib3HTTPError,
)

DEAD_REQUEUEABLE = (
    "app.tasks.discover_page",
    "app.tasks.download_document",
    "app.tasks.discover_dataset",
    "app.tasks.fetch_dataset_mes",
)


@worker_ready.connect
def _on_worker_ready(**_kwargs):
    storage.ensure_bucket(attempts=5, delay=3)
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
    ).scalar_one()
    if remaining <= 0:
        session.execute(
            update(ScrapingJob)
            .where(ScrapingJob.id == job_id, ScrapingJob.status == JobStatus.RUNNING)
            .values(status=JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))
        )
    session.commit()


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
                    s.execute(
                        update(Documento)
                        .where(Documento.id == uuid.UUID(doc_id))
                        .values(status_download=DownloadStatus.FAILED)
                    )
                if job_id:
                    finish_task_unit(s, job_id)
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


def extract_links(page_url: str, html: str) -> tuple[list[str], list[str]]:
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
            docs.append(abs_url)
        elif urlparse(abs_url).netloc == base_netloc:
            pages.append(abs_url)
    return docs, pages


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


def _minio_path(sigla: str, url: str) -> str:
    parsed = urlparse(url)
    filename = _filename_from_url(url)
    year = "unknown"
    for part in parsed.path.split("/"):
        if part.isdigit() and len(part) == 4:
            year = part
            break
    if year == "unknown":
        m = re.search(r"20\d{2}", filename)
        if m:
            year = m.group(0)
    return f"{config.MINIO_BUCKET}/{sigla.lower()}/{year}/{filename}"


def _fallback_url(url: str, base_url: str | None) -> str | None:
    """Mesmo path/query no host-base da secretaria — cobre links que apontam pra intranet."""
    if not base_url:
        return None
    parsed, base = urlparse(url), urlparse(base_url)
    if not parsed.netloc or parsed.netloc == base.netloc:
        return None
    return parsed._replace(scheme=base.scheme or parsed.scheme, netloc=base.netloc).geturl()


def _baixa_doc_pdf(dataset: str, doc: dict, seen: Optional[dict] = None) -> bool:
    """Baixa um PDF de anexo do dataset -> local + MinIO.
    doc: {'url', 'orgao', 'sub', 'nome'}. Pula se o arquivo ja existe ou
    se o conteudo ja foi gravado na mesma pasta (docs duplicados — ex.:
    termo e plano de trabalho apontando pro mesmo binario)."""
    pasta = f"{doc['orgao']}/{dataset}/documentos/{doc.get('sub') or '_avulso'}"
    rel = f"{pasta}/{doc['nome']}"
    local = os.path.join(config.LOCAL_EXPORT_DIR, rel) if config.LOCAL_EXPORT_DIR else None
    if local and os.path.exists(local):
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
    minio = storage.ensure_bucket()
    minio.put_object(
        config.MINIO_BUCKET, rel, io.BytesIO(body),
        length=len(body), content_type="application/pdf",
    )
    if local:
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(body)
    return True


def _export_local(tmp, minio_path: str) -> None:
    """Espelha o objeto (bucket/path no estilo 'raw-docs/a/b.csv') no host."""
    dst = os.path.join(config.LOCAL_EXPORT_DIR, minio_path.split("/", 1)[1])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp.seek(0)
    with open(dst, "wb") as f:
        shutil.copyfileobj(tmp, f)
    log.info("Exportado para %s", dst)


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
    docs, pages = extract_links(url, html)

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
                doc_id = s.execute(
                    update(Documento)
                    .where(Documento.url_origem == doc_url)
                    .values(job_id=job_id, status_download=DownloadStatus.PENDING)
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

    with SessionLocal() as s:
        doc = s.get(Documento, documento_id)
        if doc is None:
            log.error("Documento %s nao encontrado", documento_id)
            finish_task_unit(s, job_id)
            return
        if doc.status_download == DownloadStatus.SUCCESS:
            log.info("Documento ja baixado: %s", url)
            finish_task_unit(s, job_id)
            return
        sec = s.get(Secretaria, doc.secretaria_id)
        base_url = sec.url_base if sec else None

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
                    s.execute(
                        update(Documento)
                        .where(Documento.id == documento_id)
                        .values(status_download=DownloadStatus.FAILED)
                    )
                    finish_task_unit(s, job_id)
                log.warning("Download %d terminal (link quebrado): %s", code, url)
                return
            raise
        if truncated_magic or len(first) < len(PDF_MAGIC):
            with SessionLocal() as s:
                s.execute(
                    update(Documento)
                    .where(Documento.id == documento_id)
                    .values(magic_byte_valido=False, status_download=DownloadStatus.FAILED)
                )
                finish_task_unit(s, job_id)
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
            if dupe:
                s.execute(
                    update(Documento)
                    .where(Documento.id == documento_id)
                    .values(
                        magic_byte_valido=True,
                        minio_path=dupe.minio_path,
                        tamanho_bytes=size,
                        status_download=DownloadStatus.SUCCESS,
                        downloaded_at=datetime.now(timezone.utc),
                    )
                )
                finish_task_unit(s, job_id)
                log.info("Binario duplicado de %s — reusa %s", url, dupe.minio_path)
                return

        sigla = None
        with SessionLocal() as s:
            row = s.execute(
                select(Secretaria.sigla)
                .join(Documento, Documento.secretaria_id == Secretaria.id)
                .where(Documento.id == documento_id)
            ).scalar_one_or_none()
            sigla = row or "unknown"
        path = _minio_path(sigla, url)

        minio = storage.ensure_bucket()
        minio.put_object(
            config.MINIO_BUCKET,
            path.split("/", 1)[1],
            tmp,
            length=size,
            content_type="application/pdf",
        )

        if config.LOCAL_EXPORT_DIR:
            try:
                _export_local(tmp, path)
            except OSError:
                log.exception("Falha ao exportar %s para o host", path)
        drive.upload_file(tmp, path.split("/", 1)[1], "application/pdf")

        with SessionLocal() as s:
            try:
                s.execute(
                    update(Documento)
                    .where(Documento.id == documento_id)
                    .values(
                        hash_sha256=digest,
                        minio_path=path,
                        tamanho_bytes=size,
                        magic_byte_valido=True,
                        status_download=DownloadStatus.SUCCESS,
                        downloaded_at=datetime.now(timezone.utc),
                    )
                )
                finish_task_unit(s, job_id)
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
                s.execute(
                    update(Documento)
                    .where(Documento.id == documento_id)
                    .values(
                        magic_byte_valido=True,
                        minio_path=dupe.minio_path,
                        tamanho_bytes=size,
                        status_download=DownloadStatus.SUCCESS,
                        downloaded_at=datetime.now(timezone.utc),
                    )
                )
                finish_task_unit(s, job_id)
                try:
                    minio.remove_object(config.MINIO_BUCKET, path.split("/", 1)[1])
                except S3Error:
                    log.warning("Objeto orfao nao removido: %s", path)
                log.info("Upload duplicado descartado — reusa %s", dupe.minio_path)
                return
        log.info("Download ok %s (%d bytes) -> s3://%s", url, size, path)

    time.sleep(config.DOWNLOAD_DELAY_SECONDS)


@celery.task
def dispatch_scheduled():
    """Disparado pelo Celery Beat: cria um job por secretaria ativa."""
    with SessionLocal() as s:
        ativas = s.execute(
            select(Secretaria).where(
                Secretaria.is_ativa.is_(True), Secretaria.tipo == "crawl"
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
    return ok


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
            s.execute(
                update(Documento)
                .where(Documento.id == doc_id)
                .values(status_download=DownloadStatus.FAILED)
            )
            finish_task_unit(s, job_id)
            orphans += 1

    with SessionLocal() as s:
        stalled = s.execute(
            update(ScrapingJob)
            .where(
                ScrapingJob.status == JobStatus.RUNNING,
                ScrapingJob.started_at < job_cutoff,
            )
            .values(status=JobStatus.FAILED, completed_at=now)
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

    recortes = transparencia.build_recortes(spec, ini, fim)

    # Unidade = (recorte, orgao). Selecao explicita => 1 doc por orgao.
    # "todos" => expande para cada orgao do periodo (1 arquivo por sigla).
    # Excecoes: dataset 'range' com selecao ampla (varredura unica — a API
    # nao filtra e cada orgao custaria uma varredura completa) e datasets
    # sem conceito de orgao.
    sel = [str(s).upper() for s in orgaos] if orgaos else None
    units: list[tuple[dict, Optional[str]]] = []
    for recorte in recortes:
        if spec.granularity == "range" and (not sel or len(sel) != 1):
            units.append((recorte, None))
            continue
        if sel:
            # traduz a selecao pro rotulo do dataset (sigla vs nome oficial)
            try:
                mapa = {
                    transparencia.orgao_slug(g["sigla"]): g["sigla"]
                    for g in spec.list_orgaos(
                        recorte.get("ano", ini[0]), recorte.get("mes") or 0
                    )
                }
                labels = [
                    mapa.get(transparencia.orgao_slug(s), s) for s in sel
                ]
            except Exception:
                labels = sel
            units += [(recorte, s) for s in labels]
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
            if doc_id is None and force:
                doc_id = s.execute(
                    update(Documento)
                    .where(Documento.url_origem == url)
                    .values(job_id=job_id, status_download=DownloadStatus.PENDING)
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
    orgao = payload.get("orgao")  # sigla/nome do orgao, ou None (recorte unico)
    siglas = [orgao] if orgao else None
    rotulo = transparencia.recorte_label(recorte)

    # Documentos PDF por orgao (datasets que publicam anexos — ex.:
    # termo/plano de trabalho dos convenios). Independe do dedup do CSV —
    # roda antes, pois cada doc vai pra <SIGLA>/<dataset>/documentos/.
    if spec.iter_docs:
        n_ok, seen = 0, {}
        for d in spec.iter_docs(recorte, siglas):
            try:
                if _baixa_doc_pdf(dataset, d, seen):
                    n_ok += 1
            except Exception:
                log.exception("Doc %s falhou", d.get("url"))
            time.sleep(config.DOWNLOAD_DELAY_SECONDS)
        log.info("%s %s: %d documentos baixados", dataset, rotulo, n_ok)

    with SessionLocal() as s:
        doc = s.get(Documento, documento_id)
        if doc is None or doc.status_download == DownloadStatus.SUCCESS:
            finish_task_unit(s, job_id)
            return

    def _mark(status: DownloadStatus):
        with SessionLocal() as s:
            s.execute(
                update(Documento)
                .where(Documento.id == documento_id)
                .values(status_download=status)
            )
            finish_task_unit(s, job_id)

    total_rows = 0
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as tmp:
        text_tmp = io.TextIOWrapper(
            tmp, encoding="utf-8-sig", newline="", write_through=True
        )
        writer = None
        for row in spec.iter_rows(recorte, siglas):
            if writer is None:
                writer = csv.DictWriter(
                    text_tmp, fieldnames=list(row.keys()), extrasaction="ignore"
                )
                writer.writeheader()
            writer.writerow(row)
            total_rows += 1

        if total_rows == 0:
            _mark(DownloadStatus.FAILED)
            log.warning("%s %s sem dados publicados", dataset, rotulo)
            return

        text_tmp.flush()
        tmp.seek(0)
        sha = hashlib.sha256()
        for chunk in iter(lambda: tmp.read(256 * 1024), b""):
            sha.update(chunk)
        digest = sha.hexdigest()
        size = tmp.tell()
        tmp.seek(0)

        with SessionLocal() as s:
            dupe = s.execute(
                select(Documento).where(
                    Documento.hash_sha256 == digest, Documento.id != documento_id
                )
            ).scalar_one_or_none()
            if dupe:
                s.execute(
                    update(Documento)
                    .where(Documento.id == documento_id)
                    .values(
                        minio_path=dupe.minio_path,
                        tamanho_bytes=size,
                        status_download=DownloadStatus.SUCCESS,
                        downloaded_at=datetime.now(timezone.utc),
                    )
                )
                finish_task_unit(s, job_id)
                log.info(
                    "%s %s identico a %s — reusa",
                    dataset, rotulo, dupe.minio_path,
                )
                return

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
        minio = storage.ensure_bucket()
        minio.put_object(
            config.MINIO_BUCKET,
            path.split("/", 1)[1],
            tmp,
            length=size,
            content_type="text/csv",
        )
        if config.LOCAL_EXPORT_DIR:
            try:
                _export_local(tmp, path)
            except OSError:
                log.exception("Falha ao exportar %s para o host", path)
        drive.upload_file(tmp, path.split("/", 1)[1], "text/csv")

        with SessionLocal() as s:
            s.execute(
                update(Documento)
                .where(Documento.id == documento_id)
                .values(
                    hash_sha256=digest,
                    minio_path=path,
                    tamanho_bytes=size,
                    status_download=DownloadStatus.SUCCESS,
                    downloaded_at=datetime.now(timezone.utc),
                )
            )
            finish_task_unit(s, job_id)
        log.info(
            "%s %s ok: %d registros (%d bytes) -> s3://%s",
            dataset, rotulo, total_rows, size, path,
        )

    time.sleep(config.DOWNLOAD_DELAY_SECONDS)


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
