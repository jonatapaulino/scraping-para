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
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
)


def _beat_schedule() -> dict:
    parts = config.CELERY_BEAT_CRON.split()
    if len(parts) != 5:
        log.error("CELERY_BEAT_CRON invalido: %r", config.CELERY_BEAT_CRON)
        return {}
    return {
        "dispatch-active-secretarias": {
            "task": "app.tasks.dispatch_scheduled",
            "schedule": crontab(
                minute=parts[0], hour=parts[1], day_of_month=parts[2],
                month_of_year=parts[3], day_of_week=parts[4],
            ),
        }
    }


celery.conf.beat_schedule = _beat_schedule()
