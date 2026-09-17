"""Portable data-only archives with explicit schemas and streaming file validation."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath

FORMAT = "wenyi-project-transfer"
VERSION = 1
MAX_BYTES = 8 * 1024**3
MAX_EXPANDED_BYTES = 16 * 1024**3
TABLES = {
    "projects": "id name title fmt source_lang target_lang source_path source_sha256 source_meta initialized initialization_sha256 status error strategy config manifest meta context annotation_contexts analysis usage report book_title created_at updated_at",
    "chapters": "project_id seq title title_translated href template status review_status manifest_entry meta",
    "segments": "project_id chapter_seq seg_seq source target target_before_polish kind anchor cont meta resource_href",
    "segment_revisions": "id project_id chapter_seq seg_seq kind previous_target new_target created_at",
    "glossary": "insertion_id project_id source target reading type gender aliases first_chapter note status updated_at",
    "term_conflicts": "id project_id source existing_target proposed_target chapter note resolved created_at",
    "artifacts": "project_id key value updated_at",
    "artifact_events": "id project_id key payload created_at",
    "events": "id project_id type payload created_at",
    "exports": "id project_id format options path size status error created_at completed_at",
    "jobs": "id project_id kind status arq_job_id run_id params result error created_at updated_at",
}
JSON_COLUMNS = {
    "source_meta",
    "strategy",
    "config",
    "manifest",
    "meta",
    "context",
    "annotation_contexts",
    "analysis",
    "usage",
    "report",
    "manifest_entry",
    "aliases",
    "value",
    "payload",
    "options",
    "params",
    "result",
}


def json_bytes(value) -> bytes:
    def default(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"Unsupported transfer value: {type(item).__name__}")

    return json.dumps(value, ensure_ascii=False, default=default, separators=(",", ":")).encode(
        "utf-8"
    )


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Invalid archive member path")
    if name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("Archive contains an unsafe path")
    return name


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid transfer identifier")
    return value


def check_schema(conn) -> None:
    for table, columns in TABLES.items():
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        ).fetchall()
        if {row[0] for row in rows} != set(columns.split()):
            raise ValueError(
                f"Unsupported source/target schema for {table}; use the documented official Web version"
            )


class Archive:
    def __init__(self, path: Path):
        self.path = path
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Transfer archive exceeds 8 GiB")
        self.sha256 = digest_file(path)
        self.zip = zipfile.ZipFile(path)
        try:
            members = self.zip.infolist()
            if (
                len(members) > 100000
                or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES
            ):
                raise ValueError("Transfer archive is too large")
            names = set()
            for item in members:
                safe_member(item.filename)
                if (
                    item.filename in names
                    or stat.S_ISLNK(item.external_attr >> 16)
                    or item.flag_bits & 1
                ):
                    raise ValueError("Duplicate, encrypted or symbolic-link archive member")
                names.add(item.filename)
            if self.zip.getinfo("manifest.json").file_size > 16 * 1024**2:
                raise ValueError("Transfer manifest is too large")
            self.manifest = json.loads(self.zip.read("manifest.json"))
            if self.manifest.get("format") != FORMAT or self.manifest.get("version") != VERSION:
                raise ValueError("Unsupported transfer format/version")
            self.package_id = valid_id(self.manifest["package_id"])
            self.projects = self.manifest["projects"]
            if not isinstance(self.projects, list) or not self.projects:
                raise ValueError("Transfer has no projects")
            ids = [valid_id(p["id"]) for p in self.projects]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate project identifier")
            entries = self.manifest["entries"]
            if set(entries) != names - {"manifest.json"}:
                raise ValueError("Transfer member list does not match its manifest")
            for name, record in entries.items():
                parts = PurePosixPath(name).parts
                if not (
                    (
                        len(parts) == 2
                        and parts[0] == "projects"
                        and parts[1] in {i + ".json" for i in ids}
                    )
                    or (len(parts) >= 3 and parts[0] == "files" and parts[1] in ids)
                ):
                    raise ValueError("Unexpected archive member")
                if self.zip.getinfo(name).file_size != record["size"]:
                    raise ValueError(f"Transfer file size mismatch: {name}")
                digest = hashlib.sha256()
                with self.zip.open(name) as handle:
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != record["sha256"]:
                    raise ValueError(f"Transfer checksum mismatch: {name}")
            for pid in ids:
                document = self.document(pid)
                if set(document["tables"]) != set(TABLES):
                    raise ValueError("Unsupported project table set")
                for table, rows in document["tables"].items():
                    for row in rows:
                        if set(row) != set(TABLES[table].split()):
                            raise ValueError(f"Unsupported columns in {table}")
                        if (
                            row.get("project_id", row.get("id") if table == "projects" else pid)
                            != pid
                        ):
                            raise ValueError("Cross-project data in transfer archive")
                if len(document["tables"]["projects"]) != 1:
                    raise ValueError("Transfer must have one project row per document")
                project = document["tables"]["projects"][0]
                source = document.get("source_file")
                if project.get("source_path"):
                    name = f"files/{pid}/{safe_member(source)}"
                    if name not in entries or (
                        project.get("source_sha256")
                        and entries[name]["sha256"] != project["source_sha256"]
                    ):
                        raise ValueError(
                            "Original source is missing or does not match saved translation state"
                        )
        except (KeyError, TypeError, AttributeError) as error:
            self.zip.close()
            raise ValueError("Invalid transfer manifest or project structure") from error
        except BaseException:
            self.zip.close()
            raise

    def document(self, pid: str) -> dict:
        return json.loads(self.zip.read(f"projects/{valid_id(pid)}.json"))

    def close(self):
        self.zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
