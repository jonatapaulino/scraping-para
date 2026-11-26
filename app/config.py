import os

# Versao do sistema: vem da imagem publicada pelo CI (tag vX.Y.Z); 'dev' fora dela
APP_VERSION = os.getenv("APP_VERSION", "dev")
# repositorio de onde sai a versao publicada (aviso de "versao nova" no painel)
GITHUB_REPO = os.getenv("GITHUB_REPO", "jonatapaulino/scraping-para")


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

# Pasta (dentro do container) onde os arquivos baixados ficam — UNICA copia.
# No host ela e a DOWNLOADS_DIR do docker-compose (padrao ./downloads).
# LOCAL_EXPORT_DIR e o nome antigo da mesma configuracao (ainda aceito).
ARQUIVOS_DIR = os.getenv("ARQUIVOS_DIR") or os.getenv("LOCAL_EXPORT_DIR") or "/downloads"
LOCAL_EXPORT_DIR = ARQUIVOS_DIR  # compatibilidade com o codigo/.env antigos
# Envio ao Google Drive (rclone dentro da imagem). A configuracao com a
# autorizacao da conta fica escondida na pasta de arquivos (nao aparece no painel)
RCLONE_BIN = os.getenv("RCLONE_BIN", "rclone")
RCLONE_CONFIG = os.getenv("RCLONE_CONFIG", os.path.join(ARQUIVOS_DIR, ".drive", "rclone.conf"))

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://browserless:3000")
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "scraper")
# separados por ',' ou ';' (o README sempre documentou ';')
PROXY_POOL = [
    p.strip() for p in os.getenv("PROXY_POOL", "").replace(";", ",").split(",") if p.strip()
]

MAX_RETRIES = _int("MAX_RETRIES", 5)
RETRY_BASE_SECONDS = _int("RETRY_BASE_SECONDS", 120)
DISCOVERY_DELAY_SECONDS = _int("DISCOVERY_DELAY_SECONDS", 2)
DOWNLOAD_DELAY_SECONDS = _int("DOWNLOAD_DELAY_SECONDS", 2)
# Whitelist regex p/ PDFs do crawl de portais — só material financeiro/
# contratual (editais, contratos, convenios, atas...); vazio = baixa tudo
CRAWL_PDF_ALLOW = os.getenv(
    "CRAWL_PDF_ALLOW",
    r"edital|contrato|conv.?nio|preg.?o|chamamento|credenciamento|dispensa"
    r"|inexigibilidade|licita|ata.?de.?registro|aditivo|termo.?de"
    r"|ordem.?bancaria|nota.?de.?empenho|empenho|reajuste",
)
DISCOVERY_MAX_DEPTH = _int("DISCOVERY_MAX_DEPTH", 2)
HTTP_TIMEOUT_SECONDS = _int("HTTP_TIMEOUT_SECONDS", 30)
RENDER_FALLBACK = os.getenv("RENDER_FALLBACK", "true").lower() == "true"

CELERY_BEAT_CRON = os.getenv("CELERY_BEAT_CRON", "0 6 * * *")
# Formato: SIGLA=https://url-base;SIGLA2=https://url2
SEED_SECRETARIAS = os.getenv("SEED_SECRETARIAS", "")

# Re-crawl: paginas deduplicadas dentro desta janela (horas) sao puladas;
# passado o prazo, voltam a ser raspadas nas rodadas seguintes
CRAWL_DEDUP_HOURS = _int("CRAWL_DEDUP_HOURS", 20)
# Reconciliador (varredura no Beat): documentos PENDING e jobs RUNNING
# mais velhos que estes limites sao marcados FAILED (orfãos/zumbis)
DOC_ORPHAN_HOURS = _int("DOC_ORPHAN_HOURS", 8)
JOB_STALL_HOURS = _int("JOB_STALL_HOURS", 24)
RECONCILE_CRON = os.getenv("RECONCILE_CRON", "*/30 * * * *")

# Crawl diario das secretarias cadastradas (Beat). Desligue para rodar so as
# coletas automaticas de dados abertos abaixo.
# Fuso para o horario das agendas por fonte (Para: UTC-3, sem horario de verao)
AGENDA_UTC_OFFSET_HORAS = _int("AGENDA_UTC_OFFSET_HORAS", -3)
# Varredura diaria de TODOS os portais cadastrados (CELERY_BEAT_CRON). Desligada
# por padrao: so roda sozinho o que o usuario marcar na coluna "Agenda diaria" do
# painel. Ligue (true) so se quiser a varredura geral de madrugada.
BEAT_CRAWL_ENABLED = os.getenv("BEAT_CRAWL_ENABLED", "false").lower() == "true"

