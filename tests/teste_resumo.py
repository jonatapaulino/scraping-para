"""/resumo (cards do topo): situacoes que o usuario precisa entender."""
import uuid

from app import routers, storage, tasks
from app.database import SessionLocal, init_db
from app.models import ColetaFila, Documento, DownloadStatus, JobPending, JobStatus, ScrapingJob

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


init_db()
storage.salvar_bytes(b"%PDF-" + b"x" * 995, "SESPA/folha/2025-01/a.csv")
storage.salvar_bytes(b"y" * 3000, "IOEPA/diario_oficial/documentos/2025/x.pdf")


def resumo():
    routers._CACHE_CURTO.clear()
    with SessionLocal() as s:
        return routers.resumo_painel(s)


r = resumo()
check(r["agora"] == [] and r["fila"]["total"] == 0, "sem nada: nenhuma coleta e fila vazia")
check(r["guardado"]["arquivos"] == 2 and r["guardado"]["bytes"] == 4000 and r["guardado"]["livre_bytes"] > 0,
      f"arquivos guardados contados: {r['guardado']}")

with SessionLocal() as s:
    sec = tasks.get_or_create_transparencia(s)
    job = ScrapingJob(secretaria_id=sec.id, status=JobStatus.RUNNING, tipo="transf_recursos", total_pdfs_encontrados=10)
    s.add(job); s.flush(); s.add(JobPending(job_id=job.id, pending=10))
    for i, st in enumerate([DownloadStatus.SUCCESS] * 3 + [DownloadStatus.SEM_DADOS] + [DownloadStatus.FAILED] + [DownloadStatus.PENDING] * 5):
        s.add(Documento(secretaria_id=sec.id, job_id=job.id, url_origem=f"x://{uuid.uuid4()}", status_download=st))
    for ds, st in (("folha", "CONCLUIDA"), ("despesas", "CONCLUIDA"), ("transf_recursos", "RODANDO"),
                   ("receitas", "AGUARDANDO"), ("obras", "AGUARDANDO")):
        s.add(ColetaFila(dataset=ds, ano_ini=2019, mes_ini=1, ano_fim=2025, mes_fim=12, status=st,
                         job_id=job.id if st == "RODANDO" else None))
    s.commit(); job_id = job.id

r = resumo()
a = r["agora"][0]
check(a["nome"] == "Transferência de recursos" and a["feitos"] == 5 and a["total"] == 10 and a["percentual"] == 50.0,
      f"rodando com partes: {a['nome']} {a['feitos']}/{a['total']} = {a['percentual']}% (sem dados e falha contam como feitas)")
f = r["fila"]
check(f["posicao_atual"] == 3 and f["total"] == 5 and f["terminadas"] == 2 and f["aguardando"] == 2,
      f"fila: {f['posicao_atual']}ª de {f['total']}, {f['terminadas']} concluidas, {f['aguardando']} esperando")
check(r["falhas"]["total"] == 1 and r["sem_dados"] == 1, "falha e sem dados separados")

with SessionLocal() as s:  # diarias: uma parte unica grande
    j = s.get(ScrapingJob, job_id); j.tipo = "diarias"; j.total_pdfs_encontrados = 1
    s.query(Documento).filter(Documento.job_id == job_id).delete()
    s.add(Documento(secretaria_id=j.secretaria_id, job_id=job_id, url_origem=f"x://{uuid.uuid4()}",
                    status_download=DownloadStatus.PENDING))
    s.commit()
r = resumo()
check(r["agora"][0]["nome"] == "Diárias" and r["agora"][0]["total"] == 1, "parte unica grande (diarias) identificada")

with SessionLocal() as s:  # entre uma coleta e outra
    s.get(ScrapingJob, job_id).status = JobStatus.COMPLETED
    s.query(ColetaFila).filter(ColetaFila.status == "RODANDO").update({"status": "CONCLUIDA"})
    s.commit()
r = resumo()
check(r["agora"] == [] and r["fila"]["aguardando"] == 2 and r["fila"]["posicao_atual"] is None,
      "entre coletas: nada rodando mas fila com itens esperando")

with SessionLocal() as s:  # tudo terminado
    s.query(ColetaFila).update({"status": "CONCLUIDA"}); s.commit()
r = resumo()
check(r["agora"] == [] and r["fila"]["aguardando"] == 0 and r["fila"]["terminadas"] == 5, "tudo concluido")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
