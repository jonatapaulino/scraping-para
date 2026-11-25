"""Falhas pelo painel: motivo gravado, lista agrupada, refazer (tarefa visivel) e descartar."""
import uuid

from app import routers, tasks
from app.database import SessionLocal, init_db
from app.models import Documento, DownloadStatus, JobPending, JobStatus, ScrapingJob, Secretaria

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


init_db()

# ---------- leitura da chave do documento de dados abertos ----------
casos = {
    "transparencia://folha/2025/03/SESPA": ("folha", {"ano": 2025, "mes": 3}, "SESPA"),
    "transparencia://receitas/2025/01/GAB/VICE": ("receitas", {"ano": 2025, "mes": 1}, "GAB/VICE"),
    "transparencia://obras/2024/SEOP": ("obras", {"ano": 2024}, "SEOP"),
    "transparencia://diarias/2019-01_2025-12": ("diarias", {"ano_ini": 2019, "mes_ini": 1, "ano_fim": 2025, "mes_fim": 12}, None),
    "transparencia://bens_imoveis/snapshot/SEFA": ("bens_imoveis", {}, "SEFA"),
    "transparencia://despesas/2025/01?orgaos=SESPA": None,   # formato antigo
    "transparencia://nao_existe/2025": None,
}
for url, esperado in casos.items():
    check(tasks.recorte_de_url(url) == esperado, f"recorte_de_url({url.split('://')[1]}) = {tasks.recorte_de_url(url)}")

# ---------- cenario: falhas de varios tipos ----------
enviados = []
tasks.celery.send_task = lambda nome, args: enviados.append((nome, args[0]))
tasks.requeue_dead_letters = lambda **kw: 0
routers.requeue_dead_letters = lambda **kw: 0

with SessionLocal() as s:
    portal = Secretaria(sigla="SEDUC", url_base="https://www.seduc.pa.gov.br")
    s.add(portal); s.flush()
    transp = tasks.get_or_create_transparencia(s)
    job = ScrapingJob(secretaria_id=portal.id, status=JobStatus.COMPLETED); s.add(job); s.flush()
    docs = [
        (portal.id, "https://www.seduc.pa.gov.br/a.pdf", "Link quebrado ou bloqueado (HTTP 404)"),
        (portal.id, "https://www.seduc.pa.gov.br/b.pdf", "Link quebrado ou bloqueado (HTTP 404)"),
        (portal.id, "https://www.seduc.pa.gov.br/c.pdf", "Não é PDF (o link abre outro tipo de arquivo ou uma página)"),
        (portal.id, "https://www.seduc.pa.gov.br/d.pdf", "Falhou depois de várias tentativas: o site não respondeu a tempo"),
        (transp.id, "transparencia://pncp_contratos/2025/SESPA", "Falhou depois de várias tentativas: o site respondeu HTTP 500"),
        (transp.id, "transparencia://folha/2024/07/SEFA", None),   # falha antiga, sem motivo
    ]
    ids = {}
    for sec_id, url, erro in docs:
        d = Documento(secretaria_id=sec_id, job_id=job.id, url_origem=url,
                      status_download=DownloadStatus.FAILED, erro=erro)
        s.add(d); s.flush(); ids[url] = d.id
    s.commit()

with SessionLocal() as s:
    lista = routers.listar_falhas(s)
check(lista["total"] == 6 and lista["permanentes"] == 3 and lista["refaziveis"] == 3,
      f"lista: {lista['total']} falhas, {lista['permanentes']} permanentes, {lista['refaziveis']} refaziveis")
g404 = next(g for g in lista["grupos"] if "404" in g["motivo"])
check(g404["quantidade"] == 2 and g404["permanente"] and g404["fonte_nome"] == "SEDUC",
      "links quebrados agrupados (2) e marcados como permanentes")
check(any(g["fonte_nome"] == "Contratos PNCP" for g in lista["grupos"]), "dados abertos aparecem pelo nome do dataset")
check(any(g["motivo"] == "" and "Sem detalhe" in g["motivo_texto"] for g in lista["grupos"]),
      "falha antiga sem motivo vira 'Sem detalhe'")

# ---------- refazer: so o que vale tentar, numa tarefa nova ----------
with SessionLocal() as s:
    r = routers.refazer(routers.FalhasAcao(), s)
