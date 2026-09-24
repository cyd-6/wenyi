"""Deterministic risk routing; scores and model confidence are uncalibrated signals."""

from __future__ import annotations

from typing import Any

from .models import CRITICAL_DIMENSIONS, QualityConfig, digest


def severe_tail(dimension: dict[str, Any]) -> float:
    probabilities = dimension.get("probabilities") or {}
    return sum(float(probabilities.get(str(i), 0)) for i in (0, 1))


def route_score(row: dict[str, Any], config: QualityConfig) -> dict[str, Any]:
    """Prioritize incomplete evidence, then critical risk, then expression issues."""
    result = {
        "action": "keep",
        "reason_codes": [],
        "selected_candidate_id": None,
        "calibration_status": "uncalibrated",
    }
    if row["status"] not in {"scored", "needs_review"} or not row.get("dimensions"):
        result.update(
            action="needs_review" if row["status"] != "not_applicable" else "not_applicable",
            reason_codes=row.get("reason_codes", []),
        )
        return result
    applicable = {
        key: value for key, value in row["dimensions"].items() if value.get("status") == "scored"
    }
    if any(
        value["confidence"] < config.thresholds.min_confidence_to_act
        for value in applicable.values()
    ):
        result.update(action="needs_review", reason_codes=["uncertain_score"])
        return result
    risks = [
        key
        for key, value in applicable.items()
        if value["normalized"] < getattr(config.thresholds, key + "_min")
        or (
            key in CRITICAL_DIMENSIONS
            and severe_tail(value) >= config.thresholds.severe_tail_probability
        )
    ]
    if any(key in CRITICAL_DIMENSIONS for key in risks):
        result.update(action="retranslate", reason_codes=[f"{key}_risk" for key in risks])
    elif risks:
        result.update(action="revise", reason_codes=[f"{key}_risk" for key in risks])
    return result


def priority(row: dict[str, Any]) -> tuple[float, float, int, int]:
    """Rank all units after scoring, preserving original source order for equal risk."""
    critical = [
        value
        for key, value in row.get("dimensions", {}).items()
        if key in CRITICAL_DIMENSIONS and value.get("status") == "scored"
    ]
    return (
        -max((severe_tail(value) for value in critical), default=0),
        min((value["normalized"] for value in critical), default=100),
        row["chapter_index"],
        row["members"][0]["text_index"],
    )


def audited(unit_id: str, config: QualityConfig) -> bool:
    return int(digest([config.audit.seed, unit_id])[:16], 16) / 2**64 < config.audit.sample_rate


def critical_regression(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Reject any measured critical decline; expression improvements cannot offset it."""
    for name in CRITICAL_DIMENSIONS:
        a, b = before.get("dimensions", {}).get(name, {}), after.get("dimensions", {}).get(name, {})
        if a.get("status") != "scored":
            continue
        if b.get("status") != "scored":
            return True
        if b["normalized"] < a["normalized"] or severe_tail(b) > severe_tail(a) + 0.01:
            return True
    return False
