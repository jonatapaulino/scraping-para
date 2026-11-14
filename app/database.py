import logging
import time

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from app import config

log = logging.getLogger(__name__)

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(attempts: int = 30, delay: float = 2.0) -> None:
    from app import models

    for i in range(attempts):
        try:
            models.Base.metadata.create_all(engine)
            return
        except (OperationalError, ProgrammingError) as exc:
            if i == attempts - 1:
                raise
            log.warning("Postgres indisponivel (%s), tentativa %d/%d", exc, i + 1, attempts)
            time.sleep(delay)