# Coleta automatica de datasets (API/PNCP/Diario). Horarios em UTC (BRT = UTC-3).
AUTO_DATASETS_ENABLED = os.getenv("AUTO_DATASETS_ENABLED", "false").lower() == "true"
AUTO_ORGAOS = [o.strip().upper() for o in os.getenv(
    "AUTO_ORGAOS", "DETRAN,PRODEPA,SEDUC,SEGUP,SESPA").split(",") if o.strip()]
# diario: todo dia (edicoes novas); mensais: semanal (competencia nova do mes)
AUTO_DIARIO_CRON = os.getenv("AUTO_DIARIO_CRON", "0 6 * * *")
AUTO_DIARIO_DATASETS = [d.strip() for d in os.getenv(
    "AUTO_DIARIO_DATASETS", "diario_oficial").split(",") if d.strip()]
AUTO_MENSAIS_CRON = os.getenv("AUTO_MENSAIS_CRON", "0 9 * * 1")
AUTO_MENSAIS_DATASETS = [d.strip() for d in os.getenv(
    "AUTO_MENSAIS_DATASETS",
    "folha,despesas,empenhos,pncp_contratos,transf_voluntarias").split(",") if d.strip()]

# API de dados abertos do Portal da Transparencia (folha de pagamento)
FOLHA_API_BASE = os.getenv(
    "FOLHA_API_BASE",
    "https://api-servidores-publicos.sistemas.pa.gov.br/dados-transparencias",
)
FOLHA_PAGE_SIZE = _int("FOLHA_PAGE_SIZE", 1000)
FOLHA_PAGE_DELAY_SECONDS = _int("FOLHA_PAGE_DELAY_SECONDS", 1)
DESPESAS_API_BASE = os.getenv(
    "DESPESAS_API_BASE",
    "https://api-despesas-publicas.sistemas.pa.gov.br/despesas",
)
# APIs tematicas adicionais (hosts raiz)
EMPENHOS_API_BASE = os.getenv("EMPENHOS_API_BASE", "https://api-notas-empenho.sistemas.pa.gov.br")
TRANSF_REC_API_BASE = os.getenv("TRANSF_REC_API_BASE", "https://api-transferencia-recursos.sistemas.pa.gov.br")
RECEITAS_API_BASE = os.getenv("RECEITAS_API_BASE", "https://api-receitas-publicas.sistemas.pa.gov.br")
DIARIAS_API_BASE = os.getenv("DIARIAS_API_BASE", "https://api-despesas-diarias.sistemas.pa.gov.br")
SUPRIMENTO_API_BASE = os.getenv("SUPRIMENTO_API_BASE", "https://api-suprimento-fundos.sistemas.pa.gov.br")
OBRAS_API_BASE = os.getenv("OBRAS_API_BASE", "https://api-obras-publicas.sistemas.pa.gov.br")
AREAS_API_BASE = os.getenv("AREAS_API_BASE", "https://api-areas-tematicas.sistemas.pa.gov.br")
BENS_MOVEIS_API_BASE = os.getenv("BENS_MOVEIS_API_BASE", "https://api-bens-moveis.sistemas.pa.gov.br")
BENS_IMOVEIS_API_BASE = os.getenv("BENS_IMOVEIS_API_BASE", "https://api-bens-imoveis.sistemas.pa.gov.br")
TRANSF_VOL_API_BASE = os.getenv("TRANSF_VOL_API_BASE", "https://api-transferencias-voluntarias.sistemas.pa.gov.br")
# PNCP — Portal Nacional de Contratacoes Publicas (Lei 14.133/21)
# consulta = dados; pncp-api = arquivos de documento (edital/ata/contrato)
IOEPA_BASE = os.getenv("IOEPA_BASE", "http://www.ioepa.com.br")
PNCP_API_BASE = os.getenv("PNCP_API_BASE", "https://pncp.gov.br/api/consulta/v1")
PNCP_FILES_BASE = os.getenv("PNCP_FILES_BASE", "https://pncp.gov.br/pncp-api/v1")

# Espelho no Google Drive (pasta por sigla) — service account JSON
GDRIVE_ENABLED = os.getenv("GDRIVE_ENABLED", "false").lower() in ("1", "true", "yes")
GDRIVE_CREDENTIALS_PATH = os.getenv(
    "GDRIVE_CREDENTIALS_PATH", "/secrets/gdrive.json"
)
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "")

Q_DISCOVERY = "q.discovery"
Q_DOWNLOAD = "q.download"
Q_DEAD = "q.dead"
# tarefas curtas de controle (agenda, reconciliador): fila e worker proprios
# para nao esperar atras de milhares de paginas na q.discovery
Q_CONTROL = "q.control"
DLX_EXCHANGE = "scraper.dlx"
EXCHANGE = "scraper"
