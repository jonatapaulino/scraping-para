import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Documento, DownloadStatus, ScrapingJob, Secretaria
from app.schemas import (
    DocumentoOut,
    JobListOut,
    JobStartRequest,
    JobStartResponse,
    JobStatusOut,
    OverviewOut,
    SecretariaIn,
    SecretariaOut,
)
from app.tasks import start_job_for_secretaria

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def get_session():
    with SessionLocal() as s:
        yield s


@router.post("/jobs/start", response_model=JobStartResponse, status_code=status.HTTP_202_ACCEPTED)
def start_jobs(req: JobStartRequest, s: Session = Depends(get_session)):
    job_ids = []
    for sigla in req.secretarias:
        sec = s.execute(
            select(Secretaria).where(Secretaria.sigla == sigla.upper())
        ).scalar_one_or_none()
        if sec is None:
            raise HTTPException(404, f"Secretaria '{sigla}' nao cadastrada")
        if not sec.is_ativa:
            raise HTTPException(400, f"Secretaria '{sigla}' esta inativa")
        job = start_job_for_secretaria(s, sec, req.force_full_scrape, req.max_depth)
        job_ids.append(job.id)
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
    return OverviewOut(
        queues=broker.queue_depths(),
        documents={k.value: v for k, v in docs.items()},
        jobs={k.value: v for k, v in jobs.items()},
        secretarias_ativas=ativas,
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
    sec = Secretaria(sigla=req.sigla.upper(), url_base=req.url_base, is_ativa=req.is_ativa)
    s.add(sec)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, f"Secretaria '{req.sigla}' ja existe")
    s.refresh(sec)
    return sec
