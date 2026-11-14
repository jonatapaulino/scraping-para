import json
import logging

import pika

from app import config

log = logging.getLogger(__name__)


def _connect() -> pika.BlockingConnection:
    params = pika.ConnectionParameters(
        host=config.RABBITMQ_HOST,
        port=config.RABBITMQ_PORT,
        virtual_host=config.RABBITMQ_VHOST,
        credentials=pika.PlainCredentials(config.RABBITMQ_USER, config.RABBITMQ_PASS),
        heartbeat=120,
        blocked_connection_timeout=60,
    )
    return pika.BlockingConnection(params)


def declare_topology(channel) -> None:
    """DLX fanout + fila cemiterio. As filas do Celery sao declaradas pelo kombu."""
    channel.exchange_declare(exchange=config.DLX_EXCHANGE, exchange_type="fanout", durable=True)
    channel.queue_declare(queue=config.Q_DEAD, durable=True)
    channel.queue_bind(queue=config.Q_DEAD, exchange=config.DLX_EXCHANGE)


def send_to_dead(payload: dict, reason: str) -> bool:
    """Publica uma tarefa morta na DLX (apos esgotar os retries do Celery).

    Retorna False se o broker estiver indisponivel — o chamador deve
    persistir o dead letter localmente para nao perder o rastro."""
    try:
        conn = _connect()
        ch = conn.channel()
        declare_topology(ch)
        ch.basic_publish(
            exchange=config.DLX_EXCHANGE,
            routing_key="",
            body=json.dumps({"payload": payload, "reason": reason}).encode(),
            properties=pika.BasicProperties(
                delivery_mode=pika.DeliveryMode.Persistent,
                content_type="application/json",
            ),
        )
        conn.close()
        return True
    except pika.exceptions.AMQPError:
        log.exception("Falha ao publicar na DLX")
        return False


def queue_depths() -> dict:
    depths = {config.Q_DISCOVERY: -1, config.Q_DOWNLOAD: -1, config.Q_DEAD: -1}
    try:
        conn = _connect()
        ch = conn.channel()
        for q in depths:
            try:
                ok = ch.queue_declare(queue=q, passive=True)
                depths[q] = ok.method.message_count
            except pika.exceptions.ChannelClosedByBroker:
                ch = conn.channel()
        conn.close()
    except pika.exceptions.AMQPError:
        log.exception("Falha ao consultar profundidade das filas")
    return depths
