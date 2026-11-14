import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models import DownloadStatus, JobStatus


class JobStartRequest(BaseModel):
    secretarias: list[str] = Field(min_length=1)
    force_full_scrape: bool = False
    max_depth: int = Field(default=2, ge=0, le=10)


class JobStarted(BaseModel):
    job_id: uuid.UUID
    secretaria: str


class JobStartResponse(BaseModel):
    job_ids: list[uuid.UUID]


class JobStatusOut(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    pdfs_descobertos: int
    pdfs_baixados_sucesso: int
    pdfs_falhas: int


class JobListOut(BaseModel):
    job_id: uuid.UUID
    secretaria: str
    status: JobStatus
    pdfs_descobertos: int
    pdfs_baixados_sucesso: int
    pdfs_falhas: int
    started_at: datetime
    completed_at: Optional[datetime]


class OverviewOut(BaseModel):
    queues: dict[str, int]
    documents: dict[str, int]
    jobs: dict[str, int]
    secretarias_ativas: int


class DocumentoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    secretaria_id: uuid.UUID
    job_id: uuid.UUID
    url_origem: str
    hash_sha256: Optional[str]
    minio_path: Optional[str]
    tamanho_bytes: Optional[int]
    magic_byte_valido: Optional[bool]
    status_download: DownloadStatus
    created_at: datetime


class SecretariaIn(BaseModel):
    sigla: str = Field(max_length=20)
    url_base: str = Field(max_length=255)
    is_ativa: bool = True


class SecretariaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sigla: str
    url_base: str
    is_ativa: bool
