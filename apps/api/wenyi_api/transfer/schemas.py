"""Public transfer preview and result contracts, included in OpenAPI."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TransferConflict(BaseModel):
    kind: str
    source: str
    target: str


class TransferProject(BaseModel):
    id: str
    name: str
    format: str | None
    status: str
    name_conflict: bool
    already_imported: bool
    counts: dict[str, int]
    file_count: int
    bytes: int
    conflicts: list[TransferConflict]
    missing_credentials: list[str]
    warnings: list[str]


class TransferPreview(BaseModel):
    upload_id: str
    package_id: str
    source_version: str
    registry_revision: int
    projects: list[TransferProject]


class TransferImportRequest(BaseModel):
    project_ids: list[str] = Field(min_length=1)
    registry_revision: int = Field(ge=0)


class TransferResult(BaseModel):
    source_id: str
    project_id: str
    status: str
    error: str | None = None
