"""Roda todas as suites de teste, cada uma ISOLADA: banco novo e pasta de
arquivos temporaria propria (uma suite nao suja a outra).

Precisa de um Postgres acessivel (no CI: servico do GitHub Actions). Variaveis:
  POSTGRES_HOST/PORT/USER/PASSWORD (padrao localhost:5432 scraper/scraper)

  python tests/rodar.py            # todas
  python tests/rodar.py fila       # so as que tem 'fila' no nome
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent


def _admin():
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "scraper"),
        password=os.getenv("POSTGRES_PASSWORD", "scraper"),
        dbname=os.getenv("POSTGRES_DB_ADMIN", "postgres"),
        autocommit=True,
    )


def main() -> int:
    filtro = sys.argv[1] if len(sys.argv) > 1 else ""
    suites = sorted(p for p in AQUI.glob("teste_*.py") if filtro in p.stem)
    if not suites:
        print("Nenhuma suite encontrada")
        return 1
    falhas = []
    for suite in suites:
        banco = f"t_{suite.stem}_{os.getpid()}"
        with _admin() as c:
            c.execute(f'DROP DATABASE IF EXISTS "{banco}"')
            c.execute(f'CREATE DATABASE "{banco}"')
        with tempfile.TemporaryDirectory(prefix="arquivos-") as pasta:
            env = {
                **os.environ,
                "POSTGRES_DB": banco,
                "ARQUIVOS_DIR": pasta,
                "PYTHONPATH": str(RAIZ),
                "PYTHONDONTWRITEBYTECODE": "1",
                # sem broker de proposito: os testes cobrem o caminho "RabbitMQ fora do ar"
                "RABBITMQ_HOST": "rabbit-inexistente.invalid",
                # APIs do governo fora do alcance: testes deterministicos, sem rede
                "DESPESAS_API_BASE": "http://127.0.0.1:9/indisponivel",
                "EMPENHOS_API_BASE": "http://127.0.0.1:9/indisponivel",
                "DOWNLOAD_DELAY_SECONDS": "0",
                "DISCOVERY_DELAY_SECONDS": "0",
            }
            inicio = time.monotonic()
            r = subprocess.run(
                [sys.executable, str(suite)], cwd=RAIZ, env=env,
                capture_output=True, text=True, timeout=600,
            )
            dur = time.monotonic() - inicio
        with _admin() as c:
            c.execute(f'DROP DATABASE IF EXISTS "{banco}" WITH (FORCE)')
        linhas = [ln for ln in r.stdout.splitlines() if ln.startswith(("OK", "FALHA"))]
        ok = r.returncode == 0
        print(f"{'✔' if ok else '✘'} {suite.stem:<20} {len(linhas):>3} verificações  {dur:5.1f}s")
        if not ok:
            falhas.append(suite.stem)
            for ln in r.stdout.splitlines():
                if ln.startswith("FALHA"):
                    print(f"    {ln}")
            erro = "\n".join(ln for ln in r.stderr.splitlines() if not ln.startswith('{"ts"'))
            if erro.strip():
                print("    --- erro ---\n    " + erro.strip()[-3000:].replace("\n", "\n    "))
    print(f"\n{len(suites) - len(falhas)}/{len(suites)} suites OK")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
