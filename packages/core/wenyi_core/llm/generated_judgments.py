"""Strict JSON bridge from ordinary generation models to typed judgments."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import Field

from ..i18n.resources import read_text
from .base import Messages
from .judgments import (
    ChoiceAnswer,
    JudgmentModel,
    JudgmentProtocolError,
    JudgmentProvenance,
    JudgmentRequest,
    JudgmentResult,
    JudgmentUsage,
    NoulAnswer,
    Probability,
    ScoreAnswer,
    ScoreQuestion,
)
from .usage import UsageSample

PROTOCOL_VERSION = 1
DEFAULT_OUTPUT_TOKENS = 8192
PROMPT_RESOURCE = "quality/generation_judge.txt"


def prompt_identity() -> dict[str, str | int]:
    return {
        "protocol": PROTOCOL_VERSION,
        "prompt": hashlib.sha256(read_text(PROMPT_RESOURCE).encode("utf-8")).hexdigest(),
    }


def judgment_messages(request: JudgmentRequest) -> Messages:
    return [
        {"role": "system", "content": read_text(PROMPT_RESOURCE)},
        {
            "role": "user",
            "content": json.dumps(
                request.model_dump(exclude_none=True), ensure_ascii=False, allow_nan=False
            ),
        },
    ]


class _GeneratedScore(JudgmentModel):
    type: Literal["score"]
    probabilities: dict[str, Probability]
    confidence: Probability


_GeneratedAnswer = Annotated[
    _GeneratedScore | ChoiceAnswer | NoulAnswer, Field(discriminator="type")
]


class _Response(JudgmentModel):
    answers: dict[str, _GeneratedAnswer]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise JudgmentProtocolError("Generated judgment returned duplicate JSON keys")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise JudgmentProtocolError("Generated judgment returned non-finite JSON numbers")


def parse_generated_judgment(
    text: str, request: JudgmentRequest, *, model: str, usage: UsageSample | None
) -> JudgmentResult:
    """Validate the complete answer; transport identity and billing cannot come from text."""
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        response = _Response.model_validate(raw)
        if set(response.answers) != set(request.questions):
            raise JudgmentProtocolError("Judgment answers do not match the requested question IDs")
        answers = {}
        for key, answer in response.answers.items():
            question = request.questions[key]
            if answer.type != question.type:
                raise JudgmentProtocolError("Judgment answer type does not match its question")
            if isinstance(answer, _GeneratedScore) and isinstance(question, ScoreQuestion):
                levels = {str(index) for index in range(len(question.criteria))}
                if set(answer.probabilities) != levels:
                    raise JudgmentProtocolError("Score has incomplete levels")
                answers[key] = ScoreAnswer(
                    score=math.fsum(int(k) * v for k, v in answer.probabilities.items()),
                    legend={
                        str(index): criterion
                        if isinstance(criterion, str)
                        else json.dumps(criterion, ensure_ascii=False, allow_nan=False)
                        for index, criterion in enumerate(question.criteria)
                    },
                    probabilities=answer.probabilities,
                    confidence=answer.confidence,
                )
            else:
                answers[key] = answer
        return JudgmentResult(
            model=model,
            answers=answers,
            usage=JudgmentUsage(
                input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens
            )
            if usage is not None
            else None,
            provenance=JudgmentProvenance(
                source="generated",
                confidence="self_reported",
                probabilities="self_reported",
                model_identity="requested",
            ),
        ).validate_for(request)
    except JudgmentProtocolError:
        raise
    except (ValueError, TypeError):
        raise JudgmentProtocolError(
            "Generation model returned an invalid or incomplete JSON judgment"
        ) from None
