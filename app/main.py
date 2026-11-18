import logging
from contextlib import asynccontextmanager
from pathlib import Path

import pika
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi_pagination import add_pagination
from sqlalchemy import text

from app import broker, config, json_logging, storage
from app.database import SessionLocal, engine, init_db
from app.models import Secretaria
from app.routers import router

json_logging.setup()
log = logging.getLogger(__name__)


def seed_secretarias() -> None:
    """Semeia secretarias a partir de SEED_SECRETARIAS (SIGLA=url;SIGLA2=url2)."""
    pairs = [p.strip() for p in config.SEED_SECRETARIAS.split(";") if "=" in p]
    if not pairs:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with SessionLocal() as s:
        for pair in pairs:
            sigla, _, url = pair.partition("=")
            s.execute(
                pg_insert(Secretaria)
                .values(sigla=sigla.strip().upper(), url_base=url.strip())
                .on_conflict_do_nothing(index_elements=["sigla"])
            )
        s.commit()
        log.info("Seed: %d secretarias registradas", len(pairs))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    seed_secretarias()
    try:
        conn = broker._connect()
        broker.declare_topology(conn.channel())
        conn.close()
    except pika.exceptions.AMQPError:
        log.warning("RabbitMQ indisponivel no startup — DLX sera declarada sob demanda")
    yield


app = FastAPI(title="Scraper Gov-PA", version="2.0.0", lifespan=lifespan)
app.include_router(router)
add_pagination(app)


@app.get("/health")
def health():
    """Health profundo: verifica cada dependencia com timeout curto."""
    deps = {}
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        deps["postgres"] = "ok"
    except Exception:
        deps["postgres"] = "down"
    try:
        broker._connect().close()
        deps["rabbitmq"] = "ok"
    except Exception:
        deps["rabbitmq"] = "down"
    try:
        storage.client().bucket_exists(config.MINIO_BUCKET)
        deps["minio"] = "ok"
    except Exception:
        deps["minio"] = "down"
    healthy = all(v == "ok" for v in deps.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "deps": deps},
        status_code=200 if healthy else 503,
    )


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")


@app.middleware("http")
async def _ui_sem_cache(request, call_next):
    """Painel sempre revalida: o navegador nao fica com app.js/style.css antigos."""
    resp = await call_next(request)
    if request.url.path.startswith("/ui"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
