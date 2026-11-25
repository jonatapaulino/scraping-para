"""Testa as correcoes 1 (caminho unico) e 2 (contador idempotente) contra um
Postgres descartavel. Rodar dentro da imagem do projeto com /srv/app montado."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app import tasks
from app.database import SessionLocal, init_db
from app.models import Documento, DownloadStatus, JobPending, JobStatus, ScrapingJob, Secretaria

ok = lambda cond, msg: print(("OK   " if cond else "FALHA"), msg) or cond
falhas = 0


def check(cond, msg):
    global falhas
    if not ok(cond, msg):
        falhas += 1


# ---------- 1. caminho unico ----------
a = "https://www.igepps.pa.gov.br/sites/default/files/2023-01/RelatorioExtracaoMensalSinteticoPensionistas.pdf"
b = "https://www.igepps.pa.gov.br/sites/default/files/RelatorioExtracaoMensalSinteticoPensionistas.pdf"
pa, pb = tasks._minio_path("IGEPPS", a), tasks._minio_path("IGEPPS", b)
print(" ", pa, "\n ", pb)
check(pa != pb, "mesmo nome de arquivo em URLs diferentes -> caminhos diferentes")
check("/2023/" in pa, "pasta '2023-01' reconhecida como ano 2023")
check(tasks._minio_path("IGEPPS", a) == pa, "deterministico (mesma URL = mesmo caminho)")
p = tasks._minio_path("SEFA", "https://sefa.pa.gov.br/wp-content/uploads/2024/05/edital.pdf")
check("/sefa/2024/edital__" in p and p.endswith(".pdf"), f"wp-content 2024 -> {p}")
p = tasks._minio_path("X", "https://x.gov.br/node/1234/arquivo.pdf")
check("/unknown/" in p, f"id '1234' nao vira ano -> {p}")
p = tasks._minio_path("X", "https://x.gov.br/baixar.php?id=7")
check(p.split("/")[-1].startswith("doc-") and "__" not in p, f"fallback doc-<hash> sem tag dupla -> {p}")

# ---------- 2. contador idempotente ----------
init_db()
with SessionLocal() as s:
    sec = Secretaria(sigla=f"T{uuid.uuid4().hex[:6]}".upper(), url_base="http://x/")
    s.add(sec); s.flush()
    job = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING)
    s.add(job); s.flush()
    s.add(JobPending(job_id=job.id, pending=2))
    velho = datetime.now(timezone.utc) - timedelta(hours=48)
    da = Documento(secretaria_id=sec.id, job_id=job.id, url_origem=f"http://x/{uuid.uuid4()}.pdf",
                   status_download=DownloadStatus.PENDING, created_at=velho)
    db = Documento(secretaria_id=sec.id, job_id=job.id, url_origem=f"http://x/{uuid.uuid4()}.pdf",
                   status_download=DownloadStatus.PENDING)
    s.add_all([da, db]); s.commit()
    job_id, ida, idb = job.id, da.id, db.id


def pend():
    with SessionLocal() as s:
        return s.get(JobPending, job_id).pending


def status_job():
    with SessionLocal() as s:
        return s.get(ScrapingJob, job_id).status


r = tasks.reconcile_stalled()
check(pend() == 1, f"reconciliador fecha o doc orfao antigo (pending={pend()}, {r})")
tasks.reconcile_stalled()
check(pend() == 1, "reconciliador de novo: nao desconta duas vezes")
check(status_job() == JobStatus.RUNNING, "doc recente (B) nao foi morto pelo reconciliador")

with SessionLocal() as s:  # a task real termina DEPOIS do reconciliador
    tasks._fecha_documento(s, ida, job_id, status_download=DownloadStatus.SUCCESS)
with SessionLocal() as s:
    st_a = s.get(Documento, ida).status_download
check(st_a == DownloadStatus.SUCCESS and pend() == 1,
      f"task tardia grava SUCCESS sem descontar de novo (pending={pend()})")
with SessionLocal() as s:  # reentrega
    tasks._fecha_documento(s, ida, job_id, status_download=DownloadStatus.SUCCESS)
check(pend() == 1, "reentrega da mesma task nao desconta")

# falha definitiva via DLX (broker inalcancavel -> grava dead_letter local)
tasks.download_document.on_failure(
    RuntimeError("teste"), "tid", [{"job_id": str(job_id), "documento_id": str(idb), "url": "x"}], {}, None
)
check(pend() == 0 and status_job() == JobStatus.COMPLETED,
      f"on_failure fecha o ultimo doc -> job COMPLETED (pending={pend()})")
tasks.download_document.on_failure(
    RuntimeError("teste"), "tid", [{"job_id": str(job_id), "documento_id": str(idb), "url": "x"}], {}, None
)
check(pend() == 0, f"on_failure repetido nao deixa contador negativo (pending={pend()})")

# force: doc re-enfileirado ganha created_at novo e nao e morto pelo reconciliador
with SessionLocal() as s:
    s.execute(update(Documento).where(Documento.id == ida)
              .values(status_download=DownloadStatus.PENDING, created_at=tasks.func.now()))
    s.execute(update(JobPending).where(JobPending.job_id == job_id).values(pending=1))
    s.commit()
tasks.reconcile_stalled()
with SessionLocal() as s:
    st_a = s.get(Documento, ida).status_download
check(st_a == DownloadStatus.PENDING and pend() == 1, "doc forcado (created_at=agora) sobrevive ao reconciliador")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
