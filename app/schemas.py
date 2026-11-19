import re
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.coleta import ColetaConfig
from app.models import DownloadStatus, JobStatus


def _check_url(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    p = urlparse(v)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("URL invalida — use http(s)://host/caminho")
    return v


class JobStartRequest(BaseModel):
    secretarias: list[str] = Field(min_length=1)
    force_full_scrape: bool = False
    # None = usa o max_depth da secretaria (ou o default global)
    max_depth: Optional[int] = Field(default=None, ge=0, le=10)
    # fontes de dataset (ex.: IOEPA = Diario Oficial): anos a baixar
    ano_ini: Optional[int] = Field(default=None, ge=2008, le=2100)
    ano_fim: Optional[int] = Field(default=None, ge=2008, le=2100)


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
    tipo: str
    pdfs_descobertos: int
    pdfs_baixados_sucesso: int
    pdfs_falhas: int
    started_at: datetime
    completed_at: Optional[datetime]


class FolhaStartRequest(BaseModel):
    # dataset do portal da transparencia (folha, despesas, ...)
    dataset: str = "folha"
    ano_ini: int = Field(ge=2016, le=2100)
    mes_ini: int = Field(ge=1, le=12)
    ano_fim: int = Field(ge=2016, le=2100)
    mes_fim: int = Field(ge=1, le=12)
    # None/vazio = todos os orgaos do periodo; senao, siglas (estaveis
    # entre periodos — o id_orgao muda a cada competencia)
    orgaos: Optional[list[str]] = None
    force: bool = False


class FolhaOpcoesOut(BaseModel):
    periodos: list[dict]
    orgaos: list[dict]


class OverviewOut(BaseModel):
    queues: dict[str, int]
    documents: dict[str, int]
    jobs: dict[str, int]
    secretarias_ativas: int
    last_24h: dict[str, int]


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
    downloaded_at: Optional[datetime]


def _check_filtro(v: Optional[str]) -> Optional[str]:
    """None = padrao global; '*' ou '' = todos os PDFs (guardado como ''); senao regex valida."""
    if v is None:
        return None
    v = v.strip()
    if v in ("", "*"):
        return ""
    try:
        re.compile(v)
    except re.error as exc:
        raise ValueError(f"Filtro invalido (regex): {exc}")
    return v


class SecretariaIn(BaseModel):
    sigla: str = Field(max_length=20)
    url_base: str = Field(max_length=255)
    is_ativa: bool = True
    max_depth: Optional[int] = Field(default=None, ge=0, le=10)
    pdf_filtro: Optional[str] = Field(default=None, max_length=255)

    @field_validator("pdf_filtro")
    @classmethod
    def _filtro_ok(cls, v: Optional[str]) -> Optional[str]:
        return _check_filtro(v)

    @field_validator("url_base")
    @classmethod
    def _url_ok(cls, v: str) -> str:
        return _check_url(v)


class ColetaIn(ColetaConfig):
    """Cadastro de fonte generica: sigla + configuracao da coleta."""
    sigla: str = Field(min_length=1, max_length=20)
    is_ativa: bool = True


_HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check_hora(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    if not _HORA_RE.match(v):
        raise ValueError("Horario invalido — use HH:MM (ex.: 06:30)")
    return v


class SecretariaUpdate(BaseModel):
    url_base: Optional[str] = Field(default=None, max_length=255)
    is_ativa: Optional[bool] = None
    max_depth: Optional[int] = Field(default=None, ge=0, le=10)
    pdf_filtro: Optional[str] = Field(default=None, max_length=255)
    agendada: Optional[bool] = None
    agenda_hora: Optional[str] = Field(default=None, max_length=5)

    @field_validator("agenda_hora")
    @classmethod
    def _hora_ok(cls, v: Optional[str]) -> Optional[str]:
        return _check_hora(v)

    @field_validator("pdf_filtro")
    @classmethod
    def _filtro_ok(cls, v: Optional[str]) -> Optional[str]:
        return _check_filtro(v)

    @field_validator("url_base")
    @classmethod
    def _url_ok(cls, v: Optional[str]) -> Optional[str]:
        return _check_url(v)


class SecretariaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sigla: str
    url_base: str
    is_ativa: bool
    max_depth: Optional[int]
    tipo: str
    pdf_filtro: Optional[str] = None
    coleta: Optional[dict] = None
    agendada: bool = False
    agenda_hora: str = "06:00"
