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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    aplicar = ap.parse_args().aplicar
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
