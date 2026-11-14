import logging
import time

from minio import Minio
from minio.error import S3Error

from app import config

log = logging.getLogger(__name__)


def client() -> Minio:
    return Minio(
        config.MINIO_ENDPOINT,
        access_key=config.MINIO_ACCESS_KEY,
        secret_key=config.MINIO_SECRET_KEY,
        secure=config.MINIO_SECURE,
    )


def ensure_bucket(attempts: int = 30, delay: float = 2.0) -> Minio:
    c = client()
    for i in range(attempts):
        try:
            if not c.bucket_exists(config.MINIO_BUCKET):
                c.make_bucket(config.MINIO_BUCKET)
                log.info("Bucket %s criado", config.MINIO_BUCKET)
            return c
        except S3Error as exc:
            if i == attempts - 1:
                raise
            log.warning("MinIO indisponivel (%s), tentativa %d/%d", exc, i + 1, attempts)
            time.sleep(delay)
    return c
