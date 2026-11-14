import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from celery import Task
from celery.signals import worker_ready
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError as SAOperationalError

from app import broker, config, http_client, storage
from app.celery_app import celery
from app.database import SessionLocal
from app.models import (
    CrawledPage,
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

TRANSIENT_ERRORS = (
    httpx.HTTPError,
    SAOperationalError,
    ConnectionError,
    TimeoutError,
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


def start_job_for_secretaria(
    session, secretaria, force_full_scrape: bool, max_depth: int | None = None
) -> ScrapingJob:
    """Cria o job e enfileira a URL base na q.discovery. Chamado pela API e pelo beat."""
    job = ScrapingJob(secretaria_id=secretaria.id, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    session.add(JobPending(job_id=job.id, pending=1))
    session.commit()
    _enqueue_discover(
        secretaria.url_base, job.id, secretaria.id, 0, force_full_scrape,
        max_depth=max_depth if max_depth is not None else config.DISCOVERY_MAX_DEPTH,
    )
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
        broker.send_to_dead({"task": self.name, "payload": payload}, str(exc))
        job_id = (payload or {}).get("job_id") if isinstance(payload, dict) else None
        if job_id:
            try:
                with SessionLocal() as s:
                    finish_task_unit(s, uuid.UUID(job_id))
            except Exception:
                log.exception("Falha ao decrementar pending do job %s", job_id)


# ---------- extracao de links ----------


def extract_links(page_url: str, html: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    docs, pages = [], []
    base_netloc = urlparse(page_url).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(SKIP_SCHEMES):
            continue
        abs_url, _ = urldefrag(urljoin(page_url, href))
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        # .pdf no path OU na query (ex: arquivo.php?arq=/doc.pdf) — magic bytes valida falso-positivo
        if ".pdf" in abs_url.lower():
            docs.append(abs_url)
        elif parsed.netloc == base_netloc:
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
                if not force:
                    new_page = s.execute(
                        pg_insert(CrawledPage)
                        .values(url=page_url, job_id=job_id)
                        .on_conflict_do_nothing(index_elements=["url"])
                        .returning(CrawledPage.url)
                    ).scalar_one_or_none()
                    if new_page is None:
                        continue
                _inc_pending(s, job_id)
                page_payloads.append(page_url)

        s.execute(
            pg_insert(CrawledPage)
            .values(url=url, job_id=job_id)
            .on_conflict_do_nothing(index_elements=["url"])
        )
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

    sha = hashlib.sha256()
    size = 0
    first = b""
    truncated_magic = False
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as tmp:
        try:
            with http_client.new_client() as c:
                with c.stream("GET", url) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_bytes(256 * 1024):
                        if len(first) < len(PDF_MAGIC):
                            first += chunk[: len(PDF_MAGIC) - len(first)]
                            if first != PDF_MAGIC[: len(first)]:
                                truncated_magic = True
                                break
                        sha.update(chunk)
                        tmp.write(chunk)
                        size += len(chunk)
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
                dst = os.path.join(config.LOCAL_EXPORT_DIR, path.split("/", 1)[1])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp.seek(0)
                with open(dst, "wb") as f:
                    shutil.copyfileobj(tmp, f)
                log.info("Exportado para %s", dst)
            except OSError:
                log.exception("Falha ao exportar %s para o host", path)

        with SessionLocal() as s:
            s.execute(
                update(Documento)
                .where(Documento.id == documento_id)
                .values(
                    hash_sha256=digest,
                    minio_path=path,
                    tamanho_bytes=size,
                    magic_byte_valido=True,
                    status_download=DownloadStatus.SUCCESS,
                )
            )
            finish_task_unit(s, job_id)
        log.info("Download ok %s (%d bytes) -> s3://%s", url, size, path)

    time.sleep(config.DOWNLOAD_DELAY_SECONDS)


@celery.task
def dispatch_scheduled():
    """Disparado pelo Celery Beat: cria um job por secretaria ativa."""
    with SessionLocal() as s:
        ativas = s.execute(
            select(Secretaria).where(Secretaria.is_ativa.is_(True))
        ).scalars().all()
        for sec in ativas:
            start_job_for_secretaria(s, sec, force_full_scrape=False)
        log.info("Beat: %d jobs disparados", len(ativas))
    return len(ativas)
