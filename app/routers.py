import json
import logging
import os
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import coleta, config, storage, transparencia
from app.database import SessionLocal
from app.models import (
    ColetaFila, CrawledPage, Documento, DownloadStatus, JobPending, JobStatus, ScrapingJob, Secretaria,
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
    ColetaIn,
    SecretariaIn,
    SecretariaOut,
    SecretariaUpdate,
)
from app.tasks import (
    ERROS_PERMANENTES, avancar_fila, descartar_falhas, descartar_paginas_fila_erros,
    desconectar_drive, drive_conectado, pasta_drive_do_link, salvar_conexao_drive, start_envio_drive,
    fonte_do_documento, refazer_falhas,
    requeue_dead_letters, start_coleta_job, start_dataset_job, start_job_for_secretaria,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def get_session():
    with SessionLocal() as s:
        yield s


_CACHE_CURTO: dict = {}


def _cache_curto(chave, ttl: float, fn):
    """Reaproveita o resultado por `ttl` segundos. O painel consulta a cada 5s
    (e cada aba aberta multiplica): sem isso cada consulta abria uma conexao
    nova no RabbitMQ e listava todos os PDFs do Diario no MinIO."""
    agora = time.monotonic()
    hit = _CACHE_CURTO.get(chave)
    if hit and agora - hit[0] < ttl:
        return hit[1]
    valor = fn()
    _CACHE_CURTO[chave] = (agora, valor)
    return valor


# fontes "virtuais" da tela inicial que disparam um dataset (nao um crawl)
_FONTES_DATASET = {"IOEPA": "diario_oficial"}


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
        if sec.sigla not in _FONTES_DATASET and sec.tipo not in ("crawl", "coleta"):
            raise HTTPException(
                400,
                f"'{sigla}' e fonte de API, nao portal — use POST /api/v1/folha/start",
            )
        secs.append(sec)
    ano_atual = datetime.now(timezone.utc).year
    ano_ini = req.ano_ini or ano_atual - 1
    ano_fim = req.ano_fim or ano_atual
    if ano_ini > ano_fim:
        raise HTTPException(400, "Ano inicial maior que o final")
    job_ids = []
    for sec in secs:
        if sec.tipo == "coleta":
            job = start_coleta_job(s, sec)
        elif sec.sigla in _FONTES_DATASET:
            job = start_dataset_job(
                s, _FONTES_DATASET[sec.sigla], ano_ini, 1, ano_fim, 12, None,
                req.force_full_scrape,
            )
        else:
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
        sem_dados=counts.get(DownloadStatus.SEM_DADOS, 0),
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, s: Session = Depends(get_session)):
    """Cancela um job em andamento. O que ja foi baixado fica; as tarefas que
    ainda estao na fila viram no-op. Documentos PENDING sao removidos para que
    uma nova execucao os redescubra (a dedup e por URL)."""
    job = s.get(ScrapingJob, job_id)
    if job is None:
        raise HTTPException(404, "Job nao encontrado")
    if job.status != JobStatus.RUNNING:
        raise HTTPException(409, f"Job nao esta em andamento ({job.status.value})")
    job.status = JobStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    # Pendentes que JA tinham arquivo (re-coleta forcada) voltam a SUCCESS —
    # antes eram apagados e o historico do arquivo ja baixado se perdia.
    restaurados = s.execute(
        update(Documento)
        .where(
            Documento.job_id == job_id,
            Documento.status_download == DownloadStatus.PENDING,
            Documento.minio_path.is_not(None),
        )
        .values(status_download=DownloadStatus.SUCCESS)
    ).rowcount
    # os nunca baixados saem, para uma nova execucao redescobri-los (dedup por URL)
    removidos = s.execute(
        delete(Documento).where(
            Documento.job_id == job_id, Documento.status_download == DownloadStatus.PENDING
        )
    ).rowcount
    s.commit()
    if restaurados:
        log.info("Cancelamento do job %s: %d documentos ja baixados mantidos", job_id, restaurados)
    return {"job_id": job_id, "status": job.status, "pendentes_removidos": removidos}


