"""Migracao unica: MinIO -> pasta de downloads (fim da copia dupla).

Ate esta versao cada arquivo ficava no MinIO E em downloads/. Agora so a
pasta. Antes de desligar o MinIO, este script garante que NADA se perde:

  - objeto do MinIO que nao esta na pasta     -> copiado para a pasta
  - objeto na pasta com o mesmo tamanho        -> ok (ja era a copia)
  - objeto na pasta com tamanho DIFERENTE      -> a versao do MinIO e salva ao
    lado como <nome>.minio<ext> e entra no relatorio (nada e sobrescrito)

So SIMULA sem --aplicar. No fim diz se e seguro apagar o volume do MinIO.

Uso (MinIO ainda no ar, pasta montada em /downloads):
  python -m app.migrar_minio            # simula
  python -m app.migrar_minio --aplicar  # copia o que falta
"""
import argparse
import os
import sys

from app import config, storage


def _cliente():
    from minio import Minio

    return Minio(
        os.getenv("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )


def _baixa(mc, key: str, destino_key: str) -> None:
    import tempfile

    obj = mc.get_object(config.MINIO_BUCKET, key)
    try:
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as tmp:
            for chunk in obj.stream(1024 * 1024):
                tmp.write(chunk)
            storage.salvar(tmp, destino_key)
    finally:
        obj.close()
        obj.release_conn()


def _tar_para_stdout(args_destino: str | None = None, limite: int = 0) -> int:
    """--tar: em vez de copiar para a pasta local, escreve na saida padrao um
    .tar com os objetos que FALTAM na pasta (e a versao do MinIO dos que tem
    tamanho diferente, como <nome>.minio<ext>). Serve para levar direto a outro
    computador sem ocupar o disco daqui:
      python -m app.migrar_minio --tar | ssh servidor "tar -xf - -C destino"
    """
    import tarfile

    # --ja-no-destino: lista "<tamanho> <caminho>" do que ja chegou ao outro
    # computador — permite retomar uma transferencia interrompida
    ja: dict[str, int] = {}
    if args_destino:
        with open(args_destino, encoding="utf-8", errors="surrogateescape") as f:
            for linha in f:
                tam, _, cam = linha.rstrip("\n").partition(" ")
                if tam.isdigit():
                    ja[cam] = int(tam)

    mc = _cliente()
    n = total = 0
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
        for obj in mc.list_objects(config.MINIO_BUCKET, recursive=True):
            key = obj.object_name
            local = storage.tamanho(key)
            if local == obj.size:
                continue
            nome = key
            if local is not None:
                base, ext = os.path.splitext(key)
                nome = f"{base}.minio{ext}"
            if ja.get(nome) == obj.size:
                continue  # ja esta no destino
            if limite and total >= limite:
                break  # lote cheio: o resto vai na proxima rodada
            info = tarfile.TarInfo(nome)
            info.size = obj.size
            info.mtime = int(obj.last_modified.timestamp()) if obj.last_modified else 0
            resp = mc.get_object(config.MINIO_BUCKET, key)
            try:
                tar.addfile(info, resp)
            finally:
                resp.close()
                resp.release_conn()
            n += 1
            total += obj.size
            if n % 200 == 0:
                print(f"  ... {n} arquivos, {total / 1e9:.1f} GB", file=sys.stderr, flush=True)
    print(f"tar: {n} arquivos, {total / 1e9:.2f} GB", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--tar", action="store_true", help="manda o que falta como .tar na saida padrao")
    ap.add_argument("--ja-no-destino", help="arquivo '<tamanho> <caminho>' do que ja esta no destino")
    ap.add_argument("--lote-mb", type=int, default=0, help="para depois de ~N MB (envio em lotes)")
    args = ap.parse_args()
    if args.tar:
        return _tar_para_stdout(args.ja_no_destino, args.lote_mb * 1024 * 1024)
    aplicar = args.aplicar
    if not storage.pronto():
        print(f"ERRO: pasta de arquivos {storage.raiz()} indisponivel ou sem escrita")
        return 2

    mc = _cliente()
    total = iguais = copiados = divergentes = 0
    bytes_copiar = 0
    for obj in mc.list_objects(config.MINIO_BUCKET, recursive=True):
        total += 1
        key = obj.object_name
        local = storage.tamanho(key)
        if local == obj.size:
            iguais += 1
            continue
        if local is None:
            copiados += 1
            bytes_copiar += obj.size
            print(f"  [falta na pasta -> copiar] {key} ({obj.size} bytes)")
            if aplicar:
                _baixa(mc, key, key)
        else:
            divergentes += 1
            base, ext = os.path.splitext(key)
            print(f"  [tamanho diferente: pasta {local} x MinIO {obj.size}] {key} "
                  f"-> versao do MinIO salva como {base}.minio{ext}")
            if aplicar:
                _baixa(mc, key, f"{base}.minio{ext}")

    print(f"\n{'APLICADO' if aplicar else 'SIMULACAO'}: {total} objetos no MinIO — "
          f"{iguais} ja estavam na pasta, {copiados} faltavam ({bytes_copiar / 1e6:.1f} MB), "
          f"{divergentes} com tamanho diferente.")
    if aplicar:
        # confere de novo: tudo do MinIO tem que existir na pasta
        faltando = [
            o.object_name for o in mc.list_objects(config.MINIO_BUCKET, recursive=True)
            if storage.tamanho(o.object_name) is None
        ]
        if faltando:
            print(f"ATENCAO: {len(faltando)} objetos ainda nao estao na pasta — NAO apague o MinIO.")
            return 1
        print("OK: todo objeto do MinIO existe na pasta. E seguro apagar o volume do MinIO.")
    else:
        print("Nada foi alterado. Para copiar: python -m app.migrar_minio --aplicar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
