"""Armazenamento so na pasta (sem MinIO): gravar, ler, listar, mover, seguranca."""
import io

from app import storage

falhas = 0


def check(cond, msg):
    global falhas
    print(("OK   " if cond else "FALHA"), msg)
    falhas += 0 if cond else 1


check(storage.pronto(), f"pasta pronta: {storage.raiz()}")
storage.salvar(io.BytesIO(b"%PDF-1 abc"), "SESPA/folha/2025-01/a.csv")
check(storage.tamanho("SESPA/folha/2025-01/a.csv") == 10, "salvar + tamanho")
check(storage.chave("raw-docs/SESPA/x.pdf") == "SESPA/x.pdf", "chave() tira o prefixo do banco")
storage.mover("SESPA/folha/2025-01/a.csv", "SESPA/folha/2025-01/b.csv")
check(not storage.existe("SESPA/folha/2025-01/a.csv") and storage.existe("SESPA/folha/2025-01/b.csv"), "mover")
with storage.abrir("SESPA/folha/2025-01/b.csv") as f:
    check(f.read() == b"%PDF-1 abc", "abrir le o conteudo")
check(list(storage.listar("SESPA")) == ["SESPA/folha/2025-01/b.csv"], "listar (sem temporarios)")
try:
    storage.caminho("../../etc/passwd"); ok = False
except ValueError:
    ok = True
check(ok, "recusa caminho fora da pasta (../)")
storage.remover("SESPA/folha/2025-01/b.csv")
check(not storage.existe("SESPA/folha/2025-01/b.csv"), "remover")


class _Quebra(io.BytesIO):
    def read(self, *a):
        raise OSError("disco cheio (simulado)")


try:
    storage.salvar(_Quebra(b"x"), "SESPA/parcial.pdf")
except OSError:
    pass
check(not storage.existe("SESPA/parcial.pdf") and not any("parcial" in k for k in storage.listar()),
      "falha no meio da gravacao nao deixa arquivo pela metade")

print("\nRESULTADO:", "tudo OK" if falhas == 0 else f"{falhas} falha(s)")
raise SystemExit(1 if falhas else 0)
