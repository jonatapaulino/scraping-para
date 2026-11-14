import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from minio.error import S3Error
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config, storage, transparencia
from app.database import SessionLocal
from app.models import CrawledPage, Documento, DownloadStatus, ScrapingJob, Secretaria
from app.schemas import (
    DocumentoOut,
    FolhaOpcoesOut,
    FolhaStartRequest,
    JobListOut,
    JobStartRequest,
    JobStartResponse,
    JobStatusOut,
    OverviewOut,
    SecretariaIn,
    SecretariaOut,
    SecretariaUpdate,
)
from app.tasks import requeue_dead_letters, start_dataset_job, start_job_for_secretaria

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def get_session():
    with SessionLocal() as s:
        yield s


@router.post("/jobs/start", response_model=JobStartResponse, status_code=status.HTTP_202_ACCEPTED)
def start_jobs(req: JobStartRequest, s: Session = Depends(get_session)):
    # Valida todas as siglas antes de disparar qualquer job — evita
    # disparo parcial quando uma sigla no meio da lista e invalida.
    secs = []
    for sigla in req.secretarias:
        sec = s.execute(
            select(Secretaria).where(Secretaria.sigla == sigla.upper())
        ).scalar_one_or_none()
        if sec is None:
            raise HTTPException(404, f"Secretaria '{sigla}' nao cadastrada")
        if not sec.is_ativa:
            raise HTTPException(400, f"Secretaria '{sigla}' esta inativa")
        if sec.tipo != "crawl":
            raise HTTPException(
                400,
                f"'{sigla}' e fonte de API, nao portal — use POST /api/v1/folha/start",
            )
        secs.append(sec)
    job_ids = [
        start_job_for_secretaria(s, sec, req.force_full_scrape, req.max_depth).id
        for sec in secs
    ]
    return JobStartResponse(job_ids=job_ids)


@router.get("/jobs/{job_id}/status", response_model=JobStatusOut)
def job_status(job_id: uuid.UUID, s: Session = Depends(get_session)):
    job = s.get(ScrapingJob, job_id)
    if job is None:
        raise HTTPException(404, "Job nao encontrado")
    counts = dict(
        s.execute(
            select(Documento.status_download, func.count())
            .where(Documento.job_id == job_id)
            .group_by(Documento.status_download)
        ).all()
    )
    return JobStatusOut(
        job_id=job.id,
        status=job.status,
        pdfs_descobertos=job.total_pdfs_encontrados,
        pdfs_baixados_sucesso=counts.get(DownloadStatus.SUCCESS, 0),
        pdfs_falhas=counts.get(DownloadStatus.FAILED, 0),
    )


@router.get("/jobs", response_model=list[JobListOut])
def list_jobs(limit: int = 20, s: Session = Depends(get_session)):
    rows = s.execute(
        select(ScrapingJob, Secretaria.sigla)
        .join(Secretaria, ScrapingJob.secretaria_id == Secretaria.id)
        .order_by(ScrapingJob.started_at.desc())
        .limit(limit)
    ).all()
    job_ids = [j.id for j, _ in rows]
    counts = {}
    if job_ids:
        for st, job_id, n in s.execute(
            select(Documento.status_download, Documento.job_id, func.count())
            .where(Documento.job_id.in_(job_ids))
            .group_by(Documento.job_id, Documento.status_download)
        ).all():
            counts.setdefault(job_id, {})[st] = n
    return [
        JobListOut(
            job_id=j.id,
            secretaria=sigla,
            status=j.status,
            tipo=j.tipo,
            pdfs_descobertos=j.total_pdfs_encontrados,
            pdfs_baixados_sucesso=counts.get(j.id, {}).get(DownloadStatus.SUCCESS, 0),
            pdfs_falhas=counts.get(j.id, {}).get(DownloadStatus.FAILED, 0),
            started_at=j.started_at,
            completed_at=j.completed_at,
        )
        for j, sigla in rows
    ]


@router.get("/overview", response_model=OverviewOut)
def overview(s: Session = Depends(get_session)):
    from app import broker

    docs = dict(
        s.execute(
            select(Documento.status_download, func.count()).group_by(Documento.status_download)
        ).all()
    )
    jobs = dict(
        s.execute(select(ScrapingJob.status, func.count()).group_by(ScrapingJob.status)).all()
    )
    ativas = s.execute(
        select(func.count(Secretaria.id)).where(Secretaria.is_ativa.is_(True))
    ).scalar_one()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    last_24h = {
        "documents_ok": s.execute(
            select(func.count(Documento.id)).where(
                Documento.status_download == DownloadStatus.SUCCESS,
                Documento.downloaded_at > cutoff,
            )
        ).scalar_one(),
        "documents_failed": s.execute(
            select(func.count(Documento.id)).where(
                Documento.status_download == DownloadStatus.FAILED,
                Documento.created_at > cutoff,
            )
        ).scalar_one(),
        "pages_crawled": s.execute(
            select(func.count(CrawledPage.url_hash)).where(CrawledPage.crawled_at > cutoff)
        ).scalar_one(),
    }
    return OverviewOut(
        queues=broker.queue_depths(),
        documents={k.value: v for k, v in docs.items()},
        jobs={k.value: v for k, v in jobs.items()},
        secretarias_ativas=ativas,
        last_24h=last_24h,
    )


