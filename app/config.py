import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = _int("RABBITMQ_PORT", 5672)
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "scraper")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "scraper")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = _int("POSTGRES_PORT", 5432)
POSTGRES_DB = os.getenv("POSTGRES_DB", "scraper")
POSTGRES_USER = os.getenv("POSTGRES_USER", "scraper")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "scraper")

DATABASE_URL = (
    f"postgresql+psycopg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
CELERY_BROKER_URL = (
    f"amqp://{RABBITMQ_USER}:{RABBITMQ_PASS}"
    f"@{RABBITMQ_HOST}:{RABBITMQ_PORT}{RABBITMQ_VHOST}"
    if RABBITMQ_VHOST.startswith("/")
    else f"amqp://{RABBITMQ_USER}:{RABBITMQ_PASS}@{RABBITMQ_HOST}:{RABBITMQ_PORT}/{RABBITMQ_VHOST}"
)

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "raw-docs")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# Pasta local (bind mount no host) onde os PDFs tambem sao gravados; vazio = so MinIO
LOCAL_EXPORT_DIR = os.getenv("LOCAL_EXPORT_DIR", "")

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://browserless:3000")
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "scraper")
PROXY_POOL = [p.strip() for p in os.getenv("PROXY_POOL", "").split(",") if p.strip()]

MAX_RETRIES = _int("MAX_RETRIES", 5)
RETRY_BASE_SECONDS = _int("RETRY_BASE_SECONDS", 120)
DISCOVERY_DELAY_SECONDS = _int("DISCOVERY_DELAY_SECONDS", 2)
DOWNLOAD_DELAY_SECONDS = _int("DOWNLOAD_DELAY_SECONDS", 2)
DISCOVERY_MAX_DEPTH = _int("DISCOVERY_MAX_DEPTH", 2)
HTTP_TIMEOUT_SECONDS = _int("HTTP_TIMEOUT_SECONDS", 30)
RENDER_FALLBACK = os.getenv("RENDER_FALLBACK", "true").lower() == "true"

CELERY_BEAT_CRON = os.getenv("CELERY_BEAT_CRON", "0 6 * * *")
# Formato: SIGLA=https://url-base;SIGLA2=https://url2
SEED_SECRETARIAS = os.getenv("SEED_SECRETARIAS", "")

Q_DISCOVERY = "q.discovery"
Q_DOWNLOAD = "q.download"
Q_DEAD = "q.dead"
DLX_EXCHANGE = "scraper.dlx"
EXCHANGE = "scraper"
