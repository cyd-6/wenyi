"""Pure conservative candidate acceptance, independent of workflow and persistence."""

from __future__ import annotations

from typing import Any

from .models import QualityConfig
from .policy import critical_regression


def accept_candidate(
    before: dict[str, Any],
    after: dict[str, Any],
    comparisons: list[dict[str, Any]],
    config: QualityConfig,
) -> tuple[bool, list[str]]:
    """Require meaningful dimensional improvement and blind support against the incumbent."""
    if before.get("status") != "scored" or after.get("status") != "scored":
        return False, ["incomplete_scores"]
    if critical_regression(before, after):
        return False, ["critical_regression"]
    for row in (before, after):
        if any(
            value["confidence"] < config.thresholds.min_confidence_to_act
            for value in row["dimensions"].values()
            if value.get("status") == "scored"
        ):
            return False, ["uncertain_score"]
    if not any(
        value.get("status") == "scored"
        and before["dimensions"].get(name, {}).get("status") == "scored"
        and value["normalized"] - before["dimensions"][name]["normalized"]
        >= config.thresholds.min_dimension_improvement
        for name, value in after["dimensions"].items()
    ):
        return False, ["no_confirmed_improvement"]
    expected = 2 if config.comparison.swap_order else 1
    if len(comparisons) != expected:
        return False, ["incomplete_comparison"]
    for row in comparisons:
        preferred = "a_better" if row["candidate_label"] == "a" else "b_better"
        if row["choice"] == "equivalent":
            return False, ["equivalent"]
        if row["choice"] == "insufficient_evidence":
            return False, ["insufficient_evidence"]
        if row["choice"] != preferred:
            return False, ["order_conflict" if len(comparisons) > 1 else "original_preferred"]
        if row["confidence"] < config.thresholds.min_confidence_to_act:
            return False, ["uncertain_comparison"]
        if row["probabilities"][preferred] < config.thresholds.min_pairwise_support:
            return False, ["insufficient_support"]
    return True, ["accepted_by_comparison"]
