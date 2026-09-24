"""Pure immutable quality records and strictly validated experimental configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DIMENSIONS = ("adequacy", "coverage", "terminology", "reference", "fluency", "voice")
CRITICAL_DIMENSIONS = DIMENSIONS[:4]


def digest(value: Any) -> str:
    """Hash canonical data, preserving part boundaries and empty versus missing targets."""
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class QualitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class QualityContextConfig(QualitySettings):
    preceding_units: int = Field(default=2, ge=0, le=20)
    following_units: int = Field(default=1, ge=0, le=20)
    max_estimated_input_tokens: int = Field(default=6000, ge=256, le=30000)


class QualityThresholds(QualitySettings):
    adequacy_min: float = Field(default=75, ge=0, le=100)
    coverage_min: float = Field(default=75, ge=0, le=100)
    terminology_min: float = Field(default=75, ge=0, le=100)
    reference_min: float = Field(default=75, ge=0, le=100)
    fluency_min: float = Field(default=70, ge=0, le=100)
    voice_min: float = Field(default=65, ge=0, le=100)
    min_confidence_to_act: float = Field(default=0.70, ge=0, le=1)
    min_pairwise_support: float = Field(default=0.75, ge=0, le=1)
    min_dimension_improvement: float = Field(default=5, gt=0, le=100)
    severe_tail_probability: float = Field(default=0.20, ge=0, le=1)


class QualityComparison(QualitySettings):
    swap_order: bool = True
    keep_original_on_tie: Literal[True] = True


class QualityAudit(QualitySettings):
    sample_rate: float = Field(default=0.05, ge=0, le=1)
    seed: int = 42


class QualityConfig(QualitySettings):
    mode: Literal["off", "observe", "optimize"] = "off"
    rubric_version: Literal["quality-v1"] = "quality-v1"
    max_candidates_per_unit: int = Field(default=2, ge=0, le=2)
    max_context_expansions: int = Field(default=1, ge=0, le=2)
    max_verifications_per_unit: int = Field(default=1, ge=0, le=2)
    max_units_to_optimize_per_run: int = Field(default=100, ge=0)
    max_generation_requests_per_run: int = Field(default=300, ge=0)
    max_judge_requests_per_run: int = Field(default=10000, ge=0)
    context: QualityContextConfig = Field(default_factory=QualityContextConfig)
    thresholds: QualityThresholds = Field(default_factory=QualityThresholds)
    comparison: QualityComparison = Field(default_factory=QualityComparison)
    audit: QualityAudit = Field(default_factory=QualityAudit)
    on_error: Literal["continue_review", "stop"] = "continue_review"


@dataclass(frozen=True)
class QualityUnit:
    unit_id: str
    chapter_index: int
    text_indices: tuple[int, ...]
    segment_indices: tuple[int, ...]
    segment_refs: tuple[str, ...]
    kind: str
    source_parts: tuple[str, ...]
    target_parts: tuple[str | None, ...]
    source_hash: str
    target_hash: str
    status: str = "pending"
    reason_codes: tuple[str, ...] = ()

    @property
    def members(self) -> list[dict[str, int]]:
        return [
            {"text_index": ti, "segment_index": si}
            for ti, si in zip(self.text_indices, self.segment_indices)
        ]

    def identity(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "chapter_index": self.chapter_index,
            "members": self.members,
            "segment_refs": list(self.segment_refs),
            "source_hash": self.source_hash,
            "target_hash": self.target_hash,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class QualityContext:
    state: dict[str, Any]
    context_hash: str
    status: str
    estimated_tokens: int
    omitted: tuple[str, ...] = ()
