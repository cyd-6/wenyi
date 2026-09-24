"""Typed judgments with explicit provenance for native and generated answers."""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .usage import UsageSample

Content = str | dict[str, JsonValue] | list[JsonValue]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False, strict=True)]
DISTRIBUTION_TOLERANCE = 0.005


class JudgmentProtocolError(ValueError):
    """A successful transport response did not satisfy the judgment contract."""


class JudgmentContextOverflow(ValueError):
    """The conservative estimate cannot fit the complete evaluation evidence."""


class JudgmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ScoreQuestion(JudgmentModel):
    type: Literal["score"] = "score"
    instructions: Content
    criteria: list[Content] = Field(min_length=2, max_length=10)


class ChoiceQuestion(JudgmentModel):
    type: Literal["choice"] = "choice"
    instructions: Content
    criteria: dict[str, Content | None] = Field(min_length=1, max_length=255)


class NoulQuestion(JudgmentModel):
    type: Literal["noul"] = "noul"
    instructions: Content
    criteria: dict[Literal["true", "false"], Content] | None = None


Question = Annotated[ScoreQuestion | ChoiceQuestion | NoulQuestion, Field(discriminator="type")]


def encoded_size(value: object) -> int:
    """Use UTF-8 bytes as a conservative estimate, never as the billing tokenizer."""
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class JudgmentRequest(JudgmentModel):
    state: Content
    questions: dict[str, Question] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_json(self) -> JudgmentRequest:
        encoded_size(self.model_dump(exclude_none=True))
        if any(not key.strip() for key in self.questions):
            raise ValueError("Judgment question IDs cannot be blank")
        return self

    def estimated_input_tokens(self) -> int:
        return encoded_size(self.model_dump(exclude_none=True)) + 256

    def estimated_output_tokens(self) -> int:
        """Reserve bounded structured output without inventing a generation parameter."""
        total = 256
        for key, question in self.questions.items():
            total += encoded_size(key) + 128
            if isinstance(question, ScoreQuestion):
                total += encoded_size(question.criteria) + 64 * len(question.criteria)
            elif isinstance(question, ChoiceQuestion):
                total += encoded_size(list(question.criteria)) + 64 * len(question.criteria)
        return total


def _check_distribution(probabilities: dict[str, float]) -> None:
    if not probabilities or abs(math.fsum(probabilities.values()) - 1) > DISTRIBUTION_TOLERANCE:
        raise ValueError("Judgment probabilities must sum to one within rounding tolerance")


class ScoreAnswer(JudgmentModel):
    type: Literal["score"] = "score"
    score: FiniteNumber
    legend: dict[str, str]
    probabilities: dict[str, Probability]
    confidence: Probability

    @model_validator(mode="after")
    def valid_distribution(self) -> ScoreAnswer:
        _check_distribution(self.probabilities)
        if set(self.legend) != set(self.probabilities):
            raise ValueError("Score legend and probability levels must match")
        return self


class ChoiceAnswer(JudgmentModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, Probability]
    confidence: Probability

    @model_validator(mode="after")
    def valid_distribution(self) -> ChoiceAnswer:
        _check_distribution(self.probabilities)
        if self.choice not in self.probabilities:
            raise ValueError("Chosen option is missing from its probability distribution")
        if self.probabilities[self.choice] + DISTRIBUTION_TOLERANCE < max(
            self.probabilities.values()
        ):
            raise ValueError("Chosen option is not a highest-probability option")
        return self


class NoulAnswer(JudgmentModel):
    type: Literal["noul"] = "noul"
    noul: Probability


Answer = Annotated[ScoreAnswer | ChoiceAnswer | NoulAnswer, Field(discriminator="type")]


class JudgmentUsage(JudgmentModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)

    def sample(self) -> UsageSample:
        return UsageSample(
            self.input_tokens, self.output_tokens, self.input_tokens + self.output_tokens
        )


class JudgmentProvenance(JudgmentModel):
    """Distinguish native distributions from a generation model's self-assessment."""

    source: Literal["native", "generated"] = "native"
    confidence: Literal["model_distribution", "self_reported"] = "model_distribution"
    probabilities: Literal["model_distribution", "self_reported"] = "model_distribution"
    model_identity: Literal["resolved", "requested"] = "resolved"


class JudgmentResult(JudgmentModel):
    model: str = Field(min_length=1)
    answers: dict[str, Answer]
    usage: JudgmentUsage | None = None
    request_id: str | None = None
    provenance: JudgmentProvenance = Field(default_factory=JudgmentProvenance)

    def validate_for(self, request: JudgmentRequest) -> JudgmentResult:
        """Validate completeness and question-specific domains before exposing any answer."""
        if set(self.answers) != set(request.questions):
            raise JudgmentProtocolError("Judgment answers do not match the requested question IDs")
        for key, question in request.questions.items():
            answer = self.answers[key]
            if answer.type != question.type:
                raise JudgmentProtocolError("Judgment answer type does not match its question")
            if isinstance(question, ScoreQuestion) and isinstance(answer, ScoreAnswer):
                levels = {str(index) for index in range(len(question.criteria))}
                if set(answer.probabilities) != levels or not 0 <= answer.score <= len(levels) - 1:
                    raise JudgmentProtocolError(
                        "Score has incomplete levels or is outside its rubric"
                    )
                expected = math.fsum(
                    int(level) * value for level, value in answer.probabilities.items()
                )
                if abs(answer.score - expected) > DISTRIBUTION_TOLERANCE * len(levels):
                    raise JudgmentProtocolError("Score contradicts its probability distribution")
            elif isinstance(question, ChoiceQuestion) and isinstance(answer, ChoiceAnswer):
                if set(answer.probabilities) != set(question.criteria):
                    raise JudgmentProtocolError(
                        "Choice probabilities do not cover exactly its options"
                    )
        return self
