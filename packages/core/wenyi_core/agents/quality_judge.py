"""Structured quality requests using dedicated packaged rubric resources."""

from __future__ import annotations

from typing import Any

from ..i18n.resources import read_json
from ..llm.judgments import ChoiceQuestion, JudgmentRequest, JudgmentResult, ScoreQuestion
from ..quality.models import DIMENSIONS, QualityContext, digest


def rubric(version: str = "quality-v1") -> dict[str, Any]:
    return read_json(f"quality/{version}.json")


def rubric_fingerprint(version: str = "quality-v1") -> str:
    """Keep quality resources out of legacy prompt_fingerprint and off-mode identities."""
    return digest(rubric(version))


def score_request(context: QualityContext, version: str = "quality-v1") -> JudgmentRequest:
    rules = rubric(version)
    return JudgmentRequest(
        state=context.state,
        questions={
            dimension: ScoreQuestion(
                instructions=rules["instructions"] + "\nDimension: " + dimension,
                criteria=rules["dimensions"][dimension],
            )
            for dimension in DIMENSIONS
            if dimension != "terminology" or context.state["terms"]
        },
    )


def score_dimensions(result: JudgmentResult, request: JudgmentRequest) -> dict[str, Any]:
    result.validate_for(request)
    dimensions = {"terminology": {"status": "not_applicable"}}
    for name, answer in result.answers.items():
        data = answer.model_dump(mode="json")
        data.update(status="scored", normalized=100 * answer.score / 4)
        dimensions[name] = data
    return dimensions


def comparison_request(
    context: QualityContext,
    a: list[str],
    b: list[str],
    version: str = "quality-v1",
) -> JudgmentRequest:
    rules = rubric(version)
    state = {key: value for key, value in context.state.items() if key != "target_parts"}
    state.update(a=a, b=b)
    return JudgmentRequest(
        state=state,
        questions={
            "preference": ChoiceQuestion(
                instructions=rules["comparison"], criteria=rules["choices"]
            )
        },
    )