check(r["refeitos"] == 3, f"refazer sem filtro: 3 refeitos (links quebrados e nao-PDF ficam de fora) -> {r['refeitos']}")
nomes = sorted(n for n, _ in enviados)
check(nomes == ["app.tasks.download_document", "app.tasks.fetch_dataset_mes", "app.tasks.fetch_dataset_mes"],
      f"tarefas certas por tipo: {nomes}")
pncp = next(p for n, p in enviados if p.get("dataset") == "pncp_contratos")
check(pncp["recorte"] == {"ano": 2025} and pncp["orgao"] == "SESPA", "payload do dataset reconstruido da chave")
with SessionLocal() as s:
    novo = s.get(ScrapingJob, uuid.UUID(r["job_id"]))
    pend = s.get(JobPending, novo.id).pending
    d = s.get(Documento, ids["https://www.seduc.pa.gov.br/d.pdf"])
check(novo.tipo == "refazer" and novo.status == JobStatus.RUNNING and pend == 3 and novo.total_pdfs_encontrados == 3,
      "tarefa 'refazer' visivel, com 3 pendentes")
check(d.status_download == DownloadStatus.PENDING and d.erro is None and d.job_id == novo.id,
      "documento refeito volta a PENDING, sem motivo, na tarefa nova")
with SessionLocal() as s:
    res = routers.resumo_painel(s)
check(any(a["nome"] == "Nova tentativa das falhas" and a["total"] == 3 for a in res["agora"]),
      "'Coleta agora' mostra a nova tentativa")

# sucesso apaga o motivo
with SessionLocal() as s:
    tasks._fecha_documento(s, d.id, novo.id, status_download=DownloadStatus.SUCCESS)
with SessionLocal() as s:
    check(s.get(Documento, d.id).erro is None, "sucesso limpa o motivo")

# ---------- descartar ----------
with SessionLocal() as s:
    n = routers.descartar(routers.FalhasAcao(so_permanentes=True), s)["descartadas"]
check(n == 3, f"descartar links quebrados/nao-PDF: {n}")
with SessionLocal() as s:
    lista = routers.listar_falhas(s)
    st = s.get(Documento, ids["https://www.seduc.pa.gov.br/a.pdf"]).status_download
check(lista["total"] == 0 and st == DownloadStatus.DESCARTADO, "descartadas saem da lista e viram DESCARTADO")
with SessionLocal() as s:
    res = routers.resumo_painel(s)
check(res["falhas"]["total"] == 0, "descartadas nao contam no card Falhas")

# ---------- tarefa 'refazer' fechada por tentativa ANTIGA nao fica presa ----------
with SessionLocal() as s:
    s.query(Documento).filter(Documento.status_download == DownloadStatus.DESCARTADO).update(
        {"status_download": DownloadStatus.FAILED}, synchronize_session=False)
    s.commit()
    r2 = routers.refazer(routers.FalhasAcao(incluir_permanentes=True), s)
job2 = uuid.UUID(r2["job_id"])
with SessionLocal() as s:
    pend_docs = s.query(Documento).filter(Documento.job_id == job2,
                                          Documento.status_download == DownloadStatus.PENDING).all()
    antigo = job.id  # a tentativa velha carrega o job ANTIGO no payload
    for dd in pend_docs:
        tasks._fecha_documento(s, dd.id, antigo, status_download=DownloadStatus.SUCCESS)
with SessionLocal() as s:
    st2 = s.get(ScrapingJob, job2).status
check(r2["refeitos"] == 3 and st2 == JobStatus.COMPLETED,
      f"itens fechados por tentativa antiga: a tarefa nova conclui ({st2.value})")

# ---------- versao nova ----------
routers._CACHE_CURTO.clear()
routers._ultima_versao = lambda: {"ultima": "9.0.0", "url": "https://x/releases/v9.0.0", "publicada_em": None}
routers.config.APP_VERSION = "1.1.2"
v = routers.versao()
check(v["nova_disponivel"] and v["ultima"] == "9.0.0", "avisa versao nova (1.1.2 -> 9.0.0)")
routers._CACHE_CURTO.clear()
routers.config.APP_VERSION = "dev"
check(routers.versao()["nova_disponivel"] is False, "versao local (dev) nao mostra aviso")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
