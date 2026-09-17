"""Durable PostgreSQL dispatch using session locks, not expiring job leases.

The queue stores the *actual* dispatch arguments, including export options. Domain
jobs remain the authority for workflow state and own their existing project locks.
No database transaction is held open while running a model request or rendering.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..config import settings
from ..db import close_pool, get_pool, init_pool

log = logging.getLogger(__name__)
WORKFLOW_QUEUE = "wenyi:workflows"
EXPORT_QUEUE = "wenyi:exports"


@dataclass(frozen=True)
class QueuedJob:
    job_id: str


def enqueue(name: str, *, _job_id: str | None = None, **kwargs) -> QueuedJob | None:
    from ..workers import ExportWorkerSettings, WorkerSettings

    if os.environ.get("WENYI_STOP_FILE") and Path(os.environ["WENYI_STOP_FILE"]).exists():
        raise RuntimeError("Wenyi is shutting down; restart before submitting work")
    allowed = {fn.__name__ for fn in WorkerSettings.functions + ExportWorkerSettings.functions}
    if name not in allowed:
        raise ValueError(f"Unknown task function: {name}")
    job_id = _job_id or uuid4().hex
    with get_pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO runtime_queue(id,queue,function,kwargs) VALUES(%s,%s,%s,%s)
               ON CONFLICT(id) DO NOTHING RETURNING id""",
            (job_id, EXPORT_QUEUE if name == "run_export" else WORKFLOW_QUEUE, name, Jsonb(kwargs)),
        ).fetchone()
    return QueuedJob(job_id) if row else None


def is_active(job_id: str) -> bool:
    with get_pool().connection() as conn:
        row = conn.execute("SELECT status FROM runtime_queue WHERE id=%s", (job_id,)).fetchone()
    return bool(row and row[0] in {"queued", "running"})


async def recover_dispatches() -> None:
    """An abandoned dispatch is interrupted; never replay a model call automatically."""
    async with await psycopg.AsyncConnection.connect(settings.psycopg_dsn, autocommit=True) as conn:
        rows = await (
            await conn.execute("SELECT id FROM runtime_queue WHERE status='running'")
        ).fetchall()
        for (job_id,) in rows:
            key = f"wenyi:dispatch:{job_id}"
            acquired = await (
                await conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,))
            ).fetchone()
            if acquired and acquired[0]:
                try:
                    await conn.execute(
                        """UPDATE runtime_queue SET status='interrupted',updated_at=now()
                           WHERE id=%s AND status='running'""",
                        (job_id,),
                    )
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))


async def run_one(queue: str, functions: dict, ctx: dict) -> bool:
    """Claim one dispatch. Its dedicated connection owns the lock until completion."""
    async with await psycopg.AsyncConnection.connect(
        settings.psycopg_dsn, autocommit=True, row_factory=dict_row
    ) as conn:
        job = None
        async with conn.transaction():
            job = await (
                await conn.execute(
                    """SELECT * FROM runtime_queue WHERE queue=%s AND status='queued'
                   ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (queue,),
                )
            ).fetchone()
            if not job:
                return False
            key = f"wenyi:dispatch:{job['id']}"
            acquired = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (key,)
                )
            ).fetchone()
            if not acquired or not acquired["acquired"]:
                return False
            await conn.execute(
                "UPDATE runtime_queue SET status='running',updated_at=now() WHERE id=%s",
                (job["id"],),
            )

        async def watch_connection(task: asyncio.Task) -> None:
            try:
                while not task.done():
                    await asyncio.sleep(5)
                    await conn.execute("SELECT 1")
            except psycopg.Error:
                # A lost lock must cancel the domain operation as well.
                task.cancel()

        status, error = "done", None
        task = None
        watcher = None
        try:
            function = functions[job["function"]]
            task = asyncio.create_task(function(ctx, **job["kwargs"]))
            watcher = asyncio.create_task(watch_connection(task))
            await task
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as exc:
            status, error = "error", str(exc)
            log.exception("Native task %s failed", job["id"])
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if not conn.closed:
                await conn.execute(
                    "UPDATE runtime_queue SET status=%s,error=%s,updated_at=now() WHERE id=%s",
                    (status, error, job["id"]),
                )
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
    return True


async def serve(queue: str, *, stop: asyncio.Event | None = None) -> None:
    """Run a single native worker process with workflow/export concurrency parity."""
    from ..workers import ExportWorkerSettings, WorkerSettings
    from ..workers.recovery import recover_jobs

    if queue not in {WORKFLOW_QUEUE, EXPORT_QUEUE}:
        raise ValueError("Unknown native worker queue")
    init_pool(settings.psycopg_dsn)
    stop = stop or asyncio.Event()
    worker = ExportWorkerSettings if queue == EXPORT_QUEUE else WorkerSettings
    functions = {fn.__name__: fn for fn in worker.functions}
    instance = uuid4().hex
    ctx = {"backend": "postgres"}
    stop_file = os.environ.get("WENYI_STOP_FILE")
    tasks = []
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.psycopg_dsn, autocommit=True
        ) as owner:
            acquired = await (
                await owner.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                    (f"wenyi:worker:{queue}",),
                )
            ).fetchone()
            if not acquired or not acquired[0]:
                raise RuntimeError(f"Another worker already owns {queue}")
            await recover_dispatches()
            await recover_jobs({**ctx, "recover_immediately": True})

            async def slot() -> None:
                while not stop.is_set():
                    if not await run_one(queue, functions, ctx):
                        await asyncio.sleep(0.25)

            try:
                tasks = [asyncio.create_task(slot()) for _ in range(worker.max_jobs)]
                ticks = 0
                while not stop.is_set():
                    if stop_file and Path(stop_file).exists():
                        break
                    for task in tasks:
                        if task.done():
                            task.result()
                            raise RuntimeError("Native worker slot stopped unexpectedly")
                    if ticks % 5 == 0:
                        await owner.execute(
                            """INSERT INTO runtime_workers(queue,instance_id) VALUES(%s,%s)
                               ON CONFLICT(queue) DO UPDATE SET instance_id=EXCLUDED.instance_id,
                               heartbeat=now()""",
                            (queue, instance),
                        )
                    if ticks % 30 == 0:
                        await recover_dispatches()
                        await recover_jobs(ctx)
                        await owner.execute(
                            "DELETE FROM runtime_progress WHERE updated_at < now() - interval '7 days'"
                        )
                    ticks += 1
                    await asyncio.sleep(1)
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # Stop pending work as well: exiting never starts fresh model requests.
                with get_pool().connection() as conn:
                    conn.execute(
                        "UPDATE runtime_queue SET status='interrupted',updated_at=now() "
                        "WHERE queue=%s AND status='queued'",
                        (queue,),
                    )
                await recover_dispatches()
                await recover_jobs({**ctx, "recover_immediately": True})
    finally:
        try:
            with get_pool().connection() as conn:
                conn.execute(
                    "DELETE FROM runtime_workers WHERE queue=%s AND instance_id=%s",
                    (queue, instance),
                )
        finally:
            close_pool()
