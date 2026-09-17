"""Preview and publish project copies with crash-recoverable filesystem staging."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from ..config_documents import config_document
from ..global_settings import load_settings
from .archive import JSON_COLUMNS, TABLES, Archive, check_schema
from .exporter import ACTIVE
from .paths import file_mapping, remap_document, rewrite_resource_links
from .registry import merge_registry


class RegistryChanged(ValueError):
    """The preview must be refreshed before importing against different defaults."""


def _results(conn, package_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT source_id,project_id,status,error FROM transfer_imports
           WHERE package_id=%s ORDER BY created_at,source_id""",
        (package_id,),
    ).fetchall()
    return [dict(zip(("source_id", "project_id", "status", "error"), row)) for row in rows]


def results(pool, package_id: str) -> list[dict]:
    with pool.connection() as conn:
        return _results(conn, package_id)


def preview(pool, path: Path) -> dict:
    with Archive(path) as archive, pool.connection() as conn:
        check_schema(conn)
        current = load_settings(connection=conn)
        registry = config_document(current.config)
        existing = {row[0] for row in conn.execute("SELECT name FROM projects").fetchall()}
        imported = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT source_id,status,package_sha256 FROM transfer_imports WHERE package_id=%s",
                (archive.package_id,),
            ).fetchall()
        }
        projects = []
        for summary in archive.projects:
            pid = summary["id"]
            previous = imported.get(pid)
            if previous and previous[1] != archive.sha256:
                raise ValueError("This package identifier was already used for different content")
            document = archive.document(pid)
            registry, _, conflicts, missing = merge_registry(
                registry, document["effective_config"], archive.package_id
            )
            project = document["tables"]["projects"][0]
            files = {
                name[len(f"files/{pid}/") :]: record
                for name, record in archive.manifest["entries"].items()
                if name.startswith(f"files/{pid}/")
            }
            # Validate completed downloads before making any destination changes.
            for export in document["tables"]["exports"]:
                if export["status"] == "done" and export["path"]:
                    stored = export["path"].replace("\\", "/")
                    for prefix in (
                        document["source_root"].replace("\\", "/").rstrip("/") + "/",
                        pid + "/",
                    ):
                        if stored.startswith(prefix):
                            stored = stored[len(prefix) :]
                            break
                    if stored not in files:
                        raise ValueError(f"Completed export is missing its file: {summary['name']}")
            warning = []
            if (project.get("meta") or {}).get("babeldoc"):
                warning.append(
                    "BabelDOC projects still require the original bridge session and a reachable bridge URL."
                )
            projects.append(
                {
                    **summary,
                    "name_conflict": project["name"] in existing,
                    "already_imported": bool(previous and previous[0] == "done"),
                    "counts": {table: len(rows) for table, rows in document["tables"].items()},
                    "file_count": len(files),
                    "bytes": sum(item["size"] for item in files.values()),
                    "conflicts": conflicts,
                    "missing_credentials": missing,
                    "warnings": warning,
                }
            )
            existing.add(project["name"])
        return {
            "package_id": archive.package_id,
            "source_version": archive.manifest.get("application_version", "unknown"),
            "registry_revision": current.revision,
            "projects": projects,
        }


def _insert(conn, table: str, row: dict) -> None:
    columns = TABLES[table].split()
    conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns),
        ),
        tuple(
            Jsonb(row[key]) if key in JSON_COLUMNS and row[key] is not None else row[key]
            for key in columns
        ),
    )


def _cleanup_unpublished(conn, root: Path, package_id: str, source_id: str, pid: str) -> bool:
    # Check committed database state first: a lost COMMIT acknowledgement is not a rollback.
    exists = conn.execute("SELECT 1 FROM projects WHERE id=%s", (pid,)).fetchone()
    if exists:
        conn.execute(
            "UPDATE transfer_imports SET status='done',error=NULL WHERE package_id=%s AND source_id=%s",
            (package_id, source_id),
        )
        return False
    for directory in (root / pid, root / ".transfers" / "staging" / pid):
        if directory.exists():
            shutil.rmtree(directory)
    return True


