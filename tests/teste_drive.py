"""Envio ao Google Drive: link -> pasta, conexao, andamento e erro (com um rclone falso)."""
import json
import os
import stat
import tempfile
import uuid

from app import config, routers, storage, tasks
from app.database import SessionLocal, init_db
from app.models import JobStatus, ScrapingJob

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


init_db()

# ---------- link -> id da pasta ----------
ID = "1MfMx7GMbiHaSGlv6u-00kHcjGspAy22A"
for link in (f"https://drive.google.com/drive/folders/{ID}",
             f"https://drive.google.com/drive/u/0/folders/{ID}?usp=sharing",
             f"https://drive.google.com/open?id={ID}", ID):
    check(tasks.pasta_drive_do_link(link) == ID, f"link reconhecido: {link[:60]}")
check(tasks.pasta_drive_do_link("https://exemplo.com/nada") is None, "link que nao e do Drive e recusado")

# ---------- rclone falso: imita as estatisticas em JSON do rclone de verdade ----------
pasta = tempfile.mkdtemp()
falso = os.path.join(pasta, "rclone")
with open(falso, "w") as f:
    f.write("""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["RCLONE_ARGS_OUT"], "w").write(json.dumps(args))
if os.environ.get("RCLONE_FALHAR"):
    print(json.dumps({"level": "error", "msg": "couldn't fetch token: invalid_grant"}), file=sys.stderr)
    sys.exit(1)
for t, tot in ((0, 0), (3, 10), (10, 10)):
    print(json.dumps({"level": "notice", "msg": "stats", "stats": {
        "transfers": t, "totalTransfers": tot, "checks": 5, "bytes": t * 1000,
        "totalBytes": 10000, "errors": 0}}), file=sys.stderr)
""")
os.chmod(falso, os.stat(falso).st_mode | stat.S_IEXEC)
config.RCLONE_BIN = falso
os.environ["RCLONE_ARGS_OUT"] = os.path.join(pasta, "args.json")
tasks.enviar_drive.apply_async = lambda args: None  # a task roda abaixo, de forma direta

storage.salvar_bytes(b"%PDF-1 x", "SESPA/folha/2025-01/a.csv")
storage.salvar_bytes(b"segredo", ".drive/teste.txt")  # oculto: nao pode ir pro Drive

# ---------- sem conta conectada: recusa ----------
with SessionLocal() as s:
    try:
        routers.drive_enviar(routers.DriveEnvio(link=ID), s); ok = False
    except routers.HTTPException as e:
        ok = e.status_code == 400
check(ok, "sem conta conectada, o envio e recusado com explicacao")

# ---------- conectar ----------
try:
    routers.drive_conectar(routers.DriveToken(token="lixo")); ok = False
except routers.HTTPException:
    ok = True
check(ok, "codigo invalido e recusado")
token = 'Paste the following into your remote machine --->\n{"access_token":"a","token_type":"Bearer","refresh_token":"r","expiry":"2030-01-01T00:00:00Z"}\n<---End paste'
routers.drive_conectar(routers.DriveToken(token=token))
modo = oct(os.stat(config.RCLONE_CONFIG).st_mode & 0o777)
check(tasks.drive_conectado() and modo == "0o600", f"conectado: config gravada com permissao restrita ({modo})")
estado = routers.drive_estado(SessionLocal())
check(estado["conectado"] and "SESPA" in estado["pastas"] and ".drive" not in estado["pastas"],
      f"estado: conectado, pastas oferecidas {estado['pastas']} (sem as ocultas)")

# ---------- enviar: andamento e conclusao ----------
with SessionLocal() as s:
    r = routers.drive_enviar(routers.DriveEnvio(link=f"https://drive.google.com/drive/folders/{ID}"), s)
job_id = uuid.UUID(r["job_id"])
with SessionLocal() as s:
    try:
        routers.drive_enviar(routers.DriveEnvio(link=ID), s); ok = False
    except routers.HTTPException as e:
        ok = e.status_code == 409
check(ok, "um envio por vez (segundo pedido recusado enquanto o primeiro roda)")
tasks.enviar_drive.run({"job_id": str(job_id), "pasta_id": ID, "subpasta": ""})
args = json.load(open(os.environ["RCLONE_ARGS_OUT"]))
check("--drive-root-folder-id" in args and args[args.index("--drive-root-folder-id") + 1] == ID,
      "rclone recebe a pasta do link")
check(".*" in args and "_manutencao/**" in args, "arquivos ocultos (autorizacao) e de manutencao ficam de fora")
with SessionLocal() as s:
    job = s.get(ScrapingJob, job_id)
    prog = json.loads(job.progresso_json)
check(job.status == JobStatus.COMPLETED and prog["fase"] == "concluido" and prog["feitos"] == 10,
      f"envio concluido: {prog['feitos']} enviados, fase {prog['fase']}")
with SessionLocal() as s:
    t = [x for x in routers.progresso_tarefas(s)["tarefas"] if x["job_id"] == str(job_id)][0]
check(t["fonte"] == "Envio para o Google Drive" and "10 arquivo(s) novo(s) enviado(s)" in t["texto"],
      f"painel mostra o resultado: {t['texto']}")

# ---------- erro: autorizacao expirada ----------
os.environ["RCLONE_FALHAR"] = "1"
with SessionLocal() as s:
    r = routers.drive_enviar(routers.DriveEnvio(link=ID, subpasta="SESPA"), s)
tasks.enviar_drive.run({"job_id": r["job_id"], "pasta_id": ID, "subpasta": "SESPA"})
with SessionLocal() as s:
    job = s.get(ScrapingJob, uuid.UUID(r["job_id"]))
    prog = json.loads(job.progresso_json)
check(job.status == JobStatus.FAILED and "conecte a conta de novo" in prog["erro"],
      f"autorizacao expirada vira falha com explicacao: {prog['erro']}")
del os.environ["RCLONE_FALHAR"]
with SessionLocal() as s:
    try:
        routers.drive_enviar(routers.DriveEnvio(link=ID, subpasta="../etc"), s); ok = False
    except routers.HTTPException as e:
        ok = e.status_code == 400
check(ok, "subpasta fora da pasta de arquivos e recusada")

routers.drive_desconectar()
check(not tasks.drive_conectado(), "desconectar apaga a autorizacao")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
