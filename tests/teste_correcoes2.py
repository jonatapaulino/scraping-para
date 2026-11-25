"""Testes das correcoes da 2a rodada, contra um Postgres descartavel."""
import csv
import io
import tempfile
import uuid
from datetime import datetime, timedelta, timezone


from app import coleta, tasks, transparencia
from app.database import SessionLocal, init_db
from app.models import (Documento, DownloadStatus, JobPending, JobStatus,
                        ScrapingJob)

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


init_db()
init_db()  # idempotente (2a vez nao pode quebrar)
check(True, "init_db roda 2x sem erro (enum SEM_DADOS, VARCHAR(32))")

# ---------- nomes de pasta ----------
for rot, esp in [("PCPA", "POLICIA_CIVIL"), ("FUND. SANTA CASA", "FUND_SANTA_CASA"),
                 ("IDEFLOR - BIO", "IDEFLOR-BIO"), ("AGÊNCIA ESTADUAL X", "AGENCIA_ESTADUAL_X"),
                 ("Casa Civil", "GAB_GOV-CASA_CIV"), ("SESPA", "SESPA")]:
    got = transparencia.orgao_slug(rot)
    check(got == esp, f"orgao_slug({rot!r}) = {got!r}")

# ---------- bloqueio de enderecos internos ----------
for url in ["http://minio:9000/x", "http://localhost:8000", "http://127.0.0.1/", "http://[::1]/",
            "http://host.docker.internal:5432", "http://169.254.169.254/latest", "http://0.0.0.0/"]:
    try:
        coleta.checa_destino(url); ok = False
    except ValueError:
        ok = True
    check(ok, f"bloqueia {url}")
try:
    coleta.checa_destino("https://dados.gov.br/api/x"); ok = True
except ValueError:
    ok = False
check(ok, "libera site publico")

# ---------- CSV com uniao de colunas ----------
with tempfile.SpooledTemporaryFile() as tmp:
    total, ncol = tasks._linhas_para_csv(iter([{"a": 1, "b": 2}, {"a": 3, "c": "ç"}]), tmp, uuid.uuid4())
    tmp.seek(0)
    linhas = list(csv.reader(io.TextIOWrapper(tmp, encoding="utf-8-sig")))
check(total == 2 and linhas[0] == ["a", "b", "c"] and linhas[2] == ["3", "", "ç"],
      f"CSV une colunas de todas as linhas: {linhas}")

# ---------- fetch_dataset_mes: sem dados, copia propria, sem regravar igual ----------
publicados = {}
tasks._publica_arquivo = lambda tmp, path, size, ct: publicados.__setitem__(path, size)
DADOS = {"A": [{"x": 1}], "B": [{"x": 1}], "C": []}
transparencia.DATASETS["teste"] = transparencia.DatasetSpec(
    "teste", "ano", lambda: [{"ano": 2025}], lambda a, m: [],
    lambda recorte, siglas: iter(DADOS[siglas[0]]),
)
with SessionLocal() as s:
    sec = tasks.get_or_create_transparencia(s)
    job = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING, tipo="teste")
    s.add(job); s.flush()
    s.add(JobPending(job_id=job.id, pending=3))
    docs = {}
    for o in "ABC":
        d = Documento(secretaria_id=sec.id, job_id=job.id, url_origem=f"transparencia://teste/2025/{o}-{uuid.uuid4()}",
                      status_download=DownloadStatus.PENDING)
        s.add(d); s.flush(); docs[o] = d.id
    s.commit(); job_id = job.id
for o in "ABC":
    tasks.fetch_dataset_mes.run({"dataset": "teste", "job_id": str(job_id), "documento_id": str(docs[o]),
                                 "recorte": {"ano": 2025}, "orgao": o})
with SessionLocal() as s:
    st = {o: s.get(Documento, docs[o]) for o in "ABC"}
    pend = s.get(JobPending, job_id).pending
    jst = s.get(ScrapingJob, job_id).status
check(st["C"].status_download == DownloadStatus.SEM_DADOS, "unidade vazia -> SEM_DADOS (nao FAILED)")
check(st["A"].minio_path != st["B"].minio_path and st["B"].minio_path in publicados,
      f"conteudo identico de outro orgao grava arquivo proprio: {st['B'].minio_path}")
check(st["A"].hash_sha256 and st["B"].hash_sha256 is None, "hash fica com o 1o dono, copia sem hash (UNIQUE)")
check(pend == 0 and jst == JobStatus.COMPLETED, f"job conclui (pending={pend}, {jst})")
# re-coleta forcada com o mesmo conteudo: nao publica de novo
with SessionLocal() as s:
    s.get(Documento, docs["A"]).status_download = DownloadStatus.PENDING
    s.get(JobPending, job_id).pending = 1
    s.get(ScrapingJob, job_id).status = JobStatus.RUNNING
    s.commit()
publicados.clear()
tasks.fetch_dataset_mes.run({"dataset": "teste", "job_id": str(job_id), "documento_id": str(docs["A"]),
                             "recorte": {"ano": 2025}, "orgao": "A"})
check(not publicados, "re-coleta sem mudanca nao regrava o arquivo")

# ---------- cancelar nao apaga o que ja tinha arquivo ----------
from app.routers import cancel_job
with SessionLocal() as s:
    j2 = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING, tipo="teste"); s.add(j2); s.flush()
    ja = Documento(secretaria_id=sec.id, job_id=j2.id, url_origem=f"x://{uuid.uuid4()}",
                   status_download=DownloadStatus.PENDING, minio_path="raw-docs/a.pdf")
    novo = Documento(secretaria_id=sec.id, job_id=j2.id, url_origem=f"x://{uuid.uuid4()}",
                     status_download=DownloadStatus.PENDING)
    s.add_all([ja, novo]); s.commit(); ids = (ja.id, novo.id, j2.id)
with SessionLocal() as s:
    r = cancel_job(ids[2], s)
with SessionLocal() as s:
    check(s.get(Documento, ids[0]).status_download == DownloadStatus.SUCCESS and s.get(Documento, ids[1]) is None,
          f"cancelar: doc ja baixado volta a SUCCESS, nunca baixado sai ({r['pendentes_removidos']} removido)")

# ---------- reconciliador: so zumbi de verdade ----------
velho = datetime.now(timezone.utc) - timedelta(hours=48)
with SessionLocal() as s:
    ativo = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING, tipo="teste", started_at=velho)
    parado = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING, tipo="teste", started_at=velho)
    s.add_all([ativo, parado]); s.flush()
    s.add(Documento(secretaria_id=sec.id, job_id=ativo.id, url_origem=f"x://{uuid.uuid4()}",
                    status_download=DownloadStatus.SUCCESS, created_at=velho,
                    downloaded_at=datetime.now(timezone.utc)))
    s.commit(); ida, idp = ativo.id, parado.id
tasks.reconcile_stalled()
with SessionLocal() as s:
    check(s.get(ScrapingJob, ida).status == JobStatus.RUNNING, "job antigo mas com atividade recente segue RUNNING")
    check(s.get(ScrapingJob, idp).status == JobStatus.FAILED, "job antigo sem atividade vira FAILED")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
