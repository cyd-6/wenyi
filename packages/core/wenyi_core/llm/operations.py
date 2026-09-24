"""The single registration point for model-driven workflow operations."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..config import Config

TIERS = ("strong", "cheap", "fast")


@dataclass(frozen=True)
class OperationSpec:
    """Declare routing, output hints and workflow reachability together."""

    id: str
    description: str
    tier: str | None = None
    inherits: str | None = None
    output_tokens: int | None = None
    workflows: tuple[str, ...] = ("translate",)
    flags: tuple[str, ...] = ()
    review: bool = False
    resumable_conversation: bool = False
    protocol_version: int = 1
    capability: Literal["generation", "judgment"] = "generation"
    requires_explicit_route: bool = False


def register_operations(specs: Iterable[OperationSpec]) -> Mapping[str, OperationSpec]:
    """Validate registrations before publishing an immutable registry."""
    registry: dict[str, OperationSpec] = {}
    for spec in specs:
        if not re.fullmatch(r"[a-z][a-z_]*\.[a-z][a-z_]*", spec.id):
            raise ValueError(f"Invalid operation ID: {spec.id}")
        if spec.id in registry:
            raise ValueError(f"Duplicate operation: {spec.id}")
        if (
            sum((spec.tier is not None, spec.inherits is not None, spec.requires_explicit_route))
            != 1
        ):
            raise ValueError(f"Operation {spec.id} needs exactly one default selection")
        if spec.capability not in {"generation", "judgment"}:
            raise ValueError(f"Unknown operation capability: {spec.capability}")
        if spec.tier is not None and spec.tier not in TIERS:
            raise ValueError(f"Unknown default tier: {spec.tier}")
        if spec.output_tokens is not None and spec.output_tokens <= 0:
            raise ValueError(f"Invalid output hint: {spec.id}")
        if spec.protocol_version <= 0:
            raise ValueError(f"Invalid operation protocol version: {spec.id}")
        registry[spec.id] = spec
    for spec in registry.values():
        seen = {spec.id}
        parent = spec.inherits
        while parent is not None:
            if parent not in registry:
                raise ValueError(f"Unknown inherited operation: {parent}")
            if parent in seen:
                raise ValueError(f"Operation inheritance cycle: {parent}")
            seen.add(parent)
            parent = registry[parent].inherits
    return MappingProxyType(registry)


OPERATIONS = register_operations(
    (
        OperationSpec(
            "language.detect",
            "Detect source language",
            "cheap",
            workflows=("prepare", "translate"),
            flags=("language_auto",),
        ),
        OperationSpec(
            "analysis.style",
            "Analyze style, characters and seed terms",
            "strong",
            workflows=("prepare", "translate"),
        ),
        OperationSpec(
            "synopsis.chapter",
            "Summarize one chapter",
            "fast",
            output_tokens=600,
            workflows=("prepare", "translate"),
            flags=("book_understanding",),
        ),
        OperationSpec(
            "synopsis.book",
            "Merge digests into a book synopsis",
            "fast",
            output_tokens=1200,
            workflows=("prepare", "translate"),
            flags=("book_understanding",),
        ),
        OperationSpec(
            "translation.body", "Translate body paragraphs", "strong", protocol_version=2
        ),
        OperationSpec("translation.title", "Translate chapter and TOC titles", "strong"),
        OperationSpec(
            "polish.body",
            "Polish translated paragraphs",
            "strong",
            flags=("polish",),
            protocol_version=2,
        ),
        OperationSpec("glossary.extract", "Extract glossary candidates", "fast"),
        OperationSpec("glossary.align_history", "Align terms with earlier translations", "fast"),
        OperationSpec(
            "annotation.align",
            "Align EPUB annotation positions",
            "cheap",
            workflows=("translate", "review"),
            flags=("annotation_alignment",),
        ),
        OperationSpec(
            "review.scan",
            "Review and blindly re-review translated text",
            "cheap",
            workflows=("translate", "review"),
            review=True,
        ),
        OperationSpec(
            "review.verify",
            "Verify issues with evidence",
            "strong",
            workflows=("translate", "review"),
            flags=("review_agent_loop",),
            review=True,
            resumable_conversation=True,
        ),
        OperationSpec(
            "review.arbitrate",
            "Arbitrate conflicting recommendations",
            "strong",
            workflows=("translate", "review"),
            flags=("review_agent_loop", "review_conflict_arbitration"),
            review=True,
            resumable_conversation=True,
        ),
        OperationSpec(
            "review.fix",
            "Propose shadow translation replacements",
            "strong",
            workflows=("translate", "review"),
            flags=("review_fix_loop",),
            review=True,
        ),
        OperationSpec(
            "review.quality_score",
            "Score logical translation units with structured judgments",
            workflows=("translate", "review"),
            flags=("quality_enabled",),
            review=True,
            capability="judgment",
            requires_explicit_route=True,
        ),
        OperationSpec(
            "review.quality_compare",
            "Compare original and candidate translations blindly",
            inherits="review.quality_score",
            workflows=("translate", "review"),
            flags=("quality_optimize",),
            review=True,
            capability="judgment",
        ),
        OperationSpec(
            "review.quality_diagnose",
            "Check uncertain quality findings against source evidence",
            "strong",
            workflows=("translate", "review"),
            flags=("quality_optimize",),
            review=True,
        ),
        OperationSpec(
            "review.quality_retranslate",
            "Generate an independent logical-unit translation",
            inherits="translation.body",
            workflows=("translate", "review"),
            flags=("quality_optimize",),
            review=True,
        ),
        OperationSpec(
            "review.quality_revise",
            "Revise only confirmed translation problems",
            inherits="polish.body",
            workflows=("translate", "review"),
            flags=("quality_optimize",),
            review=True,
        ),
        OperationSpec(
            "review.quality_verify",
            "Independently verify uncertain judgments",
            "strong",
            workflows=("translate", "review"),
            flags=("quality_optimize",),
            review=True,
        ),
        OperationSpec(
            "autofix.verify",
            "Verify issues before publication",
            inherits="review.verify",
            workflows=("translate", "review"),
            flags=("review_autofix",),
            review=True,
            resumable_conversation=True,
        ),
        OperationSpec(
            "autofix.fix",
            "Propose replacements for publication",
            inherits="review.fix",
            workflows=("translate", "review"),
            flags=("review_autofix",),
            review=True,
        ),
        OperationSpec("srt.translate", "Translate subtitle cues", "strong", workflows=("srt",)),
    )
)


def require_operation(operation: str) -> OperationSpec:
    """Reject unknown operations instead of selecting an arbitrary model."""
    try:
        return OPERATIONS[operation]
    except KeyError:
        raise ValueError(f"Unknown model operation: {operation}") from None


def workflow_operations(workflow: str, flags: Mapping[str, object]) -> tuple[str, ...]:
    """Select reachable operations, including recovery, without touching state or SDKs."""
    if workflow not in {"prepare", "translate", "review", "srt"}:
        raise ValueError(f"Unknown model workflow: {workflow}")
    return tuple(
        spec.id
        for spec in OPERATIONS.values()
        if workflow in spec.workflows
        and (not spec.review or workflow == "review" or flags.get("review", True))
        and all(flags.get(flag, not flag.startswith("quality_")) for flag in spec.flags)
    )


def configured_operations(config: Config, workflow: str) -> tuple[str, ...]:
    """Map product settings to registry flags without constructing a model client."""
    flags = config.pipeline.model_dump()
    flags["language_auto"] = config.source_lang == "auto"
    quality = flags.get("quality", {})
    mode = quality.get("mode", "off") if isinstance(quality, dict) else "off"
    flags["quality_enabled"] = mode in {"observe", "optimize"}
    flags["quality_optimize"] = mode == "optimize"
    return workflow_operations(workflow, flags)
