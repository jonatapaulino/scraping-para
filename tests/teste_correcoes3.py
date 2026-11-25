"""Atalhos de 'ja guardado' e re-tentativa de unidades com falha."""
import functools
import http.server
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app import tasks, transparencia
from app.database import SessionLocal, init_db
from app.models import Documento, DownloadStatus, JobPending, JobStatus, ScrapingJob, Secretaria

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


init_db()

# servidor HTTP real com um PDF (HEAD devolve Content-Length e Last-Modified)
pasta = tempfile.mkdtemp()
corpo = b"%PDF-1.4 teste " + b"x" * 1000
os.makedirs(os.path.join(pasta, "2024"))
with open(os.path.join(pasta, "2024", "edital.pdf"), "wb") as f:
    f.write(corpo)
srv = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=pasta))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/2024/edital.pdf"

tam, mod = tasks._cabecalho_remoto(url)
check(tam == len(corpo) and mod is not None, f"HEAD le tamanho/data sem baixar ({tam}, {mod})")

# armazenamento real (pasta temporaria): grava arquivos com o tamanho pedido
from app import storage


class _Guardado(dict):
    def __setitem__(self, key, size):
        storage.salvar_bytes(b"%PDF-" + b"y" * (size - 5) if size >= 5 else b"x" * size, key)
        super().__setitem__(key, size)

    def clear(self):
        for k in list(self):
            storage.remover(k)
        super().clear()


guardado = _Guardado()
baixou = []
tasks._stream_to_tmp = lambda *a, **k: baixou.append(1) or (_ for _ in ()).throw(RuntimeError("baixou"))

with SessionLocal() as s:
    sec = Secretaria(sigla=f"T{uuid.uuid4().hex[:5]}".upper(), url_base="http://127.0.0.1/")
    s.add(sec); s.flush()
    job = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING); s.add(job); s.flush()
    s.add(JobPending(job_id=job.id, pending=10)); s.commit()
    sigla, job_id, sec_id = sec.sigla, job.id, sec.id


def novo_doc(**kw):
    with SessionLocal() as s:
        d = Documento(secretaria_id=sec_id, job_id=job_id, url_origem=f"{url}?v={uuid.uuid4()}",
                      status_download=DownloadStatus.PENDING, **kw)
        s.add(d); s.commit()
        return d.id, d.url_origem


def roda(doc_id, u):
    baixou.clear()
    try:
        tasks.download_document.run({"url": u, "job_id": str(job_id), "documento_id": str(doc_id)})
    except RuntimeError:
        pass
    with SessionLocal() as s:
        return s.get(Documento, doc_id), bool(baixou)


# 1. forcado, arquivo nao mudou (mesmo tamanho, modificado antes do nosso download)
depois = datetime.now(timezone.utc) + timedelta(minutes=1)
did, u = novo_doc()
caminho = tasks._minio_path(sigla, u)
with SessionLocal() as s:
    d = s.get(Documento, did)
    d.minio_path, d.hash_sha256, d.tamanho_bytes, d.downloaded_at = caminho, uuid.uuid4().hex * 2, len(corpo), depois
    s.commit()
guardado[caminho.split("/", 1)[1]] = len(corpo)
d, b = roda(did, u)
check(d.status_download == DownloadStatus.SUCCESS and not b, "forcado + site sem mudanca -> NAO baixa")

# 1b. forcado, site diz que mudou (tamanho diferente) -> baixa
did, u = novo_doc()
caminho = tasks._minio_path(sigla, u)
with SessionLocal() as s:
    d = s.get(Documento, did)
    d.minio_path, d.hash_sha256, d.tamanho_bytes, d.downloaded_at = caminho, uuid.uuid4().hex * 2, 5, depois
    s.commit()
d, b = roda(did, u)
check(b, "forcado + tamanho diferente -> baixa de novo")

# 2. doc novo, arquivo antigo (sem sufixo) ja guardado com mesmo tamanho -> registra sem baixar
did, u = novo_doc()
antigo = tasks._minio_path_antigo(sigla, u)
guardado[antigo.split("/", 1)[1]] = len(corpo)
d, b = roda(did, u)
check(d.status_download == DownloadStatus.SUCCESS and d.minio_path == antigo and not b,
      f"fonte recadastrada: reaproveita {antigo.rsplit('/', 1)[-1]} sem baixar")

# 2b. guardado com tamanho diferente -> baixa
guardado[antigo.split("/", 1)[1]] = 3
did, u = novo_doc()
d, b = roda(did, u)
check(b, "arquivo guardado com tamanho diferente -> baixa")

# 2c. nada guardado -> nem consulta HEAD, baixa direto
chamou_head = []
orig = tasks._cabecalho_remoto
tasks._cabecalho_remoto = lambda u: chamou_head.append(1) or orig(u)
guardado.clear()
did, u = novo_doc()
d, b = roda(did, u)
check(b and not chamou_head, "doc novo sem nada guardado: baixa sem requisicao extra")
tasks._cabecalho_remoto = orig

# 3. dados abertos: sem 'recoletar', refaz so o que falhou
enfileirados = []
tasks.fetch_dataset_mes.apply_async = lambda args: enfileirados.append(args[0]["orgao"])
transparencia.DATASETS["teste3"] = transparencia.DatasetSpec(
    "teste3", "snapshot", lambda: [], lambda a, m: [], lambda r, s: iter([]))
with SessionLocal() as s:
    tsec = tasks.get_or_create_transparencia(s)
    for org, st in (("OK", DownloadStatus.SUCCESS), ("FALHOU", DownloadStatus.FAILED), ("VAZIO", DownloadStatus.SEM_DADOS)):
        s.add(Documento(secretaria_id=tsec.id, job_id=job_id, status_download=st,
                        url_origem=transparencia.synthetic_url("teste3", {}, org)))
    j = ScrapingJob(secretaria_id=tsec.id, status=JobStatus.RUNNING, tipo="teste3"); s.add(j); s.flush()
    s.add(JobPending(job_id=j.id, pending=1)); s.commit(); j_id, t_id = j.id, tsec.id
pl = {"job_id": str(j_id), "secretaria_id": str(t_id), "dataset": "teste3", "ano_ini": 2019, "mes_ini": 1,
      "ano_fim": 2025, "mes_fim": 12, "orgaos": ["OK", "FALHOU", "VAZIO", "NOVO"], "force": False}
tasks.discover_dataset.run(pl)
check(sorted(enfileirados) == ["FALHOU", "NOVO"], f"sem recoletar: refaz falha + novo, pula ok/sem dados -> {sorted(enfileirados)}")
enfileirados.clear()
with SessionLocal() as s:
    j = ScrapingJob(secretaria_id=t_id, status=JobStatus.RUNNING, tipo="teste3"); s.add(j); s.flush()
    s.add(JobPending(job_id=j.id, pending=1)); s.commit(); pl["job_id"] = str(j.id)
tasks.discover_dataset.run({**pl, "force": True})
check(sorted(enfileirados) == ["FALHOU", "NOVO", "OK", "VAZIO"], f"com recoletar: refaz tudo -> {sorted(enfileirados)}")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
