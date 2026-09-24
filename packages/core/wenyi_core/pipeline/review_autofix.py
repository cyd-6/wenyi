"""Review Autofix publication service.
The review engine stays read-only. After its results are persisted, overlay changes on an
immutable working snapshot and verify remaining issues by paragraph through the bounded
evidence loop. Prepare every candidate and persist autofix/index.json before modifying
formal chapter target text, enabling idempotent recovery through before/after hashes.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryTerm
from ..llm.usage import validate_usage
from ..review.models import ReviewOutcome
from ..review.run_store import ReviewRunStore
from ..storage.protocol import Storage
from .autofix_candidates import AutofixCandidateService
from .autofix_plan import prepare_identity, save_plan
from .autofix_publish import AutofixPublisher
from .docx_styles import DocxStyleService
from .quality_workflow import QualityWorkflow

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class ReviewAutofixService:
    """Publish read-only review recommendations to formal chapters with a recoverable index."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._publisher = AutofixPublisher(annotations, DocxStyleService(runtime))
        self._candidates = AutofixCandidateService(
            runtime.config, runtime.client, runtime.analyzer.style_brief
        )

    @staticmethod
    def _review_result(debug: ReviewRunStore) -> dict[str, Any] | None:
        result = debug.load_json("result.json")
        return result if isinstance(result, dict) else None

    @staticmethod
    def _index(debug: ReviewRunStore) -> dict[str, Any] | None:
        result = debug.load_json("autofix/index.json")
        return result if isinstance(result, dict) else None

    def resume_pending(
        self,
        store: Storage,
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome | None:
        """Finish interrupted indexed publication before repeating review or agent calls."""
        if not self._runtime.config.pipeline.review_autofix:
            return None
        for name in ReviewRunStore.list_review_ids(store):
            if not name.startswith("review-"):
                continue
            run_dir = os.path.join(store.reviews_dir, name)
            debug = ReviewRunStore.open_existing(run_dir, storage=store)
            result = self._review_result(debug)
            if result is None:
                continue
            index = self._index(debug)
            # Handle only the newest valid review. If it lacks an index, the normal path reuses its result
            # and plans autofix; never bypass it to publish an older leftover index.
            if index is None:
                return None
            status = index.get("status")
            result_autofix = result.get("autofix")
            if status == "applying":
                debug = ReviewRunStore.open_existing(run_dir, storage=store)
                debug.log_event("review_autofix_resumed", review_id=name)
                return self._apply_index(store, debug, index, result, progress=progress)
            if status in {"completed", "partial"} and (
                not isinstance(result_autofix, dict)
                or index.get("quality_final_status") == "pending"
            ):
                debug = ReviewRunStore.open_existing(run_dir, storage=store)
                return self._finish_with_scores(store, debug, index, result, progress=progress)
            # Newest directories come first; once the newest is complete, never publish earlier indices.
            if status in {"completed", "partial"}:
                return None
        return None

    def run(
        self,
        store: Storage,
        outcome: ReviewOutcome,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """Flush completed model responses even if planning or publication is interrupted."""
        if not self._runtime.config.pipeline.review_autofix:
            return outcome
        debug = ReviewRunStore.open_existing(outcome.run_dir, storage=store)
        try:
            return self._run(store, outcome, all_terms, progress=progress)
        finally:
            self._save_usage_delta(store, debug, scope="review_autofix")

    def _run(
        self,
        store: Storage,
        outcome: ReviewOutcome,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """Prepare final candidates and an index for a completed review, then publish
        idempotently.
        """
        if not self._runtime.config.pipeline.review_autofix:
            return outcome
        debug = ReviewRunStore.open_existing(outcome.run_dir, storage=store)
        validate_usage(debug.load_usage())
        existing = self._index(debug)
        if existing is not None:
            status = existing.get("status")
            if status == "applying":
                return self._apply_index(
                    store,
                    debug,
                    existing,
                    outcome.result,
                    progress=progress,
                )
            if status in {"completed", "partial"}:
                return self._finish_with_scores(
                    store, debug, existing, outcome.result, progress=progress
                )

        from ..llm.operations import configured_operations

        quality_operations = tuple(
            operation
            for operation in configured_operations(self._runtime.config, "review")
            if operation.startswith("review.quality_")
        )
        inference = prepare_identity(debug, self._runtime.llm_config, quality_operations)
        manifest = store.load_manifest()
        chapters = [
            store.load_chapter(row["index"])
            for row in manifest.get("chapters", [])
            if isinstance(row.get("index"), int)
        ]
        quality = self._quality_workflow(store, debug, all_terms, progress)
        candidates = self._candidates.prepare(
            chapters,
            store.load_analysis() or {},
            outcome,
            all_terms,
            debug,
            progress=progress,
            quality=quality,
        )
        if quality is not None:
            if self._runtime.config.pipeline.quality.mode == "optimize":
                accepted, decisions = quality.gate(
                    chapters, {}, candidates.overrides, origin="publication"
                )
                debug.write_json("autofix/quality-publication-gate.json", decisions)
                for location in list(candidates.overrides):
                    if location not in accepted:
                        candidates.overrides.pop(location)
                        for record in candidates.records:
                            if (record["chapter"], record["index"]) == location and record[
                                "status"
                            ] == "planned":
                                record["status"] = "failed"
                                record["reason"] = "quality_publication_rejected"
                # Include unchanged members too: one manually edited member blocks
                # the complete logical paragraph at the chapter commit boundary.
                from ..quality.units import build_quality_units

                for unit in build_quality_units(chapters):
                    if not any(
                        (unit.chapter_index, i) in candidates.overrides for i in unit.text_indices
                    ):
                        continue
                    group = {
                        "quality_unit_id": unit.unit_id,
                        "member_refs": list(unit.segment_refs),
                    }
                    for i, target in zip(unit.text_indices, unit.target_parts):
                        location = (unit.chapter_index, i)
                        candidates.overrides.setdefault(location, target)
                        candidates.quality_groups[location] = group
            quality.final_scores(chapters, candidates.overrides, scope="shadow")
        index = dict(save_plan(chapters, candidates, inference, debug, outcome))
        self._save_usage_delta(store, debug, scope="review_autofix_agent")
        return self._apply_index(
            store,
            debug,
            index,
            outcome.result,
            progress=progress,
        )

    def _quality_workflow(self, store, debug, all_terms, progress):
        if self._runtime.config.pipeline.quality.mode == "off":
            return None
        return QualityWorkflow(
            self._runtime.config,
            self._runtime.client,
            store,
            debug.review_id,
            all_terms,
            store.load_analysis() or {},
            progress=progress,
            flush_usage=lambda artifacts=None: self._runtime.flush_usage(
                store, scope="quality_autofix", review=debug, artifacts=artifacts
            ),
        )

    def _save_usage_delta(
        self,
        store: Storage,
        debug: ReviewRunStore,
        *,
        scope: str,
    ) -> None:
        """Merge unflushed runtime calls into both the review ledger and whole-book totals."""
        self._runtime.flush_usage(store, scope=scope, review=debug)

    def _apply_index(
        self,
        store: Storage,
        debug: ReviewRunStore,
        index: dict[str, Any],
        result: dict[str, Any],
        *,
        progress: ProgressFn | None,
    ) -> ReviewOutcome:
        self._publisher.apply(store, debug, index, result, progress=progress)
        return self._finish_with_scores(store, debug, index, result, progress=progress)

    def _finish_with_scores(self, store, debug, index, result, *, progress):
        if index.get("quality_final_status") == "pending":
            quality = self._quality_workflow(store, debug, store.all_terms(), progress)
            if quality is not None:
                actual = [
                    store.load_chapter(row["index"])
                    for row in store.load_manifest().get("chapters", [])
                ]
                summary = quality.final_scores(actual, {}, scope="formal")
                result = {**result, "quality": {**result.get("quality", {}), "formal": summary}}
                debug.write_json("result.json", result)
            # Turning the feature off preserves indexed publication without buying judgments.
            index["quality_final_status"] = "completed" if quality is not None else "unavailable"
            debug.write_json("autofix/index.json", index)
        self._save_usage_delta(store, debug, scope="review_autofix_publish")
        return self._publisher.finish(store, debug, index, result)
