"""Transfer real saved state, preserve destination projects, and recover interrupted imports."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import test_native_runtime as native_tests
from test_native_runtime import source_project

native_env = native_tests.native_env


def prepared_archive(native_env, tmp_path):
    from wenyi_api import dal, project_service
    from wenyi_api.transfer.exporter import export_projects
    from wenyi_core.glossary.store import GlossaryTerm
    from wenyi_core.ingest.models import Chapter, Document, Segment

    pool, root, _ = native_env
    pid, source = source_project(root)
    storage = project_service.storage_for(pid)
    doc = Document(
        title="Book",
        source_path=str(source),
        fmt="text",
        source_lang="en",
        target_lang="zh",
        chapters=[
            Chapter(
                index=0,
                title="One",
                segments=[
                    Segment(index=0, source="Original", target="译文"),
                    Segment(index=7, source="Pending", target=None),
                ],
            )
        ],
    )
    storage.init_from_document(doc)
    storage.upsert_term(GlossaryTerm(source="Alice", target="爱丽丝"))
    storage.write_artifact(
        "reviews/review-test/autofix/index.json",
        {"records": [{"status": "applied", "segment_index": 0}]},
    )
    storage.save_usage({"total_tokens": 42})
    jid = dal.create_job(pid, "translation", "old-run", run_id="old-run")
    dal.set_job_status(jid, "paused")
    dal.set_project_status(pid, "paused")
    storage.log_event("task_paused", run_id="old-run")
    output = root / pid / "exports" / "1" / "book.txt"
    output.parent.mkdir(parents=True)
    output.write_text("译文", encoding="utf-8")
    eid = dal.create_export(pid, "txt", {})
    from wenyi_api.export_retention import publish_export

    publish_export(pool, pid, eid, str(output), data_dir=str(root))
    archive = tmp_path / "migration.wenyi.zip"
    manifest = export_projects(pool, root, [pid], archive)
    return pid, archive, manifest


def test_round_trip_merges_without_overwriting_and_is_idempotent(native_env, tmp_path):
    from wenyi_api import dal, project_service
    from wenyi_api.transfer.importer import import_projects, preview

    pool, root, _ = native_env
    original, archive, _ = prepared_archive(native_env, tmp_path)
    info = preview(pool, archive)
    assert info["projects"][0]["name_conflict"]
    imported = import_projects(pool, root, archive, [original], info["registry_revision"])
    pid = imported[0]["project_id"]
    assert pid != original
    assert dal.get_project(original)["name"] == "Migration test"
    assert "import" in dal.get_project(pid)["name"]
    store = project_service.storage_for(pid)
    assert store.load_chapter(0).segments[1].index == 7
    assert store.load_chapter(0).segments[1].target is None
    assert store.load_usage()["total_tokens"] == 42
    assert (
        store.read_artifact("reviews/review-test/autofix/index.json")["records"][0]["status"]
        == "applied"
    )
    assert len(store.all_terms()) == 1
    assert (
        Path(store.load_manifest()["source_path"]).read_bytes()
        == Path(project_service.storage_for(original).load_manifest()["source_path"]).read_bytes()
    )
    with pool.connection() as conn:
        output = conn.execute("SELECT path FROM exports WHERE project_id=%s", (pid,)).fetchone()[0]
        assert (root / output).read_text(encoding="utf-8") == "译文"
        assert conn.execute("SELECT count(*) FROM runtime_queue").fetchone()[0] == 0
    again = import_projects(
        pool, root, archive, [original], preview(pool, archive)["registry_revision"]
    )
    assert again == imported
    assert len(dal.list_projects()) == 2


def test_corrupt_archive_is_rejected_before_destination_changes(native_env, tmp_path):
    from wenyi_api import dal
    from wenyi_api.transfer.importer import preview

    _, archive, _ = prepared_archive(native_env, tmp_path)
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(damaged, "w") as output:
        for name in source.namelist():
            value = source.read(name)
            output.writestr(name, value + b"bad" if name.startswith("files/") else value)
    with pytest.raises(ValueError, match="mismatch"):
        preview(native_env[0], damaged)
    assert len(dal.list_projects()) == 1


def test_archive_traversal_and_future_version_are_rejected(native_env, tmp_path):
    from wenyi_api.transfer.archive import Archive

    _, path, _ = prepared_archive(native_env, tmp_path)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(bad, "w") as modified:
        for item in original.namelist():
            modified.writestr(item, original.read(item))
        modified.writestr("../outside", "bad")
    with pytest.raises(ValueError, match="unsafe"):
        Archive(bad)
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(bad, "w") as modified:
        for item in original.namelist():
            content = original.read(item)
            if item == "manifest.json":
                manifest = json.loads(content)
                manifest["version"] = 999
                content = json.dumps(manifest).encode()
            modified.writestr(item, content)
    with pytest.raises(ValueError, match="version"):
        Archive(bad)


def test_publish_failure_rolls_back_rows_and_files_then_allows_retry(
    native_env, tmp_path, monkeypatch
):
    from wenyi_api import dal
    from wenyi_api.transfer import importer

    pool, root, _ = native_env
    pid, archive, manifest = prepared_archive(native_env, tmp_path)
    original_rename = Path.rename

    def fail(source, target):
        if source.parent.name == "staging":
            raise OSError("simulated disk failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError, match="simulated"):
        importer.import_projects(pool, root, archive, [pid], 0)
    assert len(dal.list_projects()) == 1
    record = importer.results(pool, manifest["package_id"])[0]
    assert record["status"] == "error"
    assert not (root / record["project_id"]).exists()
    monkeypatch.setattr(Path, "rename", original_rename)
    assert importer.import_projects(pool, root, archive, [pid], 0)[0]["status"] == "done"


def test_registry_conflicts_preserve_defaults_and_require_fresh_preview():
    from wenyi_api.config_documents import config_document
    from wenyi_api.transfer.registry import merge_registry
    from wenyi_core.config import Config

    current = config_document(Config.from_dict({"llm": {"preset": "deepseek"}}))
    incoming = json.loads(json.dumps(current))
    incoming["llm"]["providers"]["default"]["base_url"] = "https://different.example/v1"
    merged, project, conflicts, missing = merge_registry(current, incoming, "abcdef123456")
    assert merged["llm"]["tiers"] == current["llm"]["tiers"]
    assert merged["llm"]["providers"]["default"] == current["llm"]["providers"]["default"]
    assert project["llm"]["tiers"]["strong"] != current["llm"]["tiers"]["strong"]
    assert "DEEPSEEK_API_KEY_IMPORT_ABCDEF12" in missing
    assert any(item["kind"] == "models" for item in conflicts)


def test_imported_partial_translation_resumes_without_rewriting_finished_text(native_env, tmp_path):
    from wenyi_api import dal, project_service
    from wenyi_api.job_service import start_job
    from wenyi_api.runtime import queue
    from wenyi_api.transfer.importer import import_projects
    from wenyi_api.workers import WorkerSettings

    pool, root, _ = native_env
    original, archive, _ = prepared_archive(native_env, tmp_path)
    pid = import_projects(pool, root, archive, [original], 0)[0]["project_id"]
    asyncio.run(start_job(pid, "translation"))
    asyncio.run(
        queue.run_one(
            queue.WORKFLOW_QUEUE, {fn.__name__: fn for fn in WorkerSettings.functions}, {}
        )
    )
    assert dal.get_project(pid)["status"] == "done"
    segments = project_service.storage_for(pid).load_chapter(0).segments
    assert segments[0].target == "译文"
    assert segments[1].index == 7
    assert segments[1].target is not None
    assert project_service.storage_for(original).load_chapter(0).segments[1].target is None


def test_abandoned_import_files_are_recovered_and_registry_changes_require_preview(
    native_env, tmp_path
):
    from wenyi_api.transfer import importer

    pool, root, _ = native_env
    pid, archive, manifest = prepared_archive(native_env, tmp_path)
    partial = "interruptedcopy"
    (root / partial).mkdir()
    (root / partial / "incomplete.txt").write_text("incomplete")
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO transfer_imports(package_id,source_id,project_id,package_sha256) VALUES('interrupted','source',%s,'hash')",
            (partial,),
        )
    importer.recover_imports(pool, root)
    assert not (root / partial).exists()
    assert importer.results(pool, "interrupted")[0]["status"] == "error"
    # Fail before publishing when a previously inspected registry has changed.
    with pytest.raises(importer.RegistryChanged):
        importer.import_projects(pool, root, archive, [pid], 99)
    assert importer.results(pool, manifest["package_id"])[0]["status"] == "error"
    assert importer.import_projects(pool, root, archive, [pid], 0)[0]["status"] == "done"


def test_windows_resource_names_and_structured_output_references():
    from wenyi_api.transfer.paths import file_mapping, remap_document

    mapping = file_mapping(
        ["source.txt", "exports/1/CON.txt", "source/A.png", "source/a.png"], "abcdef123456"
    )
    assert len({name.casefold() for name in mapping.values()}) == 4
    assert "CON.txt" not in mapping["exports/1/CON.txt"].split("/")
    value = remap_document(
        {
            "outputs": {"mono": "/data/old/exports/1/CON.txt"},
            "source_path": "/data/old/source.txt",
            "source": "/data/old/source.txt",
            "run_dir": "/data/old",
        },
        old_pid="old",
        new_pid="new",
        source_root="/data/old",
        files=mapping,
        runs={},
        exports={},
        jobs={},
    )
    assert value["outputs"]["mono"] == "new/" + mapping["exports/1/CON.txt"]
    assert value["source"] == "/data/old/source.txt"
    assert value["run_dir"] == "new"


def test_source_helper_runs_against_unmodified_official_api(native_env, tmp_path):
    from wenyi_api.transfer.archive import Archive

    pool, root, settings = native_env
    pid, _, _ = prepared_archive(native_env, tmp_path)
    repository = Path(__file__).resolve().parents[3]
    baseline = subprocess.run(
        ["git", "archive", "5f206257", "apps/api/wenyi_api"], cwd=repository, capture_output=True
    )
    if baseline.returncode:
        pytest.skip(
            "The official baseline commit is required for the source-image compatibility test"
        )
    vanilla = tmp_path / "official"
    with tarfile.open(fileobj=io.BytesIO(baseline.stdout)) as archive:
        for entry in archive:
            if entry.isfile():
                target = vanilla / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(entry).read())
    helper = tmp_path / "helper.pyz"
    subprocess.run(
        [sys.executable, str(repository / "scripts/build_transfer_helper.py"), str(helper)],
        check=True,
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(vanilla / "apps/api"),
        "DATABASE_URL": settings.psycopg_dsn,
        "DATA_DIR": str(root),
        "WENYI_CONFIG": settings.config_path,
    }
    listing = subprocess.run(
        [sys.executable, str(helper), "list", "--json"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(listing.stdout)[0]["id"] == pid
    output = tmp_path / "official.wenyi.zip"
    subprocess.run(
        [sys.executable, str(helper), "export", "--project", pid, "--output", str(output)],
        env=environment,
        capture_output=True,
        check=True,
    )
    with Archive(output) as archive:
        assert archive.document(pid)["tables"]["segments"][0]["target"] == "译文"
