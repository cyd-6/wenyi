"""Backend-neutral translation candidate records and read-only comparisons."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, StrictStr, model_validator

from .llm.choice import ChoiceResult
from .storage.protocol import Storage

CandidateId = Literal["A", "B", "C"]
CandidateStatus = Literal["generating", "polishing", "judging", "selected", "published"]


class CandidateView(BaseModel):
    id: CandidateId
    raw_targets: list[StrictStr] | None = None
    targets: list[StrictStr] | None = None
    polish_status: Literal["pending", "complete", "skipped"] = "pending"
    duplicate_of: CandidateId | None = None
    generation: int = 0


class CandidateState(CandidateView):
    turn: list[dict[str, str]] | None = None
    translated_indices: list[int] | None = None
    alternative: str = ""
    polish_fingerprint: str | None = None


class CandidateComparison(BaseModel):
    batch_id: str
    chapter: int
    segment_indices: list[int]
    sources: list[str]
    status: CandidateStatus
    candidates: list[CandidateView]
    decision: ChoiceResult | None = None
    extra_generations: int = 0


class CandidateRecord(CandidateComparison):
    version: Literal[1] = 1
    source_sha256: str
    source_lang: str
    target_lang: str
    candidates: list[CandidateState]
    judge_fingerprint: str | None = None

    @model_validator(mode="after")
    def aligned(self) -> CandidateRecord:
        count = len(self.sources)
        if len(self.segment_indices) != count or [c.id for c in self.candidates] != ["A", "B", "C"]:
            raise ValueError("Invalid candidate batch identity")
        for candidate in self.candidates:
            for targets in (candidate.raw_targets, candidate.targets):
                if targets is not None and len(targets) != count:
                    raise ValueError("Candidate paragraph count mismatch")
        if self.decision is not None and self.decision.choice not in {"A", "B", "C"}:
            raise ValueError("Invalid selected candidate")
        return self

    def comparison(self) -> CandidateComparison:
        return CandidateComparison.model_validate(self.model_dump())


def candidate_index_key(chapter: int) -> str:
    return f"translation-candidates/{chapter}/index.json"


def candidate_record_key(chapter: int, batch_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", batch_id):
        raise ValueError("Invalid candidate batch ID")
    return f"translation-candidates/{chapter}/{batch_id}.json"


def load_candidate_comparison(
    store: Storage, chapter: int, segment: int
) -> CandidateComparison | None:
    """Read one indexed batch and reject records from another source or language."""
    with store.state_lock():
        contents = store.load_chapter(chapter)
        current = {item.index: item.source for item in contents.segments}
        if segment not in current:
            raise KeyError(segment)
        manifest = store.load_manifest()
        index = store.read_artifact(candidate_index_key(chapter)) or {}
        batch_id = index.get(str(segment))
        if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{64}", batch_id):
            return None
        raw = store.read_artifact(candidate_record_key(chapter, batch_id))
        if raw is None:
            return None
        record = CandidateRecord.model_validate(raw)
        if (
            record.batch_id != batch_id
            or record.chapter != chapter
            or segment not in record.segment_indices
            or record.source_sha256 != manifest.get("source_sha256", "")
            or record.target_lang != manifest.get("target_lang", "zh")
            or any(
                current.get(i) != text for i, text in zip(record.segment_indices, record.sources)
            )
        ):
            return None
        return record.comparison()


def recover_candidate_publications(store: Storage, chapter: int) -> None:
    """Repair a crash between saving formal targets and marking their candidate published."""
    with store.state_lock():
        contents = store.load_chapter(chapter)
        segments = {segment.index: segment for segment in contents.segments}
        manifest = store.load_manifest()
        index = store.read_artifact(candidate_index_key(chapter)) or {}
        for batch_id in sorted(set(index.values())):
            key = candidate_record_key(chapter, batch_id)
            raw = store.read_artifact(key)
            if not raw or raw.get("status") != "selected":
                continue
            record = CandidateRecord.model_validate(raw)
            if record.source_sha256 != manifest.get("source_sha256", "") or not record.decision:
                continue
            winner = next(c for c in record.candidates if c.id == record.decision.choice)
            if winner.targets is not None and all(
                index in segments
                and segments[index].source == source
                and segments[index].target == target
                for index, source, target in zip(
                    record.segment_indices, record.sources, winner.targets
                )
            ):
                record.status = "published"
                store.write_artifact(key, record.model_dump(mode="json"))