def recover_imports(pool, root: Path) -> None:
    """Clean only unpublished directories owned by an abandoned import journal."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT package_id,source_id,project_id FROM transfer_imports WHERE status='staging'"
        ).fetchall()
        conn.commit()
        for package_id, source_id, pid in rows:
            key = f"wenyi:transfer:{package_id}:{source_id}"
            locked = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
            ).fetchone()[0]
            conn.commit()
            if not locked:
                continue
            try:
                if _cleanup_unpublished(conn, root, package_id, source_id, pid):
                    conn.execute(
                        "UPDATE transfer_imports SET status='error',error='Import interrupted; retry the original archive' WHERE package_id=%s AND source_id=%s",
                        (package_id, source_id),
                    )
                conn.commit()
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
                conn.commit()


def import_projects(
    pool, root: Path, path: Path, selected: list[str], registry_revision: int
) -> list[dict]:
    root = root.resolve()
    # Repeat validation at the write boundary, not just at the upload preview.
    preview(pool, path)
    with Archive(path) as archive:
        available = {item["id"] for item in archive.projects}
        if (
            not selected
            or len(set(selected)) != len(selected)
            or not set(selected).issubset(available)
        ):
            raise ValueError("Select distinct projects from this archive")
        for source_id in selected:
            document = archive.document(source_id)
            key = f"wenyi:transfer:{archive.package_id}:{source_id}"
            with pool.connection() as conn:
                if not conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
                ).fetchone()[0]:
                    raise ValueError("This project is already being imported")
                conn.commit()
                pid = None
                try:
                    previous = conn.execute(
                        "SELECT project_id,status,package_sha256 FROM transfer_imports WHERE package_id=%s AND source_id=%s",
                        (archive.package_id, source_id),
                    ).fetchone()
                    if previous and previous[2] != archive.sha256:
                        raise ValueError("Package identifier already belongs to different content")
                    if previous and previous[1] == "done":
                        conn.commit()
                        continue
                    pid = previous[0] if previous else uuid4().hex[:16]
                    if previous:
                        if not _cleanup_unpublished(conn, root, archive.package_id, source_id, pid):
                            conn.commit()
                            continue
                    conn.execute(
                        """INSERT INTO transfer_imports(package_id,source_id,project_id,package_sha256)
                           VALUES(%s,%s,%s,%s) ON CONFLICT(package_id,source_id)
                           DO UPDATE SET status='staging',error=NULL""",
                        (archive.package_id, source_id, pid, archive.sha256),
                    )
                    conn.commit()
                    names = [
                        name[len(f"files/{source_id}/") :]
                        for name in archive.manifest["entries"]
                        if name.startswith(f"files/{source_id}/")
                    ]
                    files = file_mapping(names, archive.package_id)
                    staging = root / ".transfers" / "staging" / pid
                    staging.mkdir(parents=True, exist_ok=False)
                    for old, new in files.items():
                        target = staging / new
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with (
                            archive.zip.open(f"files/{source_id}/{old}") as src,
                            target.open("wb") as dst,
                        ):
                            shutil.copyfileobj(src, dst, length=1024 * 1024)
                    for old, new in files.items():
                        if old != document.get("source_file"):
                            rewrite_resource_links(staging / new, old, files)

                    with conn.transaction():
                        conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended('wenyi:settings',0))"
                        )
                        current = load_settings(connection=conn)
                        if current.revision != registry_revision:
                            raise RegistryChanged(
                                "Global settings changed; refresh the migration preview"
                            )
                        registry, project_config, _, _ = merge_registry(
                            config_document(current.config),
                            document["effective_config"],
                            archive.package_id,
                        )
                        id_maps = {}
                        for table, rows in document["tables"].items():
                            serial = "insertion_id" if table == "glossary" else "id"
                            if table == "projects" or serial not in TABLES[table].split():
                                continue
                            id_maps[table] = {
                                row[serial]: conn.execute(
                                    "SELECT nextval(pg_get_serial_sequence(%s,%s))", (table, serial)
                                ).fetchone()[0]
                                for row in rows
                            }
                        runs = {}
                        for row in document["tables"]["jobs"]:
                            for field in ("run_id", "arq_job_id"):
                                if row[field]:
                                    runs.setdefault(row[field], uuid4().hex)
                        for table in TABLES:
                            for original in document["tables"][table]:
                                row = remap_document(
                                    original,
                                    old_pid=source_id,
                                    new_pid=pid,
                                    source_root=document["source_root"],
                                    files=files,
                                    runs=runs,
                                    exports=id_maps.get("exports", {}),
                                    jobs=id_maps.get("jobs", {}),
                                )
                                if table == "projects":
                                    row["id"] = pid
                                    row["config"] = project_config
                                    if conn.execute(
                                        "SELECT 1 FROM projects WHERE name=%s", (row["name"],)
                                    ).fetchone():
                                        row["name"] += f" (import {pid[:8]})"
                                    if row["status"] in ACTIVE:
                                        row["status"] = "paused"
                                elif table in id_maps:
                                    serial = "insertion_id" if table == "glossary" else "id"
                                    row[serial] = id_maps[table][original[serial]]
                                if table == "jobs" and row["status"] in {"queued", "running"}:
                                    row["status"] = "interrupted"
                                if table == "exports" and row["status"] in {
                                    "pending",
                                    "queued",
                                    "running",
                                }:
                                    row.update(
                                        status="error",
                                        error="Export was interrupted before migration; create a new export",
                                    )
                                _insert(conn, table, row)
                        conn.execute(
                            """INSERT INTO application_settings(id,document,default_template,revision) VALUES(1,%s,%s,%s)
                               ON CONFLICT(id) DO UPDATE SET document=EXCLUDED.document,revision=EXCLUDED.revision,updated_at=now()""",
                            (Jsonb(registry), current.default_template, current.revision + 1),
                        )
                        # Only new project directories are published; existing projects are never overwritten.
                        final = root / pid
                        if final.exists():
                            raise ValueError("Destination project directory already exists")
                        staging.rename(final)
                        conn.execute(
                            "UPDATE transfer_imports SET status='done',error=NULL WHERE package_id=%s AND source_id=%s",
                            (archive.package_id, source_id),
                        )
                    registry_revision += 1
                except Exception as error:
                    conn.rollback()
                    if pid and _cleanup_unpublished(conn, root, archive.package_id, source_id, pid):
                        conn.execute(
                            "UPDATE transfer_imports SET status='error',error=%s WHERE package_id=%s AND source_id=%s",
                            (str(error), archive.package_id, source_id),
                        )
                    conn.commit()
                    raise
                finally:
                    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
                    conn.commit()
        return results(pool, archive.package_id)