@router.get("/jobs/{job_id}/download")
def download_job(job_id: uuid.UUID, s: Session = Depends(get_session)):
    """Zip com todos os arquivos (PDF/CSV) ja baixados do job."""
    job = s.get(ScrapingJob, job_id)
    if job is None:
        raise HTTPException(404, "Job nao encontrado")
    paths = s.execute(
        select(Documento.minio_path)
        .where(
            Documento.job_id == job_id,
            Documento.status_download == DownloadStatus.SUCCESS,
            Documento.minio_path.is_not(None),
        )
        .distinct()
    ).scalars().all()
    if not paths:
        raise HTTPException(404, "Esta tarefa ainda nao tem arquivos baixados")
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", allowZip64=True) as zf:
            for mp in paths:
                key = storage.chave(mp)
                tipo = zipfile.ZIP_DEFLATED if key.lower().endswith(".csv") else zipfile.ZIP_STORED
                if not storage.existe(key):
                    log.warning("Arquivo ausente ao montar zip: %s", key)
                    continue
                zf.write(storage.caminho(key), arcname=key, compress_type=tipo)
    except Exception:
        os.remove(tmp_path)
        raise
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"tarefa-{str(job_id)[:8]}.zip",
        background=BackgroundTask(os.remove, tmp_path),
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
            sem_dados=counts.get(j.id, {}).get(DownloadStatus.SEM_DADOS, 0),
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
        queues=_cache_curto("filas", 10, broker.queue_depths),
        documents={k.value: v for k, v in docs.items()},
        jobs={k.value: v for k, v in jobs.items()},
        secretarias_ativas=ativas,
        last_24h=last_24h,
    )


def _tam(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}".replace(".", ",")
        n /= 1024
    return ""


def _progresso_drive(job: ScrapingJob) -> dict:
    p = json.loads(job.progresso_json or "{}")
    feitos, total, vist = p.get("feitos") or 0, p.get("total") or 0, p.get("verificados") or 0
    fase = p.get("fase")
    if fase == "falhou":
        texto = "falhou: " + (p.get("erro") or "erro desconhecido")
    elif fase == "concluido":
        texto = f"{feitos} arquivo(s) novo(s) enviado(s) ({_tam(p.get('bytes'))}) · {vist} já estavam no Drive"
    elif fase == "enviando":
        texto = f"{feitos} de {total} arquivo(s) novo(s) enviados · {_tam(p.get('bytes'))} de {_tam(p.get('total_bytes'))}"
    else:
        texto = f"comparando com o Drive… {vist} arquivo(s) já estão lá"
    if p.get("erros"):
        texto += f" · {p['erros']} erro(s)"
    return {"feitos": feitos, "total": total, "texto": texto,
            "percentual": min(100, round(feitos * 100 / total, 1)) if total else None}


def _nome_tarefa(job: ScrapingJob, sigla: str) -> str:
    if job.tipo == "crawl":
        return f"{sigla} (varredura do portal)"
    if job.tipo == "coleta":
        return f"{sigla} (fonte de dados)"
    return _DATASET_PT.get(job.tipo, job.tipo)


