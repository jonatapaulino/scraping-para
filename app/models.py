import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DownloadStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class Secretaria(Base):
    __tablename__ = "secretaria"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sigla: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    url_base: Mapped[str] = mapped_column(String(255))
    is_ativa: Mapped[bool] = mapped_column(Boolean, default=True)

    jobs: Mapped[list["ScrapingJob"]] = relationship(back_populates="secretaria")
    documentos: Mapped[list["Documento"]] = relationship(back_populates="secretaria")


class ScrapingJob(Base):
    __tablename__ = "scraping_job"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    secretaria_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("secretaria.id"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.RUNNING)
    total_pdfs_encontrados: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    secretaria: Mapped[Secretaria] = relationship(back_populates="jobs")
    documentos: Mapped[list["Documento"]] = relationship(back_populates="job")


class Documento(Base):
    __tablename__ = "documento"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    secretaria_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("secretaria.id"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("scraping_job.id"), index=True)
    url_origem: Mapped[str] = mapped_column(Text, unique=True)
    hash_sha256: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    minio_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tamanho_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    magic_byte_valido: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status_download: Mapped[DownloadStatus] = mapped_column(
        Enum(DownloadStatus), default=DownloadStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    secretaria: Mapped[Secretaria] = relationship(back_populates="documentos")
    job: Mapped[ScrapingJob] = relationship(back_populates="documentos")


# --- Tabelas internas de infraestrutura (fora do contrato SDD) ---


class JobPending(Base):
    """Contador de tarefas em voo por job — detecta quando o job drenou."""

    __tablename__ = "job_pending"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("scraping_job.id"), primary_key=True
    )
    pending: Mapped[int] = mapped_column(Integer, default=0)


class CrawledPage(Base):
    """Dedup de paginas ja navegadas (idempotencia do discovery)."""

    __tablename__ = "crawled_page"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
