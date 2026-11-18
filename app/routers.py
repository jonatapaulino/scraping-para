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
from app.models import (
    CrawledPage, Documento, DownloadStatus, JobStatus, ScrapingJob, Secretaria,
)
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


_OPCOES_OK: dict = {}  # ultimo resultado bom por (dataset, ano, mes) — serve se a API cair


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
    try:
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
    except Exception:
        log.exception("Falha ao consultar opcoes do dataset %s", dataset)
        antigo = _OPCOES_OK.get((dataset, ano, mes))
        if antigo is not None:
            return antigo  # ultima resposta boa (API fora do ar agora)
        raise HTTPException(
            502, f"API de '{dataset}' indisponivel no momento — tente de novo em instantes"
        )
    out = FolhaOpcoesOut(periodos=periodos, orgaos=orgaos)
    _OPCOES_OK[(dataset, ano, mes)] = out
    return out


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
            for slug, spec in transparencia.DATASETS.items()
            if spec.incluir_em_todos
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


@router.get("/progresso/diario")
def progresso_diario(s: Session = Depends(get_session)):
    """Progresso do download do Diario Oficial (IOEPA): PDFs ja guardados no
    MinIO por ano x edicoes publicadas no site da IOEPA (total do ano)."""
    from app import datasets

    prefixo = "IOEPA/diario_oficial/documentos/"
    contagem: dict[int, int] = {}
    try:
        for obj in storage.client().list_objects(
            config.MINIO_BUCKET, prefix=prefixo, recursive=True
        ):
            partes = obj.object_name[len(prefixo):].split("/")
            if len(partes) >= 2 and partes[0].isdigit():
                contagem[int(partes[0])] = contagem.get(int(partes[0]), 0) + 1
    except S3Error:
        log.exception("Falha ao listar o diario no MinIO")
        raise HTTPException(502, "MinIO indisponivel no momento")

    anos = []
    for ano in sorted(contagem):
        try:
            total = len(datasets._diario_edicoes(ano))
        except Exception:
            total = None
        baixados = contagem[ano]
        anos.append({
            "ano": ano,
            "baixados": baixados,
            "total": total,
            "percentual": min(100, round(baixados * 100 / total, 1)) if total else None,
        })
    soma_b = sum(a["baixados"] for a in anos)
    soma_t = sum(a["total"] or 0 for a in anos)
    job = s.execute(
        select(ScrapingJob)
        .where(ScrapingJob.tipo == "diario_oficial")
        .order_by(ScrapingJob.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {
        "anos": anos,
        "baixados": soma_b,
        "total": soma_t,
        "percentual": min(100, round(soma_b * 100 / soma_t, 1)) if soma_t else None,
        "situacao": job.status.value if job else None,
    }


_DATASET_PT = {
    "folha": "Folha de pagamento", "despesas": "Despesas", "empenhos": "Notas de empenho",
    "receitas": "Receitas", "diarias": "Diárias", "transf_recursos": "Transferência de recursos",
    "suprimento": "Suprimento de fundos", "obras": "Obras", "areas": "Áreas temáticas",
    "bens_moveis": "Bens móveis", "bens_imoveis": "Bens imóveis",
    "transf_voluntarias": "Convênios", "licitacoes": "Licitações (convênios)",
    "pncp_contratos": "Contratos PNCP", "diario_oficial": "Diário Oficial (IOEPA)",
}


def _parse_unidade(url: str):
    """'transparencia://folha/2025/03/SEDUC' -> ('2025/03', 'SEDUC').
    Sem sufixo de orgao => None (= todos os orgaos)."""
    partes = url.split("://", 1)[-1].split("/")[1:]
    if partes and partes[0] == "snapshot":
        return "snapshot", "/".join(partes[1:]) or None
    if (len(partes) >= 2 and len(partes[0]) == 4 and partes[0].isdigit()
            and len(partes[1]) == 2 and partes[1].isdigit()):
        return "/".join(partes[:2]), "/".join(partes[2:]) or None
    if partes:
        return partes[0], "/".join(partes[1:]) or None
    return "", None


@router.get("/progresso/tarefas")
def progresso_tarefas(s: Session = Depends(get_session)):
    """Tarefas em andamento (e as concluidas ha pouco) com fonte, descricao e
    progresso — crawl por secretaria ou dataset (um ou todos os orgaos)."""
    limite = datetime.now(timezone.utc) - timedelta(minutes=15)
    jobs = s.execute(
        select(ScrapingJob, Secretaria.sigla)
        .join(Secretaria, Secretaria.id == ScrapingJob.secretaria_id)
        .where(
            (ScrapingJob.status == JobStatus.RUNNING)
            | (ScrapingJob.completed_at >= limite)
        )
        .order_by(ScrapingJob.started_at.desc())
        .limit(30)
    ).all()

    tarefas = []
    for job, sigla in jobs:
        cont = dict(
            s.execute(
                select(Documento.status_download, func.count())
                .where(Documento.job_id == job.id)
                .group_by(Documento.status_download)
            ).all()
        )
        ok = cont.get(DownloadStatus.SUCCESS, 0)
        falhas = cont.get(DownloadStatus.FAILED, 0)
        total = max(job.total_pdfs_encontrados or 0, sum(cont.values()))
        feitos = ok + falhas
        rodando = job.status == JobStatus.RUNNING
        t = {
            "job_id": str(job.id),
            "situacao": job.status.value,
            "tipo": job.tipo,
            "concluidos": feitos,
            "falhas": falhas,
            "total": total,
            "detalhes": [],
        }
        if job.tipo == "crawl":
            t["fonte"] = sigla
            t["descricao"] = "varredura do site (PDFs)"
            t["percentual"] = min(100, round(feitos * 100 / total, 1)) if total else None
            t["texto"] = (
                f"{feitos} de {total} PDFs encontrados" + (" até agora — ainda descobrindo" if rodando else "")
                if total else ("procurando PDFs…" if rodando else "nenhum PDF novo")
            )
        else:
            urls = s.execute(
                select(Documento.url_origem).where(Documento.job_id == job.id).limit(3000)
            ).scalars().all()
            periodos, orgaos = set(), set()
            for u in urls:
                chave, org = _parse_unidade(u)
                if chave:
                    periodos.add(chave)
                if org:
                    orgaos.update(org.split("+"))
            ordenados = sorted(orgaos)
            quais = (
                "todos os órgãos" if not ordenados
                else ", ".join(ordenados[:6]) + (f" +{len(ordenados) - 6}" if len(ordenados) > 6 else "")
            )
            per = sorted(periodos)
            periodo = "" if not per else (per[0] if len(per) == 1 else f"{per[0]} a {per[-1]}")
            t["fonte"] = _DATASET_PT.get(job.tipo, job.tipo)
            t["descricao"] = quais + (f" · {periodo}" if periodo else "")
            t["percentual"] = min(100, round(feitos * 100 / total, 1)) if total else None
            t["texto"] = (
                f"{feitos} de {total} partes concluídas"
                if total else ("descobrindo períodos…" if rodando else "nada novo para coletar")
            )
            if job.tipo == "diario_oficial":
                d = progresso_diario(s)
                t["percentual"] = d["percentual"]
                t["concluidos"], t["total"] = d["baixados"], d["total"]
                t["detalhes"] = d["anos"]
                t["texto"] = f"{d['baixados']} de {d['total']} edições baixadas"
        tarefas.append(t)

    em_curso = [t for t in tarefas if t["situacao"] == "RUNNING"]
    soma_total = sum(t["total"] for t in em_curso)
    soma_feito = sum(t["concluidos"] for t in em_curso)
    return {
        "tarefas": tarefas,
        "geral": {
            "tarefas_rodando": len(em_curso),
            "concluidos": soma_feito,
            "total": soma_total,
            "percentual": min(100, round(soma_feito * 100 / soma_total, 1)) if soma_total else None,
        },
    }
 