@router.get("/resumo")
def resumo_painel(s: Session = Depends(get_session)):
    """Cards do topo do painel, em linguagem de quem usa: o que esta rodando e em
    que ponto, a fila de coletas, quanto ja esta guardado e quanto espaco sobra."""
    from app import broker

    rodando = s.execute(
        select(ScrapingJob, Secretaria.sigla)
        .join(Secretaria, Secretaria.id == ScrapingJob.secretaria_id)
        .where(ScrapingJob.status == JobStatus.RUNNING)
        .order_by(ScrapingJob.started_at)
    ).all()
    agora_lista = []
    for job, sigla in rodando:
        cont = dict(
            s.execute(
                select(Documento.status_download, func.count())
                .where(Documento.job_id == job.id)
                .group_by(Documento.status_download)
            ).all()
        )
        feitos = sum(v for k, v in cont.items() if k != DownloadStatus.PENDING)
        total = max(job.total_pdfs_encontrados or 0, sum(cont.values()))
        if job.tipo == "drive":
            pd = _progresso_drive(job)
            feitos, total = pd["feitos"], max(pd["total"], 1)
        agora_lista.append({
            "nome": _nome_tarefa(job, sigla),
            "feitos": feitos,
            "total": total,
            "percentual": min(100, round(feitos * 100 / total, 1)) if total else None,
            "desde": job.started_at.isoformat() if job.started_at else None,
        })

    fila = dict(s.execute(select(ColetaFila.status, func.count()).group_by(ColetaFila.status)).all())
    item_atual = s.execute(
        select(ColetaFila).where(ColetaFila.status == "RODANDO").order_by(ColetaFila.id).limit(1)
    ).scalar_one_or_none()
    total_fila = sum(fila.values())
    terminadas = sum(fila.get(k, 0) for k in ("CONCLUIDA", "FALHOU", "CANCELADA"))

    docs = dict(
        s.execute(select(Documento.status_download, func.count()).group_by(Documento.status_download)).all()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    ok_24h = s.execute(
        select(func.count(Documento.id)).where(
            Documento.status_download == DownloadStatus.SUCCESS, Documento.downloaded_at > cutoff
        )
    ).scalar_one()
    falhas_24h = s.execute(
        select(func.count(Documento.id)).where(
            Documento.status_download == DownloadStatus.FAILED, Documento.created_at > cutoff
        )
    ).scalar_one()
    filas = _cache_curto("filas", 10, broker.queue_depths)
    try:
        guardado = _cache_curto("resumo:arquivos", 60, storage.resumo)
    except OSError:
        guardado = {"arquivos": None, "bytes": None, "livre_bytes": None}

    return {
        "versao": config.APP_VERSION,
        "agora": agora_lista,
        "fila": {
            "total": total_fila,
            "terminadas": terminadas,
            "aguardando": fila.get("AGUARDANDO", 0),
            "falharam": fila.get("FALHOU", 0),
            "atual": _DATASET_PT.get(item_atual.dataset, item_atual.dataset) if item_atual else None,
            "posicao_atual": terminadas + 1 if item_atual else None,
        },
        "guardado": guardado,
        "ultimas_24h": {"baixados": ok_24h, "falhas": falhas_24h},
        "falhas": {
            "total": docs.get(DownloadStatus.FAILED, 0),
            "na_fila_de_erros": filas.get(config.Q_DEAD, -1),
        },
        "sem_dados": docs.get(DownloadStatus.SEM_DADOS, 0),
        "tecnico": {
            "fila_descoberta": filas.get(config.Q_DISCOVERY, -1),
            "fila_download": filas.get(config.Q_DOWNLOAD, -1),
            "fila_erros": filas.get(config.Q_DEAD, -1),
            "tarefas_rodando": len(rodando),
        },
    }


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
        pdf_filtro=req.pdf_filtro,
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
    dados = req.model_dump(exclude_unset=True)
    if dados.get("agendada") or "agenda_hora" in dados:
        if sec.tipo not in ("crawl", "coleta") and sec.sigla not in _FONTES_DATASET:
            raise HTTPException(
                400, f"'{sec.sigla}' nao pode ser agendada (so portais, fontes de coleta e o Diario Oficial)"
            )
    if dados.get("agendada") is None:
        dados.pop("agendada", None)
    if dados.get("agenda_hora") is None:
        dados.pop("agenda_hora", None)
    estava_agendada = sec.agendada
    for field, value in dados.items():
        setattr(sec, field, value)
    if sec.agendada and (not estava_agendada or "agenda_hora" in dados):
        # ligou a agenda ou mudou o horario: so vale a partir de agora
        sec.agendada_em = datetime.now(timezone.utc)
    s.commit()
    s.refresh(sec)
    return sec


@router.post("/coletas/testar")
def testar_coleta(cfg: coleta.ColetaConfig):
    """Pre-visualiza a coleta (1a pagina, sem gravar nada): colunas + primeiras linhas."""
    try:
        return coleta.testar(cfg)
    except Exception as exc:
        log.info("Teste de coleta falhou: %s", exc)
        raise HTTPException(400, _erro_legivel(exc))


def _erro_legivel(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"O site respondeu HTTP {exc.response.status_code} para {exc.request.url}"
    if isinstance(exc, httpx.HTTPError):
        return f"Nao consegui acessar o site: {exc}"
    if isinstance(exc, ValueError) and "Expecting value" in str(exc):
        return "A resposta nao e JSON — se e uma pagina web, use o modo 'tabela HTML'"
    return str(exc)


def _destino_ok(cfg: coleta.ColetaConfig) -> None:
    try:
        coleta.checa_destino(cfg.url.replace("{page}", str(cfg.pagina_inicial)))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/coletas", response_model=SecretariaOut, status_code=status.HTTP_201_CREATED)
def create_coleta(req: ColetaIn, s: Session = Depends(get_session)):
    cfg = coleta.ColetaConfig(**req.model_dump(exclude={"sigla", "is_ativa"}))
    _destino_ok(cfg)
    sec = Secretaria(
        sigla=req.sigla.strip().upper(), url_base=cfg.url[:255], is_ativa=req.is_ativa,
        tipo="coleta", coleta_json=cfg.model_dump_json(),
    )
    s.add(sec)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, f"Fonte '{req.sigla}' ja existe")
    s.refresh(sec)
    return sec


@router.put("/coletas/{sec_id}", response_model=SecretariaOut)
def update_coleta(sec_id: uuid.UUID, cfg: coleta.ColetaConfig, s: Session = Depends(get_session)):
    sec = s.get(Secretaria, sec_id)
    if sec is None or sec.tipo != "coleta":
        raise HTTPException(404, "Fonte de coleta nao encontrada")
    _destino_ok(cfg)
    sec.url_base = cfg.url[:255]
    sec.coleta_json = cfg.model_dump_json()
    s.commit()
    s.refresh(sec)
    return sec


@router.delete("/secretarias/{sec_id}")
def delete_secretaria(sec_id: uuid.UUID, s: Session = Depends(get_session)):
    """Exclui a fonte e o historico dela no banco (jobs, documentos, paginas
    visitadas). Os arquivos ja baixados (MinIO e downloads/) NAO sao apagados."""
    sec = s.get(Secretaria, sec_id)
    if sec is None:
        raise HTTPException(404, "Secretaria nao encontrada")
    if sec.tipo not in ("crawl", "coleta") or sec.sigla in _FONTES_DATASET:
        raise HTTPException(400, f"'{sec.sigla}' e uma fonte do sistema — nao pode ser excluida")
    rodando = s.execute(
        select(func.count(ScrapingJob.id)).where(
            ScrapingJob.secretaria_id == sec_id, ScrapingJob.status == JobStatus.RUNNING
        )
    ).scalar_one()
    if rodando:
        raise HTTPException(409, "Ha tarefa em andamento — cancele antes de excluir")
    job_ids = select(ScrapingJob.id).where(ScrapingJob.secretaria_id == sec_id)
    n_docs = s.execute(delete(Documento).where(Documento.secretaria_id == sec_id)).rowcount
    s.execute(delete(JobPending).where(JobPending.job_id.in_(job_ids)))
    s.execute(delete(CrawledPage).where(CrawledPage.job_id.in_(job_ids)))
    s.execute(delete(ScrapingJob).where(ScrapingJob.secretaria_id == sec_id))
    s.delete(sec)
    try:
        s.commit()
    except IntegrityError:
        # documento de OUTRA fonte ainda aponta pra uma tarefa desta (re-coleta
        # forcada da mesma URL) — antes virava erro 500 sem explicacao
        s.rollback()
        raise HTTPException(
            409,
            f"Nao foi possivel excluir {sec.sigla}: ha documentos de outra fonte "
            "ligados as tarefas dela. Desative a fonte em vez de excluir.",
        )
    return {"excluida": sec.sigla, "documentos_removidos": n_docs}


# ---------- falhas: ver, refazer e descartar pelo painel ----------

SEM_MOTIVO = "Sem detalhe (falha anterior ao registro de motivos)"


def _nome_fonte(chave: str) -> str:
    return _DATASET_PT.get(chave, chave)


@router.get("/falhas")
def listar_falhas(s: Session = Depends(get_session)):
    """Falhas agrupadas por fonte e motivo, com as mais recentes como exemplo."""
    rows = s.execute(
        select(Documento.url_origem, Documento.erro, Documento.created_at, Secretaria.sigla)
        .join(Secretaria, Secretaria.id == Documento.secretaria_id)
        .where(Documento.status_download == DownloadStatus.FAILED)
        .order_by(Documento.created_at.desc())
    ).all()
    grupos: dict = {}
    for url, erro, quando, sigla in rows:
        fonte = fonte_do_documento(url, sigla)
        motivo = (erro or "")[:90]
        g = grupos.setdefault((fonte, motivo), {
            "fonte": fonte, "fonte_nome": _nome_fonte(fonte),
            "motivo": motivo, "motivo_texto": erro or SEM_MOTIVO,
            "permanente": bool(erro and erro.startswith(ERROS_PERMANENTES)),
            "quantidade": 0, "exemplos": [],
        })
        g["quantidade"] += 1
        if len(g["exemplos"]) < 3:
            g["exemplos"].append({"url": url, "quando": quando.isoformat() if quando else None})
    lista = sorted(grupos.values(), key=lambda g: (-g["quantidade"], g["fonte_nome"]))
    permanentes = sum(g["quantidade"] for g in lista if g["permanente"])
    from app import broker
    return {
        # paginas de portal (e outras tarefas) que esgotaram as tentativas
        "fila_erros": _cache_curto("filas", 10, broker.queue_depths).get(config.Q_DEAD, -1),
        "total": len(rows),
        "permanentes": permanentes,
        "refaziveis": len(rows) - permanentes,
        "grupos": lista,
    }


class FalhasAcao(BaseModel):
    fonte: Optional[str] = None
    motivo: Optional[str] = None       # prefixo do motivo do grupo ("" = sem motivo)
    incluir_permanentes: bool = False  # refazer: tenta ate os links quebrados
    so_permanentes: bool = False       # descartar: so links quebrados / nao-PDF


@router.post("/falhas/refazer")
def refazer(req: FalhasAcao, s: Session = Depends(get_session)):
    """Tenta de novo as falhas numa tarefa nova (aparece em 'Coleta agora').
    A fila de erros tambem e esvaziada: paginas voltam para a fila, documentos
    entram nesta tarefa."""
    reenviadas = requeue_dead_letters(limit=100000, pular_documentos=True)
    job, n, sem_como = refazer_falhas(s, req.fonte, req.motivo, req.incluir_permanentes)
    return {"job_id": str(job.id) if job else None, "refeitos": n,
            "sem_como_refazer": sem_como, "paginas_reenviadas": reenviadas}


@router.post("/falhas/descartar")
def descartar(req: FalhasAcao, s: Session = Depends(get_session)):
    """Tira falhas da lista (nao contam mais e nao sao refeitas)."""
    n = descartar_falhas(s, req.fonte, req.motivo, req.so_permanentes)
    return {"descartadas": n}


@router.post("/falhas/descartar-paginas")
def descartar_paginas():
    """Tira da fila de erros as paginas de portal que nao responderam."""
    _CACHE_CURTO.pop("filas", None)
    return {"descartadas": descartar_paginas_fila_erros()}


# ---------- Google Drive: conectar a conta e enviar ----------


class DriveToken(BaseModel):
    token: str


class DriveEnvio(BaseModel):
    link: str
    subpasta: str = ""


@router.get("/drive")
def drive_estado(s: Session = Depends(get_session)):
    try:
        pastas = sorted(p.name for p in storage.raiz().iterdir()
                        if p.is_dir() and not p.name.startswith((".", "_")))
    except OSError:
        pastas = []
    ult = s.execute(
        select(ScrapingJob).where(ScrapingJob.tipo == "drive").order_by(ScrapingJob.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    ultimo = None
    if ult:
        ultimo = {"situacao": ult.status.value, "texto": _progresso_drive(ult)["texto"],
                  "iniciada_em": ult.started_at.isoformat() if ult.started_at else None,
                  "concluida_em": ult.completed_at.isoformat() if ult.completed_at else None}
    return {"conectado": drive_conectado(), "pastas": pastas, "ultimo": ultimo}


@router.post("/drive/conectar")
def drive_conectar(req: DriveToken):
    try:
        salvar_conexao_drive(req.token)
    except json.JSONDecodeError:
        raise HTTPException(400, "Código inválido: copie o texto inteiro, de { até }")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"conectado": True}


@router.post("/drive/desconectar")
def drive_desconectar():
    desconectar_drive()
    return {"conectado": False}


@router.post("/drive/enviar", status_code=status.HTTP_202_ACCEPTED)
def drive_enviar(req: DriveEnvio, s: Session = Depends(get_session)):
    if not drive_conectado():
        raise HTTPException(400, "Conecte a conta Google primeiro (em “Conectar conta Google”)")
    pasta_id = pasta_drive_do_link(req.link)
    if not pasta_id:
        raise HTTPException(400, "Link inválido: cole o link da PASTA do Drive (…/drive/folders/…)")
    sub = req.subpasta.strip().strip("/")
    if sub:
        try:
            ok = storage.caminho(sub).is_dir()
        except ValueError:
            ok = False
        if not ok:
            raise HTTPException(400, f"A pasta '{sub}' não existe nos arquivos guardados")
    rodando = s.execute(select(func.count(ScrapingJob.id)).where(
        ScrapingJob.tipo == "drive", ScrapingJob.status == JobStatus.RUNNING)).scalar_one()
    if rodando:
        raise HTTPException(409, "Já há um envio para o Drive em andamento — espere terminar ou cancele")
    job = start_envio_drive(s, pasta_id, sub)
    return {"job_id": str(job.id)}


def _ultima_versao() -> dict:
    import httpx

    r = httpx.get(f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest",
                  timeout=8, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    d = r.json()
    return {"ultima": d["tag_name"].lstrip("v"), "url": d["html_url"], "publicada_em": d.get("published_at")}


def _semver(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return None


@router.get("/versao")
def versao():
    """Versao em uso x ultima publicada no GitHub (consulta guardada por 1 h)."""
    atual = config.APP_VERSION
    try:
        info = _cache_curto("versao:ultima", 3600, _ultima_versao)
    except Exception:
        return {"atual": atual, "ultima": None, "nova_disponivel": False}
    a, u = _semver(atual), _semver(info["ultima"])
    return {"atual": atual, **info, "nova_disponivel": bool(a and u and u > a)}


@router.post("/dead-letters/requeue")
def requeue_dead(limit: int = 100):
    """Republica mensagens da q.dead na fila original (novo ciclo de retries)."""
    n = requeue_dead_letters(limit=limit)
    return {"requeued": n}


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: uuid.UUID, download: bool = False, s: Session = Depends(get_session)):
    """Faz proxy do arquivo guardado no MinIO (evita presigned URL com host interno)."""
    doc = s.get(Documento, doc_id)
    if doc is None or not doc.minio_path:
        raise HTTPException(404, "Documento nao baixado")
    key = storage.chave(doc.minio_path)
    if not storage.existe(key):
        raise HTTPException(404, "Arquivo nao esta mais na pasta de downloads")
    filename = key.rsplit("/", 1)[-1]
    media_type = "text/csv" if filename.lower().endswith(".csv") else "application/pdf"
    return FileResponse(
        storage.caminho(key),
        media_type=media_type,
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
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


# ---------- fila automatica de coletas (uma por vez) ----------


@router.get("/fila")
def listar_fila(s: Session = Depends(get_session)):
    itens = s.execute(select(ColetaFila).order_by(ColetaFila.id)).scalars().all()
    out = []
    for i in itens:
        orgaos = json.loads(i.orgaos_json) if i.orgaos_json else None
        out.append({
            "id": i.id,
            "dataset": i.dataset,
            "nome": _DATASET_PT.get(i.dataset, i.dataset),
            "periodo": (
                "sem período (inventário atual)"
                if getattr(transparencia.get_spec(i.dataset), "granularity", "") == "snapshot"
                else f"{i.ano_ini}-{i.mes_ini:02d} a {i.ano_fim}-{i.mes_fim:02d}"
            ),
            "orgaos": "todos" if not orgaos else ", ".join(orgaos[:5]) + (f" +{len(orgaos) - 5}" if len(orgaos) > 5 else ""),
            "recoletar": i.force,
            "status": i.status,
            "job_id": str(i.job_id) if i.job_id else None,
            "erro": i.erro,
            "iniciada_em": i.iniciada_em.isoformat() if i.iniciada_em else None,
            "concluida_em": i.concluida_em.isoformat() if i.concluida_em else None,
        })
    return out


@router.post("/fila", status_code=status.HTTP_201_CREATED)
def enfileirar(req: FolhaStartRequest, s: Session = Depends(get_session)):
    """Poe coletas na fila automatica. dataset '*' = um item por dataset
    (os mesmos de 'todos os datasets'), executados um depois do outro."""
    if (req.ano_ini, req.mes_ini) > (req.ano_fim, req.mes_fim):
        raise HTTPException(400, "Periodo inicial maior que o final")
    if req.dataset == "*":
        slugs = [k for k, v in transparencia.DATASETS.items() if v.incluir_em_todos]
    elif transparencia.get_spec(req.dataset) is not None:
        slugs = [req.dataset]
    else:
        raise HTTPException(400, f"Dataset '{req.dataset}' desconhecido")
    orgaos = json.dumps(req.orgaos, ensure_ascii=False) if req.orgaos else None
    for slug in slugs:
        s.add(ColetaFila(
            dataset=slug, ano_ini=req.ano_ini, mes_ini=req.mes_ini,
            ano_fim=req.ano_fim, mes_fim=req.mes_fim, orgaos_json=orgaos, force=req.force,
        ))
    s.commit()
    try:
        avancar_fila.apply_async()  # comeca ja, sem esperar o proximo minuto
    except Exception:
        log.warning("Fila: nao consegui acionar agora — o Beat aciona em ate 1 minuto")
    return {"adicionados": len(slugs)}


@router.delete("/fila/{item_id}")
def remover_da_fila(item_id: int, s: Session = Depends(get_session)):
    item = s.get(ColetaFila, item_id)
    if item is None:
        raise HTTPException(404, "Item nao encontrado")
    if item.status == "RODANDO":
        raise HTTPException(409, "Esta coleta esta rodando — cancele a tarefa no progresso; a fila segue para a proxima")
    s.delete(item)
    s.commit()
    return {"removido": item_id}


@router.post("/fila/esvaziar")
def esvaziar_fila(s: Session = Depends(get_session)):
    """Tira da fila tudo o que AINDA NAO comecou (a que esta rodando continua —
    para ela, use cancelar no progresso)."""
    n = s.execute(delete(ColetaFila).where(ColetaFila.status == "AGUARDANDO")).rowcount
    s.commit()
    return {"removidos": n}


@router.post("/fila/limpar")
def limpar_fila(s: Session = Depends(get_session)):
    """Tira da lista os itens ja terminados (concluidos, com falha ou cancelados)."""
    n = s.execute(
        delete(ColetaFila).where(ColetaFila.status.in_(("CONCLUIDA", "FALHOU", "CANCELADA")))
    ).rowcount
    s.commit()
    return {"removidos": n}


@router.get("/progresso/diario")
def progresso_diario(s: Session = Depends(get_session)):
    return _dados_diario(s)


def _dados_diario(s: Session, anos_filtro: Optional[list] = None):
    """Progresso do download do Diario Oficial (IOEPA): PDFs ja guardados na
    pasta de downloads por ano x edicoes publicadas no site da IOEPA (total do ano).
    anos_filtro: considera so esses anos (progresso de UMA tarefa)."""
    from app import datasets

    prefixo = "IOEPA/diario_oficial/documentos/"

    def _conta() -> dict[int, int]:
        out: dict[int, int] = {}
        for key in storage.listar(prefixo.rstrip("/")):
            partes = key[len(prefixo):].split("/")
            if len(partes) >= 2 and partes[0].isdigit():
                out[int(partes[0])] = out.get(int(partes[0]), 0) + 1
        return out

    try:
        contagem = _cache_curto("diario:contagem", 20, _conta)
    except OSError:
        log.exception("Falha ao listar o diario na pasta de downloads")
        raise HTTPException(502, "Pasta de downloads indisponivel no momento")

    anos = []
    for ano in sorted(contagem):
        if anos_filtro and ano not in anos_filtro:
            continue
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
    "refazer": "Nova tentativa das falhas",
    "drive": "Envio para o Google Drive",
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
    limite = datetime.now(timezone.utc) - timedelta(hours=24)
    jobs = s.execute(
        select(ScrapingJob, Secretaria.sigla)
        .join(Secretaria, Secretaria.id == ScrapingJob.secretaria_id)
        .where(
            (ScrapingJob.status == JobStatus.RUNNING)
            | ((ScrapingJob.completed_at >= limite) & (ScrapingJob.status != JobStatus.CANCELLED))
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
        sem_dados = cont.get(DownloadStatus.SEM_DADOS, 0)
        total = max(job.total_pdfs_encontrados or 0, sum(cont.values()))
        feitos = ok + falhas + sem_dados + cont.get(DownloadStatus.DESCARTADO, 0)
        rodando = job.status == JobStatus.RUNNING
        t = {
            "job_id": str(job.id),
            "situacao": job.status.value,
            "tipo": job.tipo,
            "concluidos": feitos,
            "falhas": falhas,
            "sem_dados": sem_dados,
            "total": total,
            "detalhes": [],
            "iniciada_em": job.started_at.isoformat() if job.started_at else None,
            "concluida_em": job.completed_at.isoformat() if job.completed_at else None,
        }
        if job.tipo == "drive":
            pd = _progresso_drive(job)
            t["fonte"] = _DATASET_PT["drive"]
            sub = json.loads(job.progresso_json or "{}").get("subpasta")
            t["descricao"] = f"pasta {sub}" if sub else "todos os arquivos"
            t.update(concluidos=pd["feitos"], total=pd["total"], percentual=pd["percentual"],
                     texto=pd["texto"], falhas=0)
        elif job.tipo == "refazer":
            t["fonte"] = _DATASET_PT["refazer"]
            t["descricao"] = f"{total} item(ns) com falha tentados de novo"
            t["percentual"] = min(100, round(feitos * 100 / total, 1)) if total else None
            t["texto"] = f"{feitos} de {total} refeitos" + (f" · {ok} recuperados" if ok else "")
        elif job.tipo == "crawl":
            t["fonte"] = sigla
            t["descricao"] = "varredura do site (PDFs)"
            t["percentual"] = min(100, round(feitos * 100 / total, 1)) if total else None
            t["texto"] = (
                f"{feitos} de {total} PDFs encontrados" + (" até agora — ainda descobrindo" if rodando else "")
                if total else ("procurando PDFs…" if rodando else "nenhum PDF novo")
            )
        elif job.tipo == "coleta":
            t["fonte"] = sigla
            t["descricao"] = "coleta de dados (CSV)"
            t["percentual"] = 100 if feitos else None
            t["texto"] = (
                "coletando…" if rodando
                else (
                    "arquivo gerado (ou idêntico ao anterior)" if ok
                    else "o site respondeu sem nenhum registro" if sem_dados
                    else "falhou"
                )
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
                if not ordenados:
                    t["descricao"] = "IOEPA" + (f" · {periodo}" if periodo else "")
                anos_job = sorted({int(p[:4]) for p in periodos if p[:4].isdigit()})
                if anos_job:
                    d = _dados_diario(s, anos_job)
                    t["percentual"] = d["percentual"]
                    t["concluidos"], t["total"] = d["baixados"], d["total"]
                    t["detalhes"] = d["anos"]
                    t["texto"] = f"{d['baixados']} de {d['total']} edições baixadas"
                elif not rodando:
                    # tarefa antiga cujos anos foram reassumidos por uma rodada mais nova
                    t["percentual"] = 100 if job.status == JobStatus.COMPLETED else None
                    t["texto"] = "concluída (os anos dela foram refeitos em uma rodada mais nova)"
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
 