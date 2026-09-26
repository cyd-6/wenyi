"""Chapter queue routing delegates durable identity and rollback to start_job."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from wenyi_api.routers import chapters


def test_translate_chapter_enqueues_only_requested_chapter(monkeypatch):
    enqueued = []
    monkeypatch.setattr(chapters, "require_project", lambda pid: {"id": pid, "fmt": "text"})
    monkeypatch.setattr(
        chapters.dal, "chapter_summaries", lambda pid: [{"index": 2, "status": "pending"}]
    )

    async def start(pid, kind, *, params):
        enqueued.append((pid, kind, params))
        return {"job_id": "chapter-job", "project_id": pid, "kind": kind}

    monkeypatch.setattr(chapters, "start_job", start)
    result = asyncio.run(chapters.translate_chapter("project-1", 2))
    assert enqueued == [("project-1", "chapter_translation", {"chapter_index": 2})]
    assert result["kind"] == "chapter_translation"


@pytest.mark.parametrize("entries,code", [([], 404), ([{"index": 0, "status": "done"}], 409)])
def test_translate_chapter_rejects_missing_or_completed_chapter(monkeypatch, entries, code):
    monkeypatch.setattr(chapters, "require_project", lambda pid: {"id": pid, "fmt": "text"})
    monkeypatch.setattr(chapters.dal, "chapter_summaries", lambda pid: entries)
    with pytest.raises(HTTPException) as error:
        asyncio.run(chapters.translate_chapter("project-1", 0))
    assert error.value.status_code == code


def test_translate_chapter_propagates_queue_failure(monkeypatch):
    monkeypatch.setattr(chapters, "require_project", lambda pid: {"id": pid, "fmt": "text"})
    monkeypatch.setattr(
        chapters.dal, "chapter_summaries", lambda pid: [{"index": 1, "status": "pending"}]
    )

    async def fail(*args, **kwargs):
        raise HTTPException(503, "queue unavailable")

    monkeypatch.setattr(chapters, "start_job", fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(chapters.translate_chapter("project-1", 1))
    assert error.value.status_code == 503


def test_candidates_are_read_only_and_source_bound(monkeypatch, tmp_path):
    from tests.test_translation_candidates import (
        CandidateClient,
        candidate_config,
        candidate_plan,
        candidate_store,
        execute,
    )

    config = candidate_config(polish=False)
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    monkeypatch.setattr(chapters, "require_project", lambda pid: {"id": pid, "fmt": "text"})
    monkeypatch.setattr(chapters, "storage_for", lambda pid: store)
    assert chapters.get_candidates("project-1", 0, 10) is None
    execute(config, CandidateClient(config), store, plan)
    comparison = chapters.get_candidates("project-1", 0, 10)
    assert comparison.decision.choice == "B"
    assert all(segment.target is None for segment in store.load_chapter(0).segments)
    assert "turn" not in comparison.model_dump()["candidates"][0]
    with pytest.raises(HTTPException) as error:
        chapters.get_candidates("project-1", 0, 999)
    assert error.value.status_code == 404
