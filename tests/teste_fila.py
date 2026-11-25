"""Fila automatica: uma por vez, avanca sozinha, falha nao trava."""
from sqlalchemy import update

from app import routers, tasks
from app.database import SessionLocal, init_db
from app.models import ColetaFila, JobStatus, ScrapingJob
from app.schemas import FolhaStartRequest

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


init_db()
tasks.discover_dataset.apply_async = lambda args: None  # sem broker: so o job e criado
routers.avancar_fila.apply_async = lambda: None


def fila():
    with SessionLocal() as s:
        return [(i.dataset, i.status) for i in s.query(ColetaFila).order_by(ColetaFila.id)]


def termina_atual(status):
    with SessionLocal() as s:
        atual = s.query(ColetaFila).filter_by(status="RODANDO").one()
        s.execute(update(ScrapingJob).where(ScrapingJob.id == atual.job_id).values(status=status))
        s.commit()


with SessionLocal() as s:
    r = routers.enfileirar(FolhaStartRequest(dataset="*", ano_ini=2019, mes_ini=1, ano_fim=2025, mes_fim=12), s)
    routers.enfileirar(FolhaStartRequest(dataset="folha", ano_ini=2019, mes_ini=1, ano_fim=2025, mes_fim=12,
                                         orgaos=["SESPA"]), s)
n = r["adicionados"]
check(n == 14, f"'todos os datasets' vira {n} itens na fila (+1 avulso)")

tasks.avancar_fila()
f = fila()
check([x[1] for x in f].count("RODANDO") == 1 and f[0][1] == "RODANDO", "1a chamada: so o 1o item roda")
tasks.avancar_fila()
check([x[1] for x in fila()].count("RODANDO") == 1, "enquanto o job roda, nao inicia outro")

termina_atual(JobStatus.COMPLETED)
tasks.avancar_fila()
f = fila()
check(f[0][1] == "CONCLUIDA" and f[1][1] == "RODANDO", "job concluido -> item CONCLUIDA e o proximo comeca")

termina_atual(JobStatus.FAILED)
tasks.avancar_fila()
f = fila()
check(f[1][1] == "FALHOU" and f[2][1] == "RODANDO", "job com falha nao trava a fila")

termina_atual(JobStatus.CANCELLED)
tasks.avancar_fila()
check(fila()[2][1] == "CANCELADA", "tarefa cancelada no painel -> item CANCELADA, fila segue")

# erro ao iniciar (dataset que sumiu do registro) -> marca e pula
with SessionLocal() as s:
    s.query(ColetaFila).filter_by(status="AGUARDANDO").order_by(ColetaFila.id).first().dataset = "nao_existe"
    s.commit()
termina_atual(JobStatus.COMPLETED)
tasks.avancar_fila()
f = fila()
check(f[4][1] == "FALHOU" and f[5][1] == "RODANDO", "item que nem inicia vira FALHOU e o seguinte comeca")

# remover: rodando -> 409; aguardando -> sai; limpar -> tira terminadas
with SessionLocal() as s:
    rod = s.query(ColetaFila).filter_by(status="RODANDO").one().id
    agu = s.query(ColetaFila).filter_by(status="AGUARDANDO").order_by(ColetaFila.id).first().id
    try:
        routers.remover_da_fila(rod, s); ok = False
    except routers.HTTPException as e:
        ok = e.status_code == 409
    check(ok, "nao deixa remover o que esta rodando")
    routers.remover_da_fila(agu, s)
    lim = routers.limpar_fila(s)
    lista = routers.listar_fila(s)
check(all(i["status"] in ("RODANDO", "AGUARDANDO") for i in lista) and lim["removidos"] == 5,
      f"remover/limpar ok ({lim['removidos']} terminadas removidas, {len(lista)} restantes)")
check(lista[-1]["orgaos"] == "SESPA" and "2019-01 a 2025-12" in lista[-1]["periodo"], "listagem mostra periodo/orgaos")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
