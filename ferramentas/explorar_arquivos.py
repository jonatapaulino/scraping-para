#!/usr/bin/env python3
"""Explorador dos arquivos baixados (SO LEITURA), para abrir no navegador.

Mostra as pastas com tamanho e quantidade de arquivos, o espaco livre do disco
e permite abrir/baixar cada arquivo. Nao apaga nem altera nada.

Uso no servidor (o atalho abrir-painel-servidor.bat ja faz isso):
    python3 explorar_arquivos.py ~/scraper-downloads 8001
e abra http://localhost:8001 pelo tunel SSH. Escuta so em 127.0.0.1.
So a biblioteca padrao do Python — nada para instalar.
"""
import html
import mimetypes
import os
import shutil
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.realpath(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/scraper-downloads"))
PORTA = int(sys.argv[2]) if len(sys.argv) > 2 else 8001
_cache: dict[str, tuple[float, tuple[int, int]]] = {}


def tam_legivel(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def resumo(pasta: str) -> tuple[int, int]:
    """(bytes, arquivos) de tudo dentro da pasta — guardado por 60 s."""
    agora = time.monotonic()
    hit = _cache.get(pasta)
    if hit and agora - hit[0] < 60:
        return hit[1]
    total = n = 0
    for dirpath, dirs, files in os.walk(pasta):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
                n += 1
            except OSError:
                pass
    _cache[pasta] = (agora, (total, n))
    return total, n


def dentro_da_raiz(rel: str) -> str | None:
    p = os.path.realpath(os.path.join(RAIZ, rel))
    return p if p == RAIZ or p.startswith(RAIZ + os.sep) else None


CSS = """
:root{--bg:#fff;--fg:#1f2328;--mut:#656d76;--lin:#d0d7de;--bar:#2f81f7;--card:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8d96a0;--lin:#30363d;--bar:#4493f8;--card:#161b22}}
body{margin:0;padding:16px;font:14px/1.45 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
h1{font-size:18px;margin:0 0 4px} .mut{color:var(--mut)} a{color:var(--bar);text-decoration:none} a:hover{text-decoration:underline}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.card{background:var(--card);border:1px solid var(--lin);border-radius:8px;padding:10px 14px}
.card b{display:block;font-size:18px}
table{border-collapse:collapse;width:100%;max-width:1100px}td,th{padding:6px 8px;border-bottom:1px solid var(--lin);text-align:left}
th{color:var(--mut);font-weight:600}td.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--lin);border-radius:3px;min-width:80px}.bar i{display:block;height:6px;background:var(--bar);border-radius:3px}
.trilha{margin:8px 0 12px}
"""


class Explorador(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencioso
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        rel = urllib.parse.unquote(url.path).lstrip("/")
        alvo = dentro_da_raiz(rel)
        if alvo is None or not os.path.exists(alvo):
            return self._texto(404, "Não encontrado")
        if os.path.isfile(alvo):
            return self._arquivo(alvo, "baixar" in urllib.parse.parse_qs(url.query))
        ordem = (urllib.parse.parse_qs(url.query).get("ordem") or ["data"])[0]
        return self._pasta(alvo, rel, ordem if ordem in ("data", "nome", "tamanho") else "data")

    def _texto(self, cod, msg):
        corpo = msg.encode()
        self.send_response(cod)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _arquivo(self, caminho, baixar):
        tipo = mimetypes.guess_type(caminho)[0] or "application/octet-stream"
        nome = os.path.basename(caminho)
        try:
            f = open(caminho, "rb")  # abre ANTES de responder: sem permissao vira aviso, nao arquivo vazio
        except PermissionError:
            return self._texto(403, "Sem permissão para ler este arquivo. Atualize o sistema: "
                                    "as permissões dos arquivos são corrigidas sozinhas ao reiniciar.")
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(os.path.getsize(caminho)))
        disp = "attachment" if baixar else "inline"
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{urllib.parse.quote(nome)}")
        self.end_headers()
        with f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def _pasta(self, pasta, rel, ordem="data"):
        itens = []
        for e in os.scandir(pasta):
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                b, n = resumo(e.path)
                # data da pasta = quando recebeu o ultimo arquivo
                itens.append((True, e.name, b, n, e.stat().st_mtime))
            elif e.is_file(follow_symlinks=False):
                st = e.stat()
                itens.append((False, e.name, st.st_size, 1, st.st_mtime))
        chave = {
            "data": lambda x: (not x[0], -(x[4] or 0), x[1].lower()),   # mais recentes primeiro
            "nome": lambda x: (not x[0], x[1].lower()),
            "tamanho": lambda x: (not x[0], -x[2], x[1].lower()),
        }[ordem]
        itens.sort(key=chave)
        total, narq = resumo(pasta)
        livre = shutil.disk_usage(pasta).free
        maior = max((i[2] for i in itens), default=1) or 1

        partes = [p for p in rel.split("/") if p]
        trilha = ['<a href="/">downloads</a>'] + [
            f'<a href="/{urllib.parse.quote("/".join(partes[:i + 1]))}/">{html.escape(p)}</a>'
            for i, p in enumerate(partes)
        ]
        linhas = []
        for eh_pasta, nome, b, n, mtime in itens:
            href = "/" + urllib.parse.quote("/".join(partes + [nome])) + ("/" if eh_pasta else "")
            if eh_pasta and ordem != "data":
                href += f"?ordem={ordem}"
            icone = "📁" if eh_pasta else "📄"
            quando = time.strftime("%d/%m/%Y %H:%M", time.localtime(mtime)) if mtime else ""
            extra = (f"{n:,} arquivos".replace(",", ".") + (f" · último em {quando}" if quando else "")) if eh_pasta else quando
            baixar = "" if eh_pasta else f' · <a href="{href}?baixar=1">baixar</a>'
            linhas.append(
                f'<tr><td>{icone} <a href="{href}">{html.escape(nome)}</a>{baixar}</td>'
                f'<td class="n">{tam_legivel(b)}</td><td><div class="bar"><i style="width:{b * 100 / maior:.1f}%"></i></div></td>'
                f'<td class="n mut">{extra}</td></tr>'
            )
        corpo = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Arquivos do servidor</title>
<style>{CSS}</style></head><body>
<h1>Arquivos guardados no servidor</h1><div class="mut">Somente leitura — {html.escape(RAIZ)}</div>
<div class="cards"><div class="card"><span class="mut">Nesta pasta</span><b>{tam_legivel(total)}</b></div>
<div class="card"><span class="mut">Arquivos</span><b>{narq:,}</b></div>
<div class="card"><span class="mut">Espaço livre no disco</span><b>{tam_legivel(livre)}</b></div></div>
<div class="trilha">{' / '.join(trilha)}</div>
<div class="mut" style="margin:-6px 0 10px">ordenar por: {' · '.join(
    f'<b>{r}</b>' if o == ordem else f'<a href="?ordem={o}">{r}</a>'
    for o, r in (("data", "mais recentes"), ("nome", "nome"), ("tamanho", "tamanho")))}</div>
<table><tr><th>Nome</th><th style="text-align:right">Tamanho</th><th></th><th style="text-align:right">Detalhe</th></tr>
{''.join(linhas) or '<tr><td colspan="4" class="mut">Pasta vazia — nada baixado aqui ainda.</td></tr>'}
</table><p class="mut">Os totais são recalculados a cada minuto.</p></body></html>""".replace(f"{narq:,}", f"{narq:,}".replace(",", "."))
        dados = corpo.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def _encerra_anterior() -> None:
    """Se uma janela antiga deixou o explorador rodando, encerra-o (so se o
    processo do pidfile for mesmo este programa) para liberar a porta."""
    import signal

    pidfile = os.path.expanduser("~/.scraper-explorar.pid")
    try:
        antigo = int(open(pidfile).read().strip())
        cmd = open(f"/proc/{antigo}/cmdline", "rb").read()
        if antigo != os.getpid() and b"explorar" in cmd:
            os.kill(antigo, signal.SIGTERM)
            time.sleep(1)
    except (OSError, ValueError):
        pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))


if __name__ == "__main__":
    _encerra_anterior()
    os.makedirs(RAIZ, exist_ok=True)
    print(f"Explorador de {RAIZ} em http://127.0.0.1:{PORTA}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORTA), Explorador).serve_forever()
