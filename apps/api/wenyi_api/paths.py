"""Organize uploaded originals and exported files within the data volume."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import settings


def store_source(value: str, *, root: str | None = None) -> str:
    """Persist owned source paths relative to DATA_DIR; accept legacy external paths."""
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(Path(root or settings.data_dir).resolve()).as_posix()
        except ValueError:
            return value
    return value.replace("\\", "/")


def resolve_source(value: str, *, root: str | None = None) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else Path(root or settings.data_dir).resolve() / path)


def portable_references(value, *, root: str | None = None):
    """Normalize typed owned file references, preserving text and frozen config records."""
    fields = {"source_path", "path", "output_path", "out_path", "outputs", "run_dir"}

    def visit(item, key=""):
        if key == "config_snapshot":
            return item
        if isinstance(item, dict):
            return {
                name: visit(child, key if key == "outputs" else name)
                for name, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if key in fields and isinstance(item, str) and Path(item).is_absolute():
            return store_source(item, root=root)
        return item

    return visit(value)


def safe_filename(value: str) -> str:
    """Use a portable basename, including on Linux when preparing Windows transfers."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(" .") or "translation"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0]):
        name = "_" + name
    return name[:120].rstrip(" .")


def project_dir(project_id: str) -> str:
    d = os.path.join(settings.data_dir, project_id)
    os.makedirs(d, exist_ok=True)
    return d


def source_path(project_id: str, fmt: str) -> str:
    ext = {
        "epub": "epub",
        "text": "txt",
        "fb2": "fb2",
        "html": "html",
        "pdf": "pdf",
    }.get(fmt, fmt or "bin")
    return os.path.join(project_dir(project_id), f"source.{ext}")


def source_cache_dir(project_id: str) -> str:
    d = os.path.join(project_dir(project_id), "source")
    os.makedirs(d, exist_ok=True)
    return d


def exports_dir(project_id: str) -> str:
    d = os.path.join(project_dir(project_id), "exports")
    os.makedirs(d, exist_ok=True)
    return d