@router.get("/documents", response_model=Page[DocumentoOut])
def list_documents(
    secretaria_id: Optional[uuid.UUID] = None,
    status_download: Optional[DownloadStatus] = None,
    s: Session = Depends(get_session),
):
    stmt = select(Documento).order_by(Documento.created_at.desc())
    if secretaria_id:
        stmt = stmt.where(Documento.secretaria_id == secretaria_id)
    if status_download:
        stmt = stmt.where(Documento.status_download == status_download)
    return paginate(s, stmt)


@router.get("/secretarias", response_model=list[SecretariaOut])
def list_secretarias(s: Session = Depends(get_session)):
    return s.execute(select(Secretaria).order_by(Secretaria.sigla)).scalars().all()


@router.post("/secretarias", response_model=SecretariaOut, status_code=status.HTTP_201_CREATED)
def create_secretaria(req: SecretariaIn, s: Session = Depends(get_session)):
    sec = Secretaria(
        sigla=req.sigla.upper(),
        url_base=req.url_base,
        is_ativa=req.is_ativa,
        max_depth=req.max_depth,
    )
    s.add(sec)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, f"Secretaria '{req.sigla}' ja existe")
    s.refresh(sec)
    return sec


@router.patch("/secretarias/{sec_id}", response_model=SecretariaOut)
def update_secretaria(sec_id: uuid.UUID, req: SecretariaUpdate, s: Session = Depends(get_session)):
    sec = s.get(Secretaria, sec_id)
    if sec is None:
        raise HTTPException(404, "Secretaria nao encontrada")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(sec, field, value)
    s.commit()
    s.refresh(sec)
    return sec


@router.post("/dead-letters/requeue")
def requeue_dead(limit: int = 100):
    """Republica mensagens da q.dead na fila original (novo ciclo de retries)."""
    n = requeue_dead_letters(limit=limit)
    return {"requeued": n}


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: uuid.UUID, s: Session = Depends(get_session)):
    """Faz proxy do arquivo guardado no MinIO (evita presigned URL com host interno)."""
    doc = s.get(Documento, doc_id)
    if doc is None or not doc.minio_path:
        raise HTTPException(404, "Documento nao baixado")
    key = doc.minio_path.split("/", 1)[1]
    minio = storage.client()
    try:
        minio.stat_object(config.MINIO_BUCKET, key)
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchObject"):
            raise HTTPException(404, "Objeto ausente no storage")
        raise HTTPException(502, "Storage indisponivel")
    obj = minio.get_object(config.MINIO_BUCKET, key)

    def _iter():
        try:
            yield from obj.stream(256 * 1024)
        finally:
            obj.close()
            obj.release_conn()

    filename = key.rsplit("/", 1)[-1]
    media_type = "text/csv" if filename.lower().endswith(".csv") else "application/pdf"
    return StreamingResponse(
        _iter(),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/transparencia/opcoes", response_model=FolhaOpcoesOut)
@router.get("/folha/opcoes", response_model=FolhaOpcoesOut)
def transparencia_opcoes(
    dataset: str = "folha", ano: Optional[int] = None, mes: Optional[int] = None
):
    """Periodos publicados + orgaos disponiveis do dataset
    (ultimo periodo com dados, ou o periodo informado)."""
    spec = transparencia.get_spec(dataset)
    if spec is None:
        raise HTTPException(
            400, f"Dataset '{dataset}' desconhecido — {sorted(transparencia.DATASETS)}"
        )
    periodos = spec.list_periods()
    if ano and mes:
        orgaos = spec.list_orgaos(ano, mes)
    else:
        orgaos = []
        if not periodos:
            # datasets snapshot: sem periodo — orgaos independem de competencia
            orgaos = spec.list_orgaos(0, 0)
        for p in reversed(periodos[-6:]):
            orgaos = spec.list_orgaos(p["ano"], p.get("mes") or 0)
            if orgaos:
                break
    return FolhaOpcoesOut(periodos=periodos, orgaos=orgaos)


@router.post("/transparencia/start", status_code=status.HTTP_202_ACCEPTED)
@router.post("/folha/start", status_code=status.HTTP_202_ACCEPTED)
def transparencia_start(req: FolhaStartRequest, s: Session = Depends(get_session)):
    """Dispara a coleta de um dataset na API da transparencia.

    Cada mes do intervalo vira um CSV em downloads/<dataset>/<ano>/<mes>/."""
    if req.dataset == "*":
        # um job por dataset, mesmo periodo/orgaos — cada dataset resolve
        # a granularidade e as siglas proprias no discover
        if (req.ano_ini, req.mes_ini) > (req.ano_fim, req.mes_fim):
            raise HTTPException(400, "Periodo inicial maior que o final")
        jobs = [
            start_dataset_job(
                s, slug, req.ano_ini, req.mes_ini, req.ano_fim, req.mes_fim,
                req.orgaos or None, req.force,
            )
            for slug in transparencia.DATASETS
        ]
        return {"job_ids": [str(j.id) for j in jobs]}
    if transparencia.get_spec(req.dataset) is None:
        raise HTTPException(
            400, f"Dataset '{req.dataset}' desconhecido — {sorted(transparencia.DATASETS)}"
        )
    if (req.ano_ini, req.mes_ini) > (req.ano_fim, req.mes_fim):
        raise HTTPException(400, "Periodo inicial maior que o final")
    job = start_dataset_job(
        s, req.dataset, req.ano_ini, req.mes_ini, req.ano_fim, req.mes_fim,
        req.orgaos or None, req.force,
    )
    return {"job_id": job.id}
