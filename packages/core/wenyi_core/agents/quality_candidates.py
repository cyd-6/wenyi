"""Bounded content generation protocols for complete logical paragraphs."""

from __future__ import annotations

import json
from typing import Any

from ..quality.models import QualityContext, QualityUnit
from .quality_judge import rubric


def candidate_messages(
    unit: QualityUnit,
    context: QualityContext,
    strategy: str,
    *,
    constraints: list[str] | None = None,
    version: str = "quality-v1",
) -> list[dict[str, str]]:
    """Independent retranslation excludes the incumbent, earlier scores and diagnostic bias."""
    state = dict(context.state)
    state["members"] = unit.members
    if strategy == "retranslate":
        state.pop("target_parts", None)
    elif constraints:
        state["verified_constraints"] = constraints
    return [
        {"role": "system", "content": rubric(version)[strategy]},
        {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
    ]


def validate_candidate(value: Any, unit: QualityUnit) -> list[str]:
    """Reject partial arrays, identity drift and empty content rather than repairing forever."""
    if not isinstance(value, dict) or set(value) != {"members", "targets"}:
        raise ValueError("Quality candidate must contain exactly members and targets")
    if value["members"] != unit.members:
        raise ValueError("Quality candidate changed member identities")
    targets = value["targets"]
    if not isinstance(targets, list) or len(targets) != len(unit.target_parts):
        raise ValueError("Quality candidate must contain one target per original member")
    if any(not isinstance(target, str) or not target.strip() for target in targets):
        raise ValueError("Quality candidate targets must be nonempty strings")
    return targets


def validate_diagnosis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"status", "constraints"}:
        raise ValueError("Invalid quality verification response")
    if value["status"] not in {"confirmed_issue", "no_confirmed_issue", "insufficient_evidence"}:
        raise ValueError("Invalid quality verification status")
    if not isinstance(value["constraints"], list) or any(
        not isinstance(constraint, str) for constraint in value["constraints"]
    ):
        raise ValueError("Invalid quality verification constraints")
    return value
