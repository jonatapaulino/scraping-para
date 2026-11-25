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
    CANCELLED = "CANCELLED"


class DownloadStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    # a fonte respondeu, mas nao havia registros (ex.: orgao sem folha no mes) —
    # nao e erro; separado para nao inflar o contador de falhas
    SEM_DADOS = "SEM_DADOS"
    # falha que o usuario tirou da lista no painel (ex.: link quebrado de vez):
    # nao conta mais como falha e nao e refeita; o registro fica para o crawl
    # nao baixar o mesmo link de novo
    DESCARTADO = "DESCARTADO"


class Secretaria(Base):
    __tablename__ = "secretaria"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sigla: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    url_base: Mapped[str] = mapped_column(String(255))
    is_ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    max_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 'crawl' = portal navegavel; 'api' = fonte de dados estruturada
    # (ex.: Portal da Transparencia) — fora das rodadas de crawl do Beat
    tipo: Mapped[str] = mapped_column(String(32), default="crawl")
    # Filtro (regex) dos PDFs a baixar nesta fonte. NULL = padrao global
    # (CRAWL_PDF_ALLOW, so material financeiro); "" = baixa todos os PDFs.
    pdf_filtro: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # tipo 'coleta': configuracao (JSON) da fonte generica cadastrada pelo painel
    coleta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Agenda diaria escolhida no painel: raspa a fonte todo dia em `agenda_hora`
    # (HH:MM, fuso AGENDA_UTC_OFFSET_HORAS). Se o sistema estiver desligado na
    # hora, roda assim que voltar. `agendada_em` evita disparar um horario que
    # ja tinha passado quando a agenda foi ligada.
    agendada: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    agenda_hora: Mapped[str] = mapped_column(String(5), default="06:00", server_default="06:00")
    agendada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def coleta(self) -> dict | None:
        import json
        return json.loads(self.coleta_json) if self.coleta_json else None

    jobs: Mapped[list["ScrapingJob"]] = relationship(back_populates="secretaria")
    documentos: Mapped[list["Documento"]] = relationship(back_populates="secretaria")


class ScrapingJob(Base):
    __tablename__ = "scraping_job"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    secretaria_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("secretaria.id"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.RUNNING)
    # 'crawl' = raspagem de paginas; 'folha' = coleta na API da transparencia
    tipo: Mapped[str] = mapped_column(String(32), default="crawl")
    total_pdfs_encontrados: Mapped[int] = mapped_column(Integer, default=0)
    # tarefas que nao sao feitas de documentos (ex.: envio ao Google Drive):
    # andamento em JSON ({"feitos", "total", "bytes", ...}) para o painel
    progresso_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # TEXT: caminhos de dataset/anexo podem passar de 255 (antes: erro no UPDATE)
    minio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    tamanho_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    magic_byte_valido: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status_download: Mapped[DownloadStatus] = mapped_column(
        Enum(DownloadStatus), default=DownloadStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # motivo da falha, em portugues, para o painel ("Link quebrado (HTTP 404)"...)
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)

    secretaria: Mapped[Secretaria] = relationship(back_populates="documentos")
    job: Mapped[ScrapingJob] = relationship(back_populates="documentos")


class ColetaFila(Base):
    """Fila automatica de coletas de dados abertos, montada no painel: o
    sistema roda UMA por vez, em ordem, e passa para a proxima sozinho quando
    a anterior termina (worker-control, a cada minuto)."""

    __tablename__ = "coleta_fila"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(32))
    ano_ini: Mapped[int] = mapped_column(Integer)
    mes_ini: Mapped[int] = mapped_column(Integer)
    ano_fim: Mapped[int] = mapped_column(Integer)
    mes_fim: Mapped[int] = mapped_column(Integer)
    orgaos_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # None = todos
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    # AGUARDANDO -> RODANDO -> CONCLUIDA | FALHOU | CANCELADA
    status: Mapped[str] = mapped_column(String(12), default="AGUARDANDO", index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    iniciada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    concluida_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- Tabelas internas de infraestrutura (fora do contrato SDD) ---


class JobPending(Base):
    """Contador de tarefas em voo por job — detecta quando o job drenou."""

    __tablename__ = "job_pending"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("scraping_job.id"), primary_key=True
    )
    pending: Mapped[int] = mapped_column(Integer, default=0)


class CrawledPage(Base):
    """Dedup de paginas navegadas. A claim expira apos CRAWL_DEDUP_HOURS —
    rodadas futuras re-raspam a pagina e descobrem documentos novos."""

    __tablename__ = "crawled_page"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class DeadLetter(Base):
    """Fallback local quando a publicacao na DLX falha (broker indisponivel)."""

    __tablename__ = "dead_letter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(255))
    payload: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
