import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from app import config

log = logging.getLogger(__name__)

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# Colunas adicionadas apos o deploy inicial — create_all nao altera tabelas
# existentes, entao os ALTERs abaixo mantem bancos antigos compativeis.
_ADDITIVE_COLUMNS = {
    "secretaria": [
        ("max_depth", "INTEGER"),
        ("tipo", "VARCHAR(10) DEFAULT 'crawl'"),
    ],
    "scraping_job": [("tipo", "VARCHAR(10) DEFAULT 'crawl'")],
    "documento": [("downloaded_at", "TIMESTAMP WITH TIME ZONE")],
}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    ).scalars()
    return set(rows)


def _migrate() -> None:
    """Ajustes de schema pos-deploy (idempotentes)."""
    from app import models

    with engine.begin() as conn:
        cols = _columns(conn, "crawled_page")
        if cols and "url_hash" not in cols:
            # v1 (PK em url TEXT, dedup eterno) — dados sao cache, recria
            conn.execute(text("DROP TABLE crawled_page"))
            log.info("crawled_page v1 descartada — recriando com nova chave")
        for table, additions in _ADDITIVE_COLUMNS.items():
            cols = _columns(conn, table)
            for name, ddl in additions:
                if cols and name not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    log.info("Coluna %s.%s adicionada", table, name)
    models.Base.metadata.create_all(engine)


def init_db(attempts: int = 30, delay: float = 2.0) -> None:
    from app import models

    for i in range(attempts):
        try:
            models.Base.metadata.create_all(engine)
            _migrate()
            return
        except (OperationalError, ProgrammingError) as exc:
            if i == attempts - 1:
                raise
            log.warning("Postgres indisponivel (%s), tentativa %d/%d", exc, i + 1, attempts)
            time.sleep(delay)
