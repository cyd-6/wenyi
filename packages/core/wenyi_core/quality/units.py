"""Read-only logical paragraphs using the same continuation direction as export."""

from collections.abc import Mapping, Sequence

from ..ingest.models import Chapter, Segment
from .models import QualityUnit, digest


def build_quality_units(
    chapters: Sequence[Chapter],
    overrides: Mapping[tuple[int, int], str] | None = None,
    *,
    allow_empty_targets: bool = False,
) -> list[QualityUnit]:
    """Group contiguous continuation slices without conflating view and persistent indices."""
    overrides = overrides or {}
    units: list[QualityUnit] = []
    for chapter in chapters:
        groups: list[list[tuple[int, Segment]]] = []
        for text_index, segment in enumerate(chapter.text_segments):
            if segment.cont and groups and groups[-1][-1][1].kind == segment.kind:
                groups[-1].append((text_index, segment))
            else:
                groups.append([(text_index, segment)])
        for group in groups:
            indices = tuple(index for index, _ in group)
            segments = tuple(segment for _, segment in group)
            source = tuple(segment.source for segment in segments)
            target = tuple(
                overrides.get((chapter.index, i), segment.target) for i, segment in group
            )
            status, reasons = "ready", ()
            if segments[0].kind not in {"text", "heading"}:
                status, reasons = "not_applicable", ("non_language_unit",)
            elif any(value is None for value in target):
                status, reasons = "pending", ("incomplete_translation",)
            elif any(value == "" for value in target):
                status = "not_applicable" if allow_empty_targets else "needs_review"
                reasons = ("empty_target",)
            units.append(
                QualityUnit(
                    unit_id=f"ch{chapter.index}:text{indices[0]}:seg{segments[0].index}",
                    chapter_index=chapter.index,
                    text_indices=indices,
                    segment_indices=tuple(segment.index for segment in segments),
                    segment_refs=tuple(f"ch{chapter.index}:text{i}:seg{s.index}" for i, s in group),
                    kind=segments[0].kind,
                    source_parts=source,
                    target_parts=target,
                    source_hash=digest(source),
                    target_hash=digest(target),
                    status=status,
                    reason_codes=reasons,
                )
            )
    return units
