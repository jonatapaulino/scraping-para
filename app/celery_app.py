import logging

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from app import config, json_logging

json_logging.setup()
log = logging.getLogger(__name__)

celery = Celery("scraper", broker=config.CELERY_BROKER_URL, include=["app.tasks"])

celery.conf.update(
    task_queues=(
        Queue(
            config.Q_DISCOVERY,
            Exchange(config.EXCHANGE, type="direct"),
            routing_key=config.Q_DISCOVERY,
            durable=True,
            queue_arguments={
                "x-dead-letter-exchange": config.DLX_EXCHANGE,
            },
        ),
        Queue(
            config.Q_DOWNLOAD,
            Exchange(config.EXCHANGE, type="direct"),
            routing_key=config.Q_DOWNLOAD,
            durable=True,
            queue_arguments={
                "x-dead-letter-exchange": config.DLX_EXCHANGE,
            },
        ),
    ),
    task_routes={
        "app.tasks.discover_page": {"queue": config.Q_DISCOVERY},
        "app.tasks.download_document": {"queue": config.Q_DOWNLOAD},
        "app.tasks.dispatch_scheduled": {"queue": config.Q_DISCOVERY},
        "app.tasks.reconcile_stalled": {"queue": config.Q_DISCOVERY},
        "app.tasks.discover_dataset": {"queue": config.Q_DISCOVERY},
        "app.tasks.fetch_dataset_mes": {"queue": config.Q_DOWNLOAD},
        "app.tasks.dispatch_datasets_scheduled": {"queue": config.Q_DISCOVERY},
        "app.tasks.coletar_fonte": {"queue": config.Q_DOWNLOAD},
        "app.tasks.dispatch_agendadas": {"queue": config.Q_DISCOVERY},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
)


def _cron(expr: str):
    parts = expr.split()
    if len(parts) != 5:
        log.error("Cron invalido: %r", expr)
        return None
    return crontab(
        minute=parts[0], hour=parts[1], day_of_month=parts[2],
        month_of_year=parts[3], day_of_week=parts[4],
    )


def _beat_schedule() -> dict:
    schedule = {}
    dispatch = _cron(config.CELERY_BEAT_CRON)
    if dispatch and config.BEAT_CRAWL_ENABLED:
        schedule["dispatch-active-secretarias"] = {
            "task": "app.tasks.dispatch_scheduled",
            "schedule": dispatch,
        }
    if config.AUTO_DATASETS_ENABLED:
        for nome, expr, grupo in (
            ("auto-diario", config.AUTO_DIARIO_CRON, "diario"),
            ("auto-mensais", config.AUTO_MENSAIS_CRON, "mensais"),
        ):
            c = _cron(expr)
            if c:
                schedule[nome] = {
                    "task": "app.tasks.dispatch_datasets_scheduled",
                    "schedule": c,
                    "args": (grupo,),
                }
    # agendas por fonte (painel): confere a cada 5 min quem ja esta na hora
    schedule["dispatch-agendadas"] = {
        "task": "app.tasks.dispatch_agendadas",
        "schedule": crontab(minute="*/5"),
    }
    reconcile = _cron(config.RECONCILE_CRON)
    if reconcile:
        schedule["reconcile-stalled"] = {
            "task": "app.tasks.reconcile_stalled",
            "schedule": reconcile,
        }
    return schedule


celery.conf.beat_schedule = _beat_schedule()
