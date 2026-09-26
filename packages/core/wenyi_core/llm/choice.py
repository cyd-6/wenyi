"""Provider-neutral closed-set decisions and validated results."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from ..i18n.resources import read_text


class ChoiceProtocolError(ValueError):
    """A successful response did not identify a valid option."""


class ChoiceUnavailable(RuntimeError):
    """No configured decision model returned a usable answer."""


class ChoiceInputTooLarge(RuntimeError):
    """The complete decision cannot fit this provider's context window."""


@dataclass(frozen=True)
class ChoiceRequest:
    state: dict[str, Any]
    instructions: str
    options: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.instructions.strip() or len(self.options) < 2:
            raise ValueError("A choice requires instructions and at least two options")
        if any(not isinstance(key, str) or not key for key in self.options):
            raise ValueError("Choice option IDs must be nonempty strings")

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": read_text("tasks/choice_system.txt")},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instructions": self.instructions,
                        "state": self.state,
                        "options": self.options,
                    },
                    ensure_ascii=False,
                ),
            },
        ]


@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    model: str | None = None
    provider: str | None = None
    fingerprint: str | None = None
    fallback_used: bool = False


def parse_choice(data: Any, request: ChoiceRequest) -> ChoiceResult:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("choice"), str)
        or data["choice"] not in request.options
    ):
        raise ChoiceProtocolError("The judge did not select a supplied candidate ID")

    def probability(value: Any) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ChoiceProtocolError("Invalid decision probability")
        return float(value)

    confidence = data.get("confidence")
    if confidence is not None:
        confidence = probability(confidence)
    probabilities = data.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or set(probabilities) != set(request.options):
            raise ChoiceProtocolError("Decision probabilities must cover exactly the options")
        probabilities = {key: probability(value) for key, value in probabilities.items()}
        if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01):
            raise ChoiceProtocolError("Decision probabilities must sum to one")
    return ChoiceResult(data["choice"], confidence, probabilities)
