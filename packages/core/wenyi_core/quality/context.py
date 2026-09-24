"""Fixed structured evidence and conservative context budgeting without truncating core text."""

from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..glossary.store import GlossaryTerm
from ..i18n import languages
from ..ingest.tokens import count_tokens
from .models import QualityContext, QualityUnit, digest


def build_context(
    unit: QualityUnit,
    units: list[QualityUnit],
    config: Config,
    terms: list[GlossaryTerm],
    analysis: dict[str, Any] | None = None,
    *,
    expansion: int = 0,
) -> QualityContext:
    """Include relevant terms and fixed neighbors; omit supplemental material before failing."""
    options = config.pipeline.quality.context
    same_chapter = [value for value in units if value.chapter_index == unit.chapter_index]
    position = next(i for i, value in enumerate(same_chapter) if value.unit_id == unit.unit_id)
    before = same_chapter[max(0, position - options.preceding_units - expansion) : position]
    after = same_chapter[position + 1 : position + 1 + options.following_units + expansion]
    source = "".join(unit.source_parts)
    applicable = sorted(
        (
            {"source": term.source, "target": term.target, "type": term.type, "note": term.note}
            for term in terms
            if term.source in source or any(alias and alias in source for alias in term.aliases)
        ),
        key=lambda term: (term["source"], term["target"]),
    )
    neighbors = [
        {
            "unit_id": value.unit_id,
            "source_parts": list(value.source_parts),
            "target_parts": list(value.target_parts),
        }
        for value in before + after
    ]
    state = {
        "source_language": config.source_lang,
        "target_language": config.target_lang,
        "honorific_strategy": config.honorific_strategy,
        "language_guidance": languages.translate_guidance(
            config.source_lang, tgt=config.target_lang
        ),
        "target_guidance": languages.profile(config.target_lang)["target_guidance"],
        "kind": unit.kind,
        "source_parts": list(unit.source_parts),
        "target_parts": list(unit.target_parts),
        "terms": applicable,
        "neighbors": neighbors,
        "supplementary": analysis or {},
    }
    omitted = []

    def estimate() -> int:
        return count_tokens(json.dumps(state, ensure_ascii=False))

    # Keep current source, target, terms and actual neighbor evidence intact.
    # No truncated input is reported as a full paragraph assessment.
    if estimate() > options.max_estimated_input_tokens and state["supplementary"]:
        state["supplementary"] = {}
        omitted.append("supplementary")
    tokens = estimate()
    context = {
        key: value for key, value in state.items() if key not in {"source_parts", "target_parts"}
    }
    return QualityContext(
        state,
        digest(context),
        "ready" if tokens <= options.max_estimated_input_tokens else "context_overflow",
        tokens,
        tuple(omitted),
    )
