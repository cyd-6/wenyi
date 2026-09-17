"""Upload, validate and import data-only migration archives; never execute archive code."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, UploadFile

from ..config import settings
from ..db import get_pool
from ..transfer.archive import MAX_BYTES, Archive, valid_id
from ..transfer.importer import RegistryChanged, import_projects, preview, results
from ..transfer.schemas import TransferImportRequest, TransferPreview, TransferResult

router = APIRouter(prefix="/transfers", tags=["transfers"])


def upload_path(upload_id: str) -> Path:
    try:
        valid_id(upload_id)
    except ValueError as error:
        raise HTTPException(404, "Unknown transfer upload") from error
    path = Path(settings.data_dir) / ".transfers" / "uploads" / f"{upload_id}.wenyi.zip"
    if not path.is_file():
        raise HTTPException(404, "Unknown transfer upload")
    return path


def preview_response(upload_id: str, path: Path) -> dict:
    try:
        return {"upload_id": upload_id, **preview(get_pool(), path)}
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        raise HTTPException(422, f"Invalid transfer: {error}") from error


@router.post("/preview", response_model=TransferPreview)
async def upload(file: UploadFile):
    upload_id = uuid4().hex
    directory = Path(settings.data_dir) / ".transfers" / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{upload_id}.wenyi.zip"
    try:
        size = 0
        with path.open("xb") as target:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > MAX_BYTES:
                    raise HTTPException(413, "Transfer archive exceeds 8 GiB")
                await asyncio.to_thread(target.write, block)
        return await asyncio.to_thread(preview_response, upload_id, path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.get("/{upload_id}", response_model=TransferPreview)
def inspect_upload(upload_id: str):
    return preview_response(upload_id, upload_path(upload_id))


@router.post("/{upload_id}/import", response_model=list[TransferResult])
def import_upload(upload_id: str, body: TransferImportRequest):
    try:
        return import_projects(
            get_pool(),
            Path(settings.data_dir),
            upload_path(upload_id),
            body.project_ids,
            body.registry_revision,
        )
    except RegistryChanged as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{upload_id}/results", response_model=list[TransferResult])
def import_results(upload_id: str):
    with Archive(upload_path(upload_id)) as archive:
        return results(get_pool(), archive.package_id)
