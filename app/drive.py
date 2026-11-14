"""Espelho no Google Drive — terceiro destino, alem de MinIO e downloads/.

Estrutura: <pasta-raiz>/<sigla>/<ano>/arquivo.pdf (ou folha/<ano>/<mes>/.csv).
Pastas sao achadas/criadas pelo nome — a sigla do orgao ja e o nome da pasta.
Auth: service account; as pastas do usuario sao compartilhadas com o email
da conta de servico uma unica vez."""

import logging
import threading

from app import config

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_FOLDER_MIME = "application/vnd.google-apps.folder"

_service = None
_folder_ids: dict[tuple[str, str], str] = {}  # (parent_id, nome) -> folder id
_lock = threading.Lock()


def _drive():
    global _service
    if _service is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            config.GDRIVE_CREDENTIALS_PATH, scopes=_SCOPES
        )
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _find_or_create_folder(name: str, parent_id: str) -> str:
    key = (parent_id, name)
    if key in _folder_ids:
        return _folder_ids[key]
    with _lock:
        if key in _folder_ids:
            return _folder_ids[key]
        svc = _drive()
        q = (
            f"mimeType='{_FOLDER_MIME}' and name='{_escape(name)}' "
            f"and '{parent_id}' in parents and trashed=false"
        )
        found = (
            svc.files()
            .list(q=q, fields="files(id)", supportsAllDrives=True)
            .execute()
            .get("files", [])
        )
        if found:
            fid = found[0]["id"]
        else:
            body = {
                "name": name,
                "mimeType": _FOLDER_MIME,
                "parents": [parent_id],
            }
            fid = (
                svc.files()
                .create(body=body, fields="id", supportsAllDrives=True)
                .execute()["id"]
            )
        _folder_ids[key] = fid
        return fid


def upload_file(tmp, rel_path: str, content_type: str) -> str | None:
    """Sobe tmp para <raiz>/<rel_path> (ex.: seduc/2024/portaria.pdf).

    Idempotente: re-upload atualiza o arquivo de mesmo nome na pasta.
    Best-effort: falhas so sao logadas — o objeto ja esta seguro em
    MinIO + disco. Devolve o file id do Drive ou None."""
    if not config.GDRIVE_ENABLED:
        return None
    if not config.GDRIVE_ROOT_FOLDER_ID:
        log.warning("GDRIVE_ENABLED mas GDRIVE_ROOT_FOLDER_ID vazio — skip")
        return None
    try:
        from googleapiclient.http import MediaIoBaseUpload

        *dirs, name = rel_path.split("/")
        parent = config.GDRIVE_ROOT_FOLDER_ID
        for d in dirs:
            parent = _find_or_create_folder(d, parent)

        svc = _drive()
        q = (
            f"name='{_escape(name)}' and '{parent}' in parents "
            f"and trashed=false"
        )
        existing = (
            svc.files()
            .list(q=q, fields="files(id)", supportsAllDrives=True)
            .execute()
            .get("files", [])
        )
        tmp.seek(0)
        media = MediaIoBaseUpload(tmp, mimetype=content_type, resumable=True)
        if existing:
            file_id = existing[0]["id"]
            svc.files().update(fileId=file_id, media_body=media).execute()
            action = "atualizado"
        else:
            body = {"name": name, "parents": [parent]}
            file_id = (
                svc.files()
                .create(
                    body=body,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()["id"]
            )
            action = "criado"
        log.info("Drive %s: %s -> %s", action, rel_path, file_id)
        return file_id
    except Exception:
        log.exception("Falha no upload ao Drive: %s", rel_path)
        return None
