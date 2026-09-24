"""Whole-book review history and guarded human translation edits."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from ..job_service import start_job
from ..project_service import (
    effective_config,
    project_write,
    require_book,
    require_project,
    storage_for,
)
from ..quality_presentation import candidate_details, quality_records
from ..review_presentation import review_items
from ..schemas import (
    ChapterSegments,
    JobEnqueued,
    QualityPage,
    QualityUnitDetail,
    ReviewRun,
    ReviewRunRequest,
    SegmentEdit,
    SegmentRevision,
)
from .chapters import chapter_payload

router = APIRouter(prefix="/projects/{pid}/review", tags=["review"])


def _review_run(storage, rid: str) -> dict:
    if not re.fullmatch(r"review-[A-Za-z0-9_-]+", rid):
        raise HTTPException(404, "review run not found")
    result = storage.read_artifact(f"reviews/{rid}/result.json")
    if not isinstance(result, dict):
        raise HTTPException(404, "review run not found")
    index = storage.read_artifact(f"reviews/{rid}/autofix/index.json")
    autofix = dict(result.get("autofix") or {})
    if isinstance(index, dict):
        autofix.update(index=index, records=index.get("records", []))
    return {
        "id": rid,
        "review_id": rid,
        "status": result.get("status", "running"),
        "created_at": result.get("started_at"),
        "issues": result.get("issues") or [],
        "changes": result.get("changes") or [],
        "autofix": autofix,
        "summary": result.get("summary") or {},
        "quality": storage.read_artifact(f"reviews/{rid}/quality/summary.json") or {},
        "result": result,
    }


@router.post("/run", response_model=JobEnqueued)
async def run_ai_review(pid: str, body: ReviewRunRequest | None = None) -> dict:
    require_book(require_project(pid))
    storage = storage_for(pid)
    if not storage.exists():
        raise HTTPException(409, "Prepare and translate the book before review")
    manifest = storage.load_manifest()
    if not manifest.get("chapters") or storage.pending_chapters():
        raise HTTPException(409, "Complete every chapter translation before whole-book review")
    params = {"autofix": body.autofix if body else None}
    if body is not None and body.quality_mode is not None:
        params["quality_mode"] = body.quality_mode
    return await start_job(pid, "review", params=params)


@router.get("/runs", response_model=list[ReviewRun])
def list_runs(pid: str) -> list[dict]:
    require_book(require_project(pid))
    storage = storage_for(pid)
    ids = {
        key.split("/")[1]
        for key in storage.list_artifacts("reviews/")
        if re.fullmatch(r"reviews/review-[A-Za-z0-9_-]+/result\.json", key)
    }
    return [_review_run(storage, rid) for rid in sorted(ids, reverse=True)]


@router.get("/runs/{rid}", response_model=ReviewRun)
def get_run(pid: str, rid: str) -> dict:
    require_book(require_project(pid))
    storage = storage_for(pid)
    with storage.state_lock():
        run = _review_run(storage, rid)
        run["items"] = review_items(storage, rid, run["result"], run["autofix"])
    return run


@router.get("/runs/{rid}/quality", response_model=QualityPage)
def get_quality(
    pid: str,
    rid: str,
    view: Literal["formal", "shadow"] = "formal",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = None,
    max_score: float | None = Query(None, ge=0, le=100),
    chapter: int | None = Query(None, ge=0),
    segment: int | None = Query(None, ge=0),
) -> dict:
    project = require_project(pid)
    require_book(project)
    storage = storage_for(pid)
    with storage.state_lock():
        _review_run(storage, rid)
        rows = quality_records(storage, rid, effective_config(project), view=view)
        if chapter is not None:
            rows = [row for row in rows if row.get("chapter_index") == chapter]
        if segment is not None:
            rows = [
                row
                for row in rows
                if any(member["segment_index"] == segment for member in row.get("members", []))
            ]
        if status:
            rows = [
                row
                for row in rows
                if status
                in {
                    row.get("status"),
                    row.get("publication_status"),
                    row.get("decision", {}).get("action"),
                    "stale" if row.get("stale") else None,
                }
            ]
        if max_score is not None:
            rows = [
                row
                for row in rows
                if row["minimum_score"] is not None and row["minimum_score"] <= max_score
            ]
        return {
            "items": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "text_scope": view,
            "summary": storage.read_artifact(f"reviews/{rid}/quality/summary.json") or {},
        }


@router.get("/runs/{rid}/quality/{uid}", response_model=QualityUnitDetail)
def get_quality_unit(
    pid: str, rid: str, uid: str, view: Literal["formal", "shadow"] = "formal"
) -> dict:
    project = require_project(pid)
    require_book(project)
    storage = storage_for(pid)
    with storage.state_lock():
        _review_run(storage, rid)
        row = next(
            (
                row
                for row in quality_records(storage, rid, effective_config(project), view=view)
                if row["unit_id"] == uid
            ),
            None,
        )
        if row is None:
            raise HTTPException(404, "quality unit not found")
        chapter = storage.load_chapter(row["chapter_index"])
        segments = {segment.index: segment for segment in chapter.segments}
        members = [
            segments[member["segment_index"]]
            for member in row["members"]
            if member["segment_index"] in segments
        ]
        baseline = storage.read_artifact(f"reviews/{rid}/quality/units/{uid}/baseline.json") or {}
        return {
            **row,
            "source_parts": [segment.source for segment in members],
            "current_target_parts": [segment.target for segment in members],
            "original_target_parts": baseline.get("target_parts", []),
            "candidates": candidate_details(storage, rid, uid),
        }


@router.get("/{ci}", response_model=ChapterSegments)
def get_chapter_for_review(pid: str, ci: int) -> dict:
    require_book(require_project(pid))
    return chapter_payload(storage_for(pid), ci)


@router.put("/{ci}/segments/{seg_idx}")
def edit_segment(pid: str, ci: int, seg_idx: int, body: SegmentEdit) -> dict:
    with project_write(pid) as (project, storage):
        require_book(project)
        try:
            chapter = storage.load_chapter(ci)
        except KeyError:
            raise HTTPException(404, "chapter not found") from None
        segment = next((item for item in chapter.segments if item.index == seg_idx), None)
        if segment is None:
            raise HTTPException(404, "segment not found")
        before = segment.target
        if before != body.expected_target:
            raise HTTPException(
                409, "This paragraph changed; reload its latest translation before saving"
            )
        if before == body.target:
            return {"ok": True, "index": seg_idx}
        segment.target = body.target
        chapter.meta.pop("review_passed", None)
        chapter.meta["review_invalidated_at"] = datetime.now(timezone.utc).isoformat()
        with storage.state_lock():
            storage.save_chapter(chapter, revision_kind="manual")
            storage.set_chapter_review_status(ci, "pending")
            storage.log_event(
                "manual_translation_edited",
                chapter=ci,
                index=seg_idx,
                before=before,
                after=body.target,
                history_recorded=True,
            )
    return {"ok": True, "index": seg_idx}


@router.get("/{ci}/segments/{seg_idx}/history", response_model=list[SegmentRevision])
def segment_history(pid: str, ci: int, seg_idx: int) -> list[dict]:
    require_book(require_project(pid))
    try:
        return storage_for(pid).load_segment_history(ci, seg_idx)
    except KeyError:
        raise HTTPException(404, "segment not found") from None


@router.post("/{ci}/complete")
def mark_reviewed(pid: str, ci: int) -> dict:
    with project_write(pid) as (project, storage):
        require_book(project)
        try:
            chapter = storage.load_chapter(ci)
        except KeyError:
            raise HTTPException(404, "chapter not found") from None
        if any(segment.target is None for segment in chapter.text_segments):
            raise HTTPException(409, "Translate every text segment before marking review complete")
        chapter.meta["review_passed"] = True
        chapter.meta["manual_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        with storage.state_lock():
            storage.save_chapter(chapter)
            storage.set_chapter_review_status(ci, "completed")
            storage.log_event("manual_review_completed", chapter=ci)
    return {"ok": True, "chapter": ci, "review_passed": True}
