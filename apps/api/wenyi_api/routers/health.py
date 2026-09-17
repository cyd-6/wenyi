"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..db import get_pool

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    try:
        with get_pool().connection() as c:
            c.execute("SELECT 1")
        db = "ok"
    except Exception as e:  # noqa: BLE001
        db = f"error: {e}"
    result = {"status": "ok" if db == "ok" else "error", "db": db}
    if settings.runtime_backend == "postgres" and db == "ok":
        with get_pool().connection() as c:
            rows = c.execute(
                "SELECT queue FROM runtime_workers WHERE heartbeat > now() - interval '15 seconds'"
            ).fetchall()
        ready = {row[0] for row in rows}
        result["workers"] = sorted(ready)
        if not {"wenyi:workflows", "wenyi:exports"}.issubset(ready):
            result["status"] = "starting"
    return result
