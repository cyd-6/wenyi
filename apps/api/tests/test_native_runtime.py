"""Real PostgreSQL tests for native queue ownership, recovery and portable state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool


@pytest.fixture
def native_env(tmp_path, monkeypatch):
    from wenyi_api import config
    from wenyi_api.db import pool as db
    from wenyi_core.llm import factory
    from wenyi_core.llm.providers.fake import FakeClient

    def response(messages, tier, json_mode):
        cues = re.findall(r'"(\d+)"\s*:', messages[-1]["content"])
        if cues:
            return json.dumps({cue: f"字幕 {cue}" for cue in cues})
        count = len(re.findall(r"^\[\d+\]", messages[-1]["content"], re.MULTILINE))
        return (
            json.dumps(
                {
                    "translations": [f"译文 {i}" for i in range(count)],
                    "titles": [f"标题 {i}" for i in range(count)],
                }
            )
            if count
            else ("[]" if json_mode else "译文")
        )

    monkeypatch.setattr(factory, "build_client", lambda _: FakeClient(response))

    base = os.environ.get("WENYI_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Set WENYI_TEST_DATABASE_URL for native runtime integration tests")
    schema = "native_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_conninfo(base, options=f"-c search_path={schema},public")
    root = tmp_path / "项目 files"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "language:\n  source: en\n  target: zh\nllm:\n  preset: fake\npipeline:\n  book_understanding: false\n  polish: false\n  review: false\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        previous_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    original = config.settings
    value = replace(
        original,
        database_url=dsn,
        data_dir=str(root),
        config_path=str(config_path),
        runtime_backend="postgres",
    )
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("wenyi_api") and isinstance(
            getattr(module, "settings", None), config.Settings
        ):
            monkeypatch.setattr(module, "settings", value)
    pool = ConnectionPool(dsn, min_size=1, max_size=16, open=True)
    pool.wait()
    with pool.connection() as conn:
        conn.execute((Path(__file__).parents[1] / "wenyi_api/db/schema.sql").read_text())
    monkeypatch.setattr(db, "_pool", pool)
    try:
        yield pool, root, value
    finally:
        pool.close()
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(previous_policy)
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def source_project(
    root, *, name="Migration test", text="Chapter One\n\nA quiet morning.", fmt="text"
):
    from wenyi_api import dal
    from wenyi_core.pipeline.runstore import source_sha256

    pid = uuid4().hex[:16]
    directory = root / pid
    directory.mkdir()
    path = directory / ("source.srt" if fmt == "srt" else "source.txt")
    path.write_text(text, encoding="utf-8")
    dal.create_project(
        name,
        "en",
        "zh",
        {"template": "快速出稿"},
        project_id=pid,
        source={
            "source_path": str(path),
            "source_sha256": source_sha256(str(path)),
            "fmt": fmt,
            "source_meta": {"original_filename": path.name},
        },
    )
    return pid, path


def test_queue_executes_real_parse_and_keeps_source_portable(native_env):
    from wenyi_api import dal
    from wenyi_api.job_service import start_job
    from wenyi_api.runtime import queue
    from wenyi_api.workers import WorkerSettings

    pool, root, _ = native_env
    pid, source = source_project(root)
    job = asyncio.run(start_job(pid, "parse"))
    assert asyncio.run(
        queue.run_one(
            queue.WORKFLOW_QUEUE, {fn.__name__: fn for fn in WorkerSettings.functions}, {}
        )
    )
    assert dal.get_project(pid)["status"] == "uploaded"
    assert dal.get_project(pid)["source_path"] == f"{pid}/{source.name}"
    assert dal.get_job_by_arq_id(job["job_id"])["status"] == "done"
    assert not queue.is_active(job["job_id"])
    assert not asyncio.run(queue.run_one(queue.WORKFLOW_QUEUE, {}, {}))


def test_duplicate_enqueue_and_two_claimants_execute_once(native_env):
    from wenyi_api.runtime import queue

    seen = []
    assert queue.enqueue("run_parse", _job_id="one", project_id="p", run_id="one")
    assert queue.enqueue("run_parse", _job_id="one", project_id="p", run_id="one") is None

    async def function(ctx, **kwargs):
        seen.append(kwargs)
        await asyncio.sleep(0.1)

    async def run():
        return await asyncio.gather(
            *(queue.run_one(queue.WORKFLOW_QUEUE, {"run_parse": function}, {}) for _ in range(2))
        )

    assert sorted(asyncio.run(run())) == [False, True]
    assert seen == [{"project_id": "p", "run_id": "one"}]


def test_long_task_lock_prevents_false_recovery_and_crash_is_interrupted(native_env):
    from wenyi_api import dal
    from wenyi_api.runtime import queue
    from wenyi_api.workers.recovery import recover_jobs

    pool, root, _ = native_env
    pid, _ = source_project(root)
    jid = dal.create_job(pid, "translation", "lost", run_id="lost")
    dal.set_project_status(pid, "translating")
    queue.enqueue("run_translation", _job_id="lost", project_id=pid, run_id="lost")
    with pool.connection() as conn:
        conn.execute("UPDATE runtime_queue SET status='running' WHERE id='lost'")
    with psycopg.connect(native_env[2].psycopg_dsn, autocommit=True) as owner:
        owner.execute("SELECT pg_advisory_lock(hashtextextended('wenyi:dispatch:lost',0))")
        asyncio.run(queue.recover_dispatches())
        assert queue.is_active("lost")
    asyncio.run(queue.recover_dispatches())
    assert not queue.is_active("lost")
    asyncio.run(recover_jobs({"backend": "postgres", "recover_immediately": True}))
    assert dal.get_job(jid)["status"] == "interrupted"
    assert dal.get_project(pid)["status"] == "paused"


def test_progress_large_payload_and_reconnect_snapshot(native_env):
    from wenyi_api.runtime.progress import latest, publish, subscribe

    pid, _ = source_project(native_env[1])
    payload = {"project_id": pid, "run_id": "new-run", "kind": "translation", "label": "中" * 10000}
    publish(payload)
    assert latest(pid) == payload

    async def run():
        stream = subscribe(pid)
        try:
            import json

            assert json.loads(await asyncio.wait_for(anext(stream), 3)) == payload
        finally:
            await stream.aclose()

    asyncio.run(run())


def test_moved_project_can_still_load_manifest_and_export(native_env, tmp_path, monkeypatch):
    from wenyi_api import project_service
    from wenyi_api.job_service import start_job
    from wenyi_api.runtime import queue
    from wenyi_api.storage_pg import PostgresStorage
    from wenyi_api.workers import WorkerSettings, tasks

    _, root, settings = native_env
    pid, _ = source_project(root)
    asyncio.run(start_job(pid, "translation"))
    asyncio.run(
        queue.run_one(
            queue.WORKFLOW_QUEUE, {fn.__name__: fn for fn in WorkerSettings.functions}, {}
        )
    )
    moved = tmp_path / "moved 中文"
    shutil.move(root, moved)
    updated = replace(settings, data_dir=str(moved))
    for module in list(sys.modules.values()):
        if (
            getattr(module, "__name__", "").startswith("wenyi_api")
            and getattr(module, "settings", None) is settings
        ):
            monkeypatch.setattr(module, "settings", updated)
    storage = PostgresStorage(pid, native_env[0], run_dir=str(moved / pid))
    assert Path(storage.load_manifest()["source_path"]).is_file()
    assert Path(tasks._resolve_source(pid)).is_file()
    assert project_service.storage_for(pid).load_chapter(0).segments[0].target is not None


def test_subtitles_use_durable_queue_and_portable_state(native_env):
    from wenyi_api import dal, project_service
    from wenyi_api.job_service import start_job
    from wenyi_api.routers.export import enqueue_export
    from wenyi_api.runtime import queue
    from wenyi_api.schemas import ExportRequest
    from wenyi_api.workers import ExportWorkerSettings, WorkerSettings

    pool, root, _ = native_env
    pid, _ = source_project(
        root,
        fmt="srt",
        text="1\n00:00:01,000 --> 00:00:03,000\nHello\n\n2\n00:00:04,000 --> 00:00:06,000\nWorld\n",
    )
    asyncio.run(start_job(pid, "srt"))
    asyncio.run(
        queue.run_one(
            queue.WORKFLOW_QUEUE, {fn.__name__: fn for fn in WorkerSettings.functions}, {}
        )
    )
    assert dal.get_project(pid)["status"] == "done"
    store = project_service.storage_for(pid)
    assert store.read_artifact("srt/cues.jsonl")["1"]["target"] == "字幕 1"
    with pool.connection() as conn:
        manifest = conn.execute(
            "SELECT value FROM artifacts WHERE project_id=%s AND key='srt/manifest.json'", (pid,)
        ).fetchone()[0]
    assert manifest["source_path"] == f"{pid}/source.srt"
    job = asyncio.run(enqueue_export(pid, ExportRequest(format="srt", bilingual=True)))
    asyncio.run(
        queue.run_one(
            queue.EXPORT_QUEUE, {fn.__name__: fn for fn in ExportWorkerSettings.functions}, {}
        )
    )
    with pool.connection() as conn:
        path, status = conn.execute(
            "SELECT path,status FROM exports WHERE id=%s", (job["export_id"],)
        ).fetchone()
    assert status == "done"
    assert "00:00:01,000 --> 00:00:03,000" in (root / path).read_text(encoding="utf-8")


def test_cancelled_dispatch_waits_for_saved_checkpoint_and_releases_lock(native_env, monkeypatch):
    from wenyi_api import dal, project_service
    from wenyi_api.job_service import start_job
    from wenyi_api.runtime import queue
    from wenyi_api.workers import tasks

    pool, root, _ = native_env
    pid, _ = source_project(root)
    started = threading.Event()

    def long_operation(kind, project_id, storage, config, client, progress, params):
        storage.write_artifact("checkpoint.json", {"saved": True})
        started.set()
        for _ in range(100):
            time.sleep(0.01)
            progress(1, 10, "Checkpoint saved")
        raise AssertionError("Task was never cancelled")

    monkeypatch.setattr(tasks, "_book_operation", long_operation)
    job = asyncio.run(start_job(pid, "translation"))

    async def cancel():
        task = asyncio.create_task(
            queue.run_one(queue.WORKFLOW_QUEUE, {"run_translation": tasks.run_translation}, {})
        )
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert dal.get_project(pid)["status"] == "paused"
    assert project_service.storage_for(pid).read_artifact("checkpoint.json")["saved"]
    assert not queue.is_active(job["job_id"])
    with project_service.storage_for(pid).lock(blocking=False):
        pass


def test_worker_shutdown_interrupts_pending_dispatches_instead_of_replaying_them(
    native_env, monkeypatch
):
    from wenyi_api import dal
    from wenyi_api.job_service import start_job
    from wenyi_api.runtime import queue
    from wenyi_api.workers import WorkerSettings

    pool, root, _ = native_env
    ids = [source_project(root)[0] for _ in range(2)]
    for pid in ids:
        asyncio.run(start_job(pid, "parse"))
    monkeypatch.setattr(queue, "init_pool", lambda _: pool)
    monkeypatch.setattr(queue, "close_pool", lambda: None)

    async def exercise():
        started, stop = asyncio.Event(), asyncio.Event()

        async def run_parse(ctx, **kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(WorkerSettings, "functions", [run_parse])
        worker = asyncio.create_task(queue.serve(queue.WORKFLOW_QUEUE, stop=stop))
        await asyncio.wait_for(started.wait(), 5)
        stop.set()
        await asyncio.wait_for(worker, 5)

    asyncio.run(exercise())
    assert [dal.get_project(pid)["status"] for pid in ids] == ["paused", "paused"]
    with pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM runtime_queue WHERE status='interrupted'"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT count(*) FROM runtime_workers").fetchone()[0] == 0
