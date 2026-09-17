"""PostgreSQL progress snapshots and notifications; payloads match Redis Pub/Sub."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import psycopg
from psycopg.types.json import Jsonb

from ..config import settings
from ..db import get_pool

log = logging.getLogger(__name__)


def publish(payload: dict) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                """INSERT INTO runtime_progress(project_id,payload) VALUES(%s,%s)
                   ON CONFLICT(project_id) DO UPDATE SET payload=EXCLUDED.payload,
                   revision=runtime_progress.revision+1,updated_at=now()""",
                (payload["project_id"], Jsonb(payload)),
            )
            # NOTIFY has a small payload limit; send identity, not model progress text.
            conn.execute("SELECT pg_notify('wenyi_progress',%s)", (payload["project_id"],))
    except Exception:
        log.warning("Could not publish native progress", exc_info=True)


def latest(project_id: str) -> dict | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            """SELECT payload FROM runtime_progress WHERE project_id=%s
               AND updated_at > now() - interval '7 days'""",
            (project_id,),
        ).fetchone()
    return row[0] if row else None


async def subscribe(project_id: str) -> AsyncIterator[str]:
    # A dedicated autocommit listener does not consume a domain connection-pool slot.
    async with await psycopg.AsyncConnection.connect(settings.psycopg_dsn, autocommit=True) as conn:
        await conn.execute("LISTEN wenyi_progress")
        last = None
        while True:
            payload = await asyncio.to_thread(latest, project_id)
            encoded = json.dumps(payload, ensure_ascii=False) if payload else None
            if encoded and encoded != last:
                last = encoded
                yield encoded
            # A periodic read also heals notifications missed during reconnects.
            async for notification in conn.notifies(timeout=1, stop_after=1):
                if notification.payload == project_id:
                    break
