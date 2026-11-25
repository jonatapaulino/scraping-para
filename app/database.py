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
        ("tipo", "VARCHAR(32) DEFAULT 'crawl'"),
        ("pdf_filtro", "VARCHAR(255)"),
        ("coleta_json", "TEXT"),
        ("agendada", "BOOLEAN DEFAULT FALSE"),
        ("agenda_hora", "VARCHAR(5) DEFAULT '06:00'"),
        ("agendada_em", "TIMESTAMP WITH TIME ZONE"),
    ],
    "scraping_job": [("tipo", "VARCHAR(32) DEFAULT 'crawl'"), ("progresso_json", "TEXT")],
    "documento": [("downloaded_at", "TIMESTAMP WITH TIME ZONE"), ("erro", "TEXT")],
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
        # bancos antigos criaram 'tipo' como VARCHAR(10) — nomes de dataset
        # como 'transf_voluntarias' (18) nao cabiam
        for table in ("secretaria", "scraping_job"):
            tam = conn.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'tipo'"
                ),
                {"t": table},
            ).scalar_one_or_none()
            if tam is not None and tam < 32:
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN tipo TYPE VARCHAR(32)"))
                log.info("Coluna %s.tipo ampliada para VARCHAR(32)", table)
        # minio_path era VARCHAR(255): caminhos longos derrubavam o download
        tipo_mp = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'documento' AND column_name = 'minio_path'"
            )
        ).scalar_one_or_none()
        if tipo_mp and tipo_mp != "text":
            conn.execute(text("ALTER TABLE documento ALTER COLUMN minio_path TYPE TEXT"))
            log.info("Coluna documento.minio_path ampliada para TEXT")
    models.Base.metadata.create_all(engine)
    # Valores novos nos enums nativos do Postgres (create_all nao altera tipos
    # existentes). ADD VALUE nao roda dentro de transacao em versoes antigas -> AUTOCOMMIT.
    for tipo, valor in (("jobstatus", "CANCELLED"), ("downloadstatus", "SEM_DADOS"),
                        ("downloadstatus", "DESCARTADO")):
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f"ALTER TYPE {tipo} ADD VALUE IF NOT EXISTS '{valor}'"))
        except Exception:
            log.exception("Nao foi possivel adicionar %s ao enum %s", valor, tipo)


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
