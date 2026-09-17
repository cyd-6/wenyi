"""Export selected idle projects using the current official Docker application's locks."""

from __future__ import annotations

import hashlib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from psycopg import sql

from .. import __version__
from ..config_documents import config_document
from ..export_retention import export_history_lock
from ..global_settings import load_settings
from ..project_service import effective_config
from ..storage_pg import PostgresStorage
from .archive import FORMAT, TABLES, VERSION, check_schema, json_bytes, safe_member, valid_id

ACTIVE = {
    "parsing",
    "queued",
    "preparing",
    "translating",
    "reviewing",
    "autofixing",
    "postprocessing",
    "pausing",
}


def export_projects(pool, data_dir: Path, project_ids: list[str], destination: Path) -> dict:
    ids = sorted(set(valid_id(pid) for pid in project_ids))
    if not ids:
        raise ValueError("Select at least one project")
    root = data_dir.resolve()
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "package_id": uuid4().hex,
        "application_version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "projects": [],
        "entries": {},
    }
    temporary = destination.with_suffix(".partial")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with pool.connection() as conn:
            # Shared registry lock serializes configuration snapshots with registry edits.
            conn.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended('wenyi:settings',0))"
            )
            check_schema(conn)
            for pid in ids:
                store = PostgresStorage(pid, pool, run_dir=str(root / pid))
                if not conn.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)", (store._lock_key("write"),)
                ).fetchone()[0]:
                    raise ValueError(f"Project is busy: {pid}; pause it before exporting")
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (store._lock_key("state"),))
                export_history_lock(conn, pid, shared=True)
                status = conn.execute("SELECT status FROM projects WHERE id=%s", (pid,)).fetchone()
                if status is None:
                    raise ValueError(f"Project does not exist: {pid}")
                if (
                    status[0] in ACTIVE
                    or conn.execute(
                        "SELECT 1 FROM jobs WHERE project_id=%s AND status IN ('queued','running')",
                        (pid,),
                    ).fetchone()
                ):
                    raise ValueError(
                        f"Pause project {pid} and wait for its exports to finish before exporting"
                    )
            defaults = load_settings(connection=conn).config
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:

                def add_bytes(name: str, value: bytes):
                    archive.writestr(name, value)
                    manifest["entries"][name] = {
                        "size": len(value),
                        "sha256": hashlib.sha256(value).hexdigest(),
                    }

                for pid in ids:
                    tables = {}
                    for table, fields in TABLES.items():
                        columns = fields.split()
                        order = (
                            "insertion_id"
                            if table == "glossary"
                            else "id"
                            if "id" in columns
                            else columns[1]
                        )
                        cursor = conn.execute(
                            sql.SQL("SELECT {} FROM {} WHERE {}=%s ORDER BY {}").format(
                                sql.SQL(",").join(map(sql.Identifier, columns)),
                                sql.Identifier(table),
                                sql.Identifier("id" if table == "projects" else "project_id"),
                                sql.Identifier(order),
                            ),
                            (pid,),
                        )
                        tables[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    project = tables["projects"][0]
                    directory = root / pid
                    if directory.is_symlink():
                        raise ValueError("Project resource directory may not be a symbolic link")
                    source_file = None
                    if project.get("source_path"):
                        source = root / project["source_path"]
                        try:
                            source_file = source.resolve().relative_to(directory).as_posix()
                        except ValueError as error:
                            raise ValueError(
                                f"Source is outside project resources: {pid}"
                            ) from error
                        if not source.is_file():
                            raise ValueError(f"Original source is missing: {pid}")
                    document = {
                        "tables": tables,
                        "effective_config": config_document(
                            effective_config(project, defaults=defaults)
                        ),
                        "source_root": str(directory),
                        "source_file": source_file,
                    }
                    add_bytes(f"projects/{pid}.json", json_bytes(document))
                    for file in sorted(directory.rglob("*")):
                        if file.is_symlink():
                            raise ValueError(
                                f"Symbolic links cannot be migrated: {file.relative_to(directory)}"
                            )
                        if not file.is_file():
                            continue
                        relative = safe_member(file.relative_to(directory).as_posix())
                        name = f"files/{pid}/{relative}"
                        digest, size = hashlib.sha256(), 0
                        with (
                            file.open("rb") as src,
                            archive.open(name, "w", force_zip64=True) as dst,
                        ):
                            while block := src.read(1024 * 1024):
                                digest.update(block)
                                size += len(block)
                                dst.write(block)
                        manifest["entries"][name] = {"size": size, "sha256": digest.hexdigest()}
                    manifest["projects"].append(
                        {
                            "id": pid,
                            "name": project["name"],
                            "format": project["fmt"],
                            "status": project["status"],
                        }
                    )
                archive.writestr("manifest.json", json_bytes(manifest))
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return manifest
