"""Run-level quality scoring and conservative shadow proposals over Storage artifacts.

All model responses use the existing usage journal when a runtime callback is supplied.
Nothing in this service writes chapters, manifests, glossary data or publication indexes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import RLock
from typing import Any

from ..agents.quality_candidates import (
    candidate_messages,
    validate_candidate,
    validate_diagnosis,
)
from ..agents.quality_judge import (
    comparison_request,
    rubric_fingerprint,
    score_dimensions,
    score_request,
)
from ..config import Config
from ..glossary.store import GlossaryTerm
from ..ingest.models import Chapter
from ..llm.base import LLMClient
from ..llm.judgments import JudgmentContextOverflow, JudgmentResult
from ..llm.retrying import is_resumable_provider_interrupt
from ..quality.context import build_context
from ..quality.models import QualityContext, QualityUnit, digest
from ..quality.policy import audited, critical_regression, priority, route_score
from ..quality.selection import accept_candidate
from ..quality.units import build_quality_units
from ..storage.protocol import Storage

Overrides = Mapping[tuple[int, int], str]


class QualityBudgetExceeded(ValueError):
    """A local quality reservation could not be made; ordinary Review can still run."""


class QualityRequestFailed(ValueError):
    """A persisted response is unavailable or malformed; never accept its candidate."""


class QualityWorkflow:
    """Score every logical unit once, generate bounded proposals, and gate automatic edits."""

    def __init__(
        self,
        config: Config,
        client: LLMClient,
        store: Storage,
        review_id: str,
        terms: list[GlossaryTerm],
        analysis: dict[str, Any] | None = None,
        flush_usage: Callable[[dict[str, Any]], Any] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ):
        self.config, self.client, self.store = config, client, store
        self.options = config.pipeline.quality
        self.review_id, self.terms = review_id, terms
        self.analysis, self.flush_usage, self.progress = analysis or {}, flush_usage, progress
        self.prefix = f"reviews/{review_id}/quality"
        self._lock = RLock()
        self._attempted_responses: set[str] = set()
        self._view_cache: list[tuple[list[QualityUnit], str, dict[int, list[QualityUnit]]]] = []
        self._manifest = store.read_artifact(f"{self.prefix}/manifest.json") or {
            "schema_version": 1,
            "phase": "pending",
            "requests": {},
            "selected_unit_ids": None,
            "cache_reused": 0,
            "degraded": False,
            "incomplete": False,
        }
        manifest = store.load_manifest()
        self.allow_empty = manifest.get("format", manifest.get("fmt")) == "pdf" and (
            manifest.get("pdf_backend", "mineru") == "mineru"
        )
        self._manifest["config"] = {
            "source_lang": config.source_lang,
            "target_lang": config.target_lang,
            "honorific_strategy": config.honorific_strategy,
            "context": self.options.context.model_dump(mode="json"),
            "rubric_version": self.options.rubric_version,
        }
        self._manifest["mode"] = self.options.mode
        policy_identity = digest(self.options.model_dump(mode="json"))
        previous_policy = self._manifest.get("policy_identity")
        self._policy_changed = previous_policy is not None and previous_policy != policy_identity
        if self._policy_changed:
            self._manifest["selected_unit_ids"] = None
        self._manifest["policy_identity"] = policy_identity
        self._prior_responses: dict[str, str] = {}
        if self.options.mode != "off":
            for key in sorted(store.list_artifacts("reviews/")):
                if "/quality/responses/" in key and not key.startswith(self.prefix + "/"):
                    self._prior_responses[key.rsplit("/", 1)[-1]] = key

    @property
    def degraded(self) -> bool:
        return bool(self._manifest["degraded"])

    def _save_manifest(self) -> None:
        self.store.write_artifact(f"{self.prefix}/manifest.json", self._manifest)

    def _phase(self, phase: str, done: int = 0, total: int = 0) -> None:
        self._manifest["phase"] = phase
        self._save_manifest()
        self.store.log_event(phase, review_id=self.review_id, completed=done, total=total)
        if self.progress:
            self.progress(done, total, phase)

    def _identity(self, operation: str) -> str:
        method = getattr(self.client, "inference_fingerprint", None)
        if method:
            return method(operation)
        # Test clients may not expose routing; production RoutedLLMClient always does.
        return digest([type(self.client).__module__, type(self.client).__qualname__, operation])

    def _response(
        self,
        operation: str,
        payload: Any,
        call: Callable[[], Any],
        *,
        kind: str,
        unit_id: str | None = None,
    ) -> Any:
        """Reserve once, persist response with its usage, and reuse without repurchasing."""
        response_id = digest(
            {
                "operation": operation,
                "inference": self._identity(operation),
                "payload": payload,
                "schema_version": 1,
            }
        )
        key = f"{self.prefix}/responses/{response_id}.json"
        with self._lock:
            cached = self.store.read_artifact(key)
            route = getattr(self.client, "routes", {}).get(operation)
            requested_model = getattr(route, "model", None)
            # Moving aliases may resolve differently between runs. Reuse only pinned judges.
            if (
                cached is None
                and kind == "judge"
                and requested_model
                and re.search(r"-\d+\.\d+\.\d+$", requested_model)
            ):
                prior_key = self._prior_responses.get(response_id + ".json")
                prior = self.store.read_artifact(prior_key) if prior_key else None
                if (
                    prior
                    and prior.get("status") == "completed"
                    and prior.get("response", {}).get("model") == requested_model
                    and prior.get("response", {}).get("provenance", {}).get("source", "native")
                    == "native"
                    and prior.get("response", {})
                    .get("provenance", {})
                    .get("model_identity", "resolved")
                    == "resolved"
                ):
                    cached = {**prior, "cache_origin": prior_key}
                    self._persist_response(key, cached)
            if cached is not None:
                if cached.get("status") == "completed":
                    self._manifest["cache_reused"] += 1
                    return cached["response"]
                # A new caller-created workflow is an explicit resume. Permit one new
                # reservation for a previously failed/ambiguous request, never a local loop.
                if (
                    cached.get("status") not in {"failed", "ambiguous"}
                    or response_id in self._attempted_responses
                ):
                    raise QualityRequestFailed(cached.get("error_type", "quality_request_failed"))
            if self.degraded:
                raise QualityRequestFailed("quality_degraded")
            requests = self._manifest["requests"]
            # An interrupted reservation is ambiguous, not proof that the provider was unbilled.
            limit = (
                self.options.max_judge_requests_per_run
                if kind == "judge"
                else self.options.max_generation_requests_per_run
            )
            spent = sum(value["attempts"] for value in requests.values() if value["kind"] == kind)
            if spent >= limit:
                self._manifest["incomplete"] = True
                self._save_manifest()
                raise QualityBudgetExceeded("quality_budget_exhausted")
            previous = requests.get(response_id, {})
            history = list((cached or {}).get("attempt_history", []))
            if cached is not None:
                history.append(
                    {
                        "attempt": cached.get("attempt", previous.get("attempts", 1)),
                        **{
                            field: cached[field]
                            for field in ("status", "error_type", "ambiguous")
                            if field in cached
                        },
                    }
                )
            attempt = previous.get("attempts", 0) + 1
            self._attempted_responses.add(response_id)
            requests[response_id] = {
                "kind": kind,
                "operation": operation,
                "unit_id": unit_id,
                "attempts": attempt,
                "status": "in_flight",
                "request_id": response_id,
                "ambiguous_previous_attempt": bool(previous)
                and (
                    previous.get("status") == "in_flight"
                    or (cached or {}).get("status") == "ambiguous"
                ),
            }
            self._save_manifest()
        try:
            response = call()
            if hasattr(response, "model_dump"):
                response = response.model_dump(mode="json")
            record = {
                "status": "completed",
                "operation": operation,
                "request_id": response_id,
                "response": response,
                "attempt": attempt,
                "attempt_history": history,
            }
        except BaseException as error:
            # Sanitized diagnostics only: provider messages can contain private source text.
            record = {
                "status": "ambiguous"
                if not isinstance(error, Exception) or is_resumable_provider_interrupt(error)
                else "failed",
                "operation": operation,
                "request_id": response_id,
                "error_type": type(error).__name__,
                "ambiguous": not isinstance(error, ValueError),
                "attempt": attempt,
                "attempt_history": history,
            }
            self._persist_response(key, record)
            self._manifest["requests"][response_id]["status"] = "failed"
            self._save_manifest()
            raise
        self._persist_response(key, record)
        self._manifest["requests"][response_id]["status"] = "completed"
        self._save_manifest()
        return response

    def _persist_response(self, key: str, response: dict[str, Any]) -> None:
        if self.flush_usage is not None:
            self.flush_usage({key: response})
        else:
            self.store.write_artifact(key, response)

    def _handle_error(self, error: Exception) -> str:
        if self.options.on_error == "stop":
            raise error
        self._manifest["incomplete"] = True
        if not isinstance(error, QualityBudgetExceeded):
            self._manifest["degraded"] = True
        self._save_manifest()
        return (
            "budget_exhausted"
            if isinstance(error, QualityBudgetExceeded)
            else "quality_unavailable"
        )

    def _units(self, chapters: list[Chapter], overrides: Overrides) -> list[QualityUnit]:
        return build_quality_units(chapters, overrides, allow_empty_targets=self.allow_empty)

    def _view(self, units: list[QualityUnit]) -> tuple[str, dict[int, list[QualityUnit]]]:
        """Cache immutable snapshot identity and chapter groups instead of hashing a book per unit."""
        for cached_units, snapshot, chapters in self._view_cache:
            if cached_units is units:
                return snapshot, chapters
        grouped: dict[int, list[QualityUnit]] = {}
        for unit in units:
            grouped.setdefault(unit.chapter_index, []).append(unit)
        snapshot = digest([(unit.unit_id, unit.target_hash) for unit in units])
        self._view_cache.append((units, snapshot, grouped))
        self._view_cache = self._view_cache[-8:]
        return snapshot, grouped

    def _context(
        self,
        unit: QualityUnit,
        units: list[QualityUnit],
        *,
        expansion: int = 0,
    ) -> QualityContext:
        _, grouped = self._view(units)
        return build_context(
            unit,
            grouped[unit.chapter_index],
            self.config,
            self.terms,
            self.analysis,
            expansion=expansion,
        )

    def _score(
        self,
        unit: QualityUnit,
        units: list[QualityUnit],
        *,
        scope: str,
        context: QualityContext | None = None,
    ) -> dict[str, Any]:
        context = context or self._context(unit, units)
        row = {
            "schema_version": 1,
            **unit.identity(),
            "text_scope": scope,
            "context_hash": context.context_hash,
            "snapshot_id": self._view(units)[0],
            "status": unit.status,
            "reason_codes": list(unit.reason_codes),
            "dimensions": {},
            "publication_status": "not_requested",
            "generation_attempts": sum(
                value["attempts"]
                for value in self._manifest["requests"].values()
                if value.get("unit_id") == unit.unit_id
                and value["operation"] in {"review.quality_retranslate", "review.quality_revise"}
            ),
            "target_parts": list(unit.target_parts),
            "judge": {
                "rubric_version": self.options.rubric_version,
                "rubric_fingerprint": rubric_fingerprint(self.options.rubric_version),
                "inference_fingerprint": self._identity("review.quality_score"),
                "requested_model": getattr(
                    getattr(self.client, "routes", {}).get("review.quality_score"), "model", None
                ),
            },
            "estimated_input_tokens": context.estimated_tokens,
            "context_omitted": list(context.omitted),
        }
        if unit.status == "ready" and context.status == "ready":
            request = score_request(context, self.options.rubric_version)
            try:
                raw = self._response(
                    "review.quality_score",
                    request.model_dump(mode="json"),
                    lambda: self.client.evaluate(request, operation="review.quality_score"),
                    kind="judge",
                )
                result = JudgmentResult.model_validate(raw)
                row["dimensions"] = score_dimensions(result, request)
                row["judge"].update(self._judge_identity(result))
                row["status"] = "scored"
            except JudgmentContextOverflow:
                row["status"], row["reason_codes"] = "needs_review", ["context_overflow"]
            except Exception as error:
                row["status"] = "failed"
                row["reason_codes"] = [self._handle_error(error)]
        elif unit.status == "ready":
            row["status"], row["reason_codes"] = "needs_review", ["context_overflow"]
        row["decision"] = route_score(row, self.options)
        return row

    @staticmethod
    def _judge_identity(result: JudgmentResult) -> dict[str, Any]:
        """Keep generated self-reports distinct from provider-native distributions."""
        identity = {
            "model": result.model,
            "model_identity_kind": result.provenance.model_identity,
            "provenance": result.provenance.model_dump(mode="json"),
        }
        if result.provenance.model_identity == "resolved":
            identity["resolved_model"] = result.model
        return identity

    def _generation(
        self,
        unit: QualityUnit,
        context: QualityContext,
        strategy: str,
        *,
        constraints: list[str] | None = None,
    ) -> Any:
        operation = f"review.quality_{strategy}"
        messages = candidate_messages(
            unit, context, strategy, constraints=constraints, version=self.options.rubric_version
        )
        return self._response(
            operation,
            messages,
            lambda: self.client.complete_json(messages, operation=operation),
            kind="generation",
            unit_id=unit.unit_id,
        )

    def _compare(
        self,
        context: QualityContext,
        before: list[str],
        after: list[str],
        unit_id: str,
    ) -> list[dict[str, Any]]:
        # Seed controls the initial blind order. Swapping always reverses the mapping.
        candidate_first = int(digest([self.options.audit.seed, unit_id])[:8], 16) % 2 == 0
        rows = []
        for swapped in range(2 if self.options.comparison.swap_order else 1):
            first = candidate_first != bool(swapped)
            a, b = (after, before) if first else (before, after)
            request = comparison_request(context, a, b, self.options.rubric_version)
            raw = self._response(
                "review.quality_compare",
                request.model_dump(mode="json"),
                lambda: self.client.evaluate(request, operation="review.quality_compare"),
                kind="judge",
            )
            result = JudgmentResult.model_validate(raw)
            result.validate_for(request)
            rows.append(
                {
                    **result.answers["preference"].model_dump(mode="json"),
                    "candidate_label": "a" if first else "b",
                    "swapped": bool(swapped),
                    "seed": self.options.audit.seed,
                    **self._judge_identity(result),
                }
            )
        return rows

    def _candidate_decision(
        self,
        unit: QualityUnit,
        units: list[QualityUnit],
        targets: list[str],
        baseline: dict,
        *,
        origin: str,
        context: QualityContext | None = None,
    ) -> dict[str, Any]:
        candidate = replace(unit, target_parts=tuple(targets), target_hash=digest(targets))
        context = context or self._context(unit, units)
        candidate_context = replace(context, state={**context.state, "target_parts": targets})
        candidate_units = [candidate if value.unit_id == unit.unit_id else value for value in units]
        score = self._score(
            candidate, candidate_units, scope="candidate", context=candidate_context
        )
        comparison_id = digest(
            [
                unit.target_hash,
                candidate.target_hash,
                context.context_hash,
                self.options.model_dump(mode="json"),
                origin,
            ]
        )
        comparisons = []
        try:
            if score["status"] == "scored" and baseline["status"] == "scored":
                comparisons = self._compare(context, list(unit.target_parts), targets, unit.unit_id)
            accepted, reasons = accept_candidate(baseline, score, comparisons, self.options)
        except Exception as error:
            accepted, reasons = False, [self._handle_error(error)]
        decision = {
            "unit_id": unit.unit_id,
            "accepted": accepted,
            "reason_codes": reasons,
            "member_refs": list(unit.segment_refs),
            "members": unit.members,
            "before_hash": unit.target_hash,
            "after_hash": candidate.target_hash,
            "candidate_id": candidate.target_hash,
            "origin": origin,
            "calibration_status": "uncalibrated",
            "decision_ref": f"{self.prefix}/units/{unit.unit_id}/comparisons/{comparison_id}.json",
            "comparisons": comparisons,
            "score": score,
            "targets": targets,
        }
        self.store.write_artifact(decision["decision_ref"], decision)
        generation_operation = {
            "quality_retranslation": "review.quality_retranslate",
            "quality_revision": "review.quality_revise",
            "review_fix": "review.fix",
            "final_issue_fix": "autofix.fix",
        }.get(origin, origin)
        generation_route = getattr(self.client, "routes", {}).get(generation_operation)
        candidate_key = (
            f"{self.prefix}/units/{unit.unit_id}/candidates/{candidate.target_hash}.json"
        )
        previous = self.store.read_artifact(candidate_key) or {}
        record = {
            **decision,
            "before": list(unit.target_parts),
            "after": targets,
            "target_parts": targets,
            "target_hash": candidate.target_hash,
            "strategy": origin,
            "model": getattr(generation_route, "model", None),
            "provider": getattr(generation_route, "provider", None),
            "model_identity_kind": "configured_route",
            "operation": generation_operation,
            "status": "accepted" if accepted else "rejected",
        }
        if previous:
            for key in (
                "origin",
                "before",
                "strategy",
                "model",
                "provider",
                "model_identity_kind",
                "operation",
            ):
                if key in previous:
                    record[key] = previous[key]
        record["decision_refs"] = list(
            dict.fromkeys([*previous.get("decision_refs", []), decision["decision_ref"]])
        )
        record["acceptance_scope"] = origin
        self.store.write_artifact(candidate_key, record)
        return decision

    def _neighbor_check(
        self,
        chapters: list[Chapter],
        before_overrides: Overrides,
        proposed: dict[tuple[int, int], str],
        decisions: list[dict[str, Any]],
    ) -> bool:
        """Check the combined snapshot, including unchanged neighbors, in source order."""
        if not proposed:
            return True
        before = self._units(chapters, before_overrides)
        after = self._units(chapters, {**before_overrides, **proposed})
        before_by_id = {unit.unit_id: unit for unit in before}
        affected = set()
        for i, unit in enumerate(before):
            if any((unit.chapter_index, ti) in proposed for ti in unit.text_indices):
                lo = max(0, i - max(1, self.options.context.following_units))
                hi = min(len(before), i + max(1, self.options.context.preceding_units) + 1)
                affected.update(
                    u.unit_id for u in before[lo:hi] if u.chapter_index == unit.chapter_index
                )
        safe = True
        for unit in after:
            if unit.unit_id not in affected or unit.status != "ready":
                continue
            baseline = next(
                (
                    decision["score"]
                    for decision in decisions
                    if decision.get("accepted")
                    and decision["unit_id"] == unit.unit_id
                    and decision.get("after_hash") == unit.target_hash
                ),
                None,
            )
            safety_expansion = int(
                not self.options.context.preceding_units or not self.options.context.following_units
            )
            if baseline is None:
                prior_unit = before_by_id[unit.unit_id]
                baseline = self._score(
                    prior_unit,
                    before,
                    scope="shadow",
                    context=self._context(prior_unit, before, expansion=safety_expansion),
                )
            result = self._score(
                unit,
                after,
                scope="shadow",
                context=self._context(unit, after, expansion=safety_expansion),
            )
            if (
                baseline["status"] != "scored"
                or result["status"] != "scored"
                or result["decision"]["action"] == "needs_review"
                or critical_regression(baseline, result)
            ):
                safe = False
        if not safe:
            for decision in decisions:
                if decision["accepted"]:
                    decision.update(accepted=False, reason_codes=["combined_neighbor_regression"])
                    self._update_decision(decision)
        return safe

    def _update_decision(self, decision: dict[str, Any]) -> None:
        """Keep the comparison, candidate and selected-decision projections consistent."""
        if "decision_ref" not in decision:
            return
        self.store.write_artifact(decision["decision_ref"], decision)
        unit_path = f"{self.prefix}/units/{decision['unit_id']}"
        key = f"{unit_path}/candidates/{decision['candidate_id']}.json"
        candidate = self.store.read_artifact(key)
        if candidate is not None:
            candidate.update(
                accepted=decision["accepted"],
                reason_codes=decision["reason_codes"],
                status="accepted" if decision["accepted"] else "rejected",
            )
            self.store.write_artifact(key, candidate)
        selected = self.store.read_artifact(f"{unit_path}/decision.json")
        if selected and selected.get("candidate_id") == decision["candidate_id"]:
            self.store.write_artifact(f"{unit_path}/decision.json", decision)

    def gate(
        self,
        chapters: list[Chapter],
        overrides: Overrides,
        proposed: Overrides,
        origin: str = "review_fix",
    ) -> tuple[dict[tuple[int, int], str], list[dict[str, Any]]]:
        """Gate complete logical units for Review/Fixer and final Autofix proposals."""
        if self.options.mode != "optimize" or self.degraded:
            return dict(proposed), []
        units = self._units(chapters, overrides)
        accepted: dict[tuple[int, int], str] = {}
        decisions = []
        for unit in units:
            if not any((unit.chapter_index, index) in proposed for index in unit.text_indices):
                continue
            targets = [
                proposed.get((unit.chapter_index, index), target)
                for index, target in zip(unit.text_indices, unit.target_parts)
            ]
            if any(not isinstance(target, str) or not target.strip() for target in targets):
                decision = {
                    "unit_id": unit.unit_id,
                    "accepted": False,
                    "reason_codes": ["invalid_candidate"],
                    "member_refs": list(unit.segment_refs),
                }
                decisions.append(decision)
                continue
            baseline = self._score(unit, units, scope="shadow")
            decision = self._candidate_decision(unit, units, targets, baseline, origin=origin)
            decisions.append(decision)
            if decision["accepted"]:
                accepted.update(
                    {
                        (unit.chapter_index, index): target
                        for index, target in zip(unit.text_indices, targets)
                        if (unit.chapter_index, index) in proposed
                    }
                )
        if self.degraded:
            # The configured fallback is the existing Review flow at every automatic entry point.
            return dict(proposed), decisions
        self._phase("quality_comparing")
        if not self._neighbor_check(chapters, overrides, accepted, decisions):
            accepted = {}
        if self.degraded:
            return dict(proposed), decisions
        return accepted, decisions

    def prelude(self, chapters: list[Chapter], overrides: Overrides) -> list[dict[str, Any]]:
        if self.options.mode == "off":
            return []
        saved = self.store.read_artifact(f"{self.prefix}/prelude.json")
        if saved is not None and not self._policy_changed:
            return saved.get("patches", [])
        units = self._units(chapters, overrides)
        self._manifest["units"] = [unit.identity() for unit in units]
        self._phase("quality_scoring", 0, len(units))
        scores = {}
        for position, unit in enumerate(units, 1):
            row = self._score(unit, units, scope="formal")
            scores[unit.unit_id] = row
            self.store.write_artifact(f"{self.prefix}/units/{unit.unit_id}/baseline.json", row)
            self.store.write_artifact(f"{self.prefix}/final_scores/formal/{unit.unit_id}.json", row)
            if self.progress:
                self.progress(position, len(units), "quality_scoring")
        if self.options.mode == "observe" or self.degraded:
            self.store.write_artifact(f"{self.prefix}/prelude.json", {"patches": []})
            self._phase("quality_done")
            self._summary(list(scores.values()), "formal")
            return []
        if self._manifest["selected_unit_ids"] is None:
            selected = [
                row
                for row in scores.values()
                if row["decision"]["action"] in {"retranslate", "revise", "needs_review"}
                and row["dimensions"]
            ]
            selected.extend(
                row
                for row in scores.values()
                if row["decision"]["action"] == "keep" and audited(row["unit_id"], self.options)
            )
            selected.sort(key=priority)
            self._manifest["selected_unit_ids"] = [
                row["unit_id"] for row in selected[: self.options.max_units_to_optimize_per_run]
            ]
            self._save_manifest()
        by_id = {unit.unit_id: unit for unit in units}
        proposed: dict[tuple[int, int], str] = {}
        decisions = []
        self._phase("quality_generating")
        for unit_id in self._manifest["selected_unit_ids"]:
            if self.degraded:
                break
            unit, baseline = by_id[unit_id], scores[unit_id]
            context = self._context(unit, units)
            action = baseline["decision"]["action"]
            constraints = []
            try:
                if action == "needs_review":
                    # Additional context never truncates core evidence; stronger generation
                    # checks can request review but cannot override an uncertain judge alone.
                    for expansion in range(1, self.options.max_context_expansions + 1):
                        expanded = self._context(unit, units, expansion=expansion)
                        baseline = self._score(unit, units, scope="formal", context=expanded)
                        if baseline["decision"]["action"] != "needs_review":
                            context = expanded
                            action = baseline["decision"]["action"]
                            break
                    if action == "needs_review":
                        if self.options.max_verifications_per_unit:
                            verification = validate_diagnosis(
                                self._generation(unit, context, "verify")
                            )
                            self.store.write_artifact(
                                f"{self.prefix}/units/{unit_id}/verification.json",
                                {**verification, "origin": "review.quality_verify"},
                            )
                        continue
                diagnosis = validate_diagnosis(self._generation(unit, context, "diagnose"))
                self.store.write_artifact(
                    f"{self.prefix}/units/{unit_id}/diagnosis.json",
                    {**diagnosis, "origin": "review.quality_diagnose"},
                )
                if diagnosis["status"] != "confirmed_issue":
                    continue
                constraints = diagnosis["constraints"]
                strategies = (
                    ["revise", "retranslate"] if action == "revise" else ["retranslate", "revise"]
                )
                winners = []
                seen = {unit.target_hash}
                for strategy in strategies[: self.options.max_candidates_per_unit]:
                    raw = self._generation(unit, context, strategy, constraints=constraints)
                    targets = validate_candidate(raw, unit)
                    candidate_hash = digest(targets)
                    if candidate_hash in seen:
                        continue
                    seen.add(candidate_hash)
                    decision = self._candidate_decision(
                        unit,
                        units,
                        targets,
                        baseline,
                        origin="quality_retranslation"
                        if strategy == "retranslate"
                        else "quality_revision",
                        context=context,
                    )
                    decisions.append(decision)
                    if decision["accepted"]:
                        winners.append(decision)
                if not winners:
                    continue
                winner = winners[0]
                if len(winners) > 1:
                    # Both already defeated the original; compare them without any origin labels.
                    comparisons = self._compare(
                        context, winner["targets"], winners[1]["targets"], unit_id
                    )
                    supports = all(
                        row["choice"]
                        == ("a_better" if row["candidate_label"] == "a" else "b_better")
                        and row["confidence"] >= self.options.thresholds.min_confidence_to_act
                        and row["probabilities"][row["choice"]]
                        >= self.options.thresholds.min_pairwise_support
                        for row in comparisons
                    )
                    supports_first = all(
                        row["choice"]
                        == ("b_better" if row["candidate_label"] == "a" else "a_better")
                        and row["confidence"] >= self.options.thresholds.min_confidence_to_act
                        and row["probabilities"][row["choice"]]
                        >= self.options.thresholds.min_pairwise_support
                        for row in comparisons
                    )
                    self.store.write_artifact(
                        f"{self.prefix}/units/{unit_id}/candidate_comparison.json", comparisons
                    )
                    if not supports and not supports_first:
                        for alternative in winners:
                            alternative.update(
                                accepted=False, reason_codes=["candidate_preference_conflict"]
                            )
                            self._update_decision(alternative)
                        continue
                    if supports:
                        winner = winners[1]
                for alternative in winners:
                    if alternative is not winner:
                        alternative.update(
                            accepted=False, reason_codes=["alternative_not_selected"]
                        )
                        self._update_decision(alternative)
                proposed.update(
                    {
                        (unit.chapter_index, index): target
                        for index, target in zip(unit.text_indices, winner["targets"])
                    }
                )
                self.store.write_artifact(f"{self.prefix}/units/{unit_id}/decision.json", winner)
            except Exception as error:
                self._handle_error(error)
        self._phase("quality_comparing")
        if self.degraded or not self._neighbor_check(chapters, overrides, proposed, decisions):
            proposed = {}
            for decision in decisions:
                if decision.get("accepted"):
                    decision.update(accepted=False, reason_codes=["quality_degraded"])
                    self._update_decision(decision)
        patches = []
        for unit in units:
            if not any((unit.chapter_index, index) in proposed for index in unit.text_indices):
                continue
            decision = self.store.read_artifact(f"{self.prefix}/units/{unit.unit_id}/decision.json")
            for index, before in zip(unit.text_indices, unit.target_parts):
                after = proposed[(unit.chapter_index, index)]
                if before == after:
                    continue
                before_hash = hashlib.sha256(before.encode()).hexdigest()
                patches.append(
                    {
                        "patch_id": "quality-" + digest([unit.unit_id, index, before, after])[:20],
                        "round": 0,
                        "chapter": unit.chapter_index,
                        "index": index,
                        "segment_ref": f"ch{unit.chapter_index}:p{index}",
                        "before": before,
                        "after": after,
                        "before_hash": before_hash,
                        "after_hash": hashlib.sha256(after.encode()).hexdigest(),
                        "issue_ids": [],
                        "issue_keys": [],
                        "status": "accepted_by_comparison",
                        "origin": decision["origin"],
                        "quality_unit_id": unit.unit_id,
                        "quality_decision_ref": decision["decision_ref"],
                        "member_refs": list(unit.segment_refs),
                    }
                )
        self._phase("quality_overlay_committing")
        self.store.write_artifact(f"{self.prefix}/prelude.json", {"patches": patches})
        self._phase("quality_done")
        self._summary(list(scores.values()), "formal")
        return patches

    def final_scores(
        self,
        chapters: list[Chapter],
        overrides: Overrides,
        scope: str = "shadow",
    ) -> dict[str, Any]:
        """Refresh actual text/context snapshots, keeping formal and shadow views separate."""
        if self.options.mode == "off":
            return {}
        if scope not in {"formal", "shadow"}:
            raise ValueError("Quality score scope must be formal or shadow")
        self._phase("quality_final_scoring")
        units, formal = self._units(chapters, overrides), self._units(chapters, {})
        formal_by_id = {unit.unit_id: unit for unit in formal}
        rows = []
        for unit in units:
            row = self._score(unit, units, scope=scope)
            formal_unit = formal_by_id[unit.unit_id]
            row["formal_base_hash"] = formal_unit.target_hash
            row["formal_base_context_hash"] = self._context(formal_unit, formal).context_hash
            decision = self.store.read_artifact(f"{self.prefix}/units/{unit.unit_id}/decision.json")
            if scope == "formal":
                index = (
                    self.store.read_artifact(f"reviews/{self.review_id}/autofix/index.json") or {}
                )
                locations = [
                    location
                    for location in index.get("locations", [])
                    if location.get("chapter") == unit.chapter_index
                    and location.get("index") in unit.text_indices
                ]
                if locations:
                    actual = dict(zip(unit.text_indices, unit.target_parts))
                    if all(
                        location.get("status") in {"applied", "no_net_change"}
                        and actual[location["index"]] == location.get("target")
                        for location in locations
                    ):
                        row["publication_status"] = "published"
                    else:
                        row["publication_status"] = "failed"
            if decision:
                row["decision"] = {
                    key: value
                    for key, value in decision.items()
                    if key not in {"score", "comparisons", "targets"}
                }
            self.store.write_artifact(
                f"{self.prefix}/final_scores/{scope}/{unit.unit_id}.json", row
            )
            rows.append(row)
        return self._summary(rows, scope)

    def _summary(self, rows: list[dict], scope: str) -> dict[str, Any]:
        summary = self.store.read_artifact(f"{self.prefix}/summary.json") or {}
        candidates = self.store.list_artifacts(f"{self.prefix}/units/")
        candidate_rows = [
            self.store.read_artifact(key) for key in candidates if "/candidates/" in key
        ]
        requests = list(self._manifest["requests"].values())
        values = {
            "logical_units": len(rows),
            "internal_segments": sum(len(row["members"]) for row in rows),
            "scored": sum(row["status"] == "scored" for row in rows),
            "failed": sum(row["status"] == "failed" for row in rows),
            "not_applicable": sum(row["status"] == "not_applicable" for row in rows),
            "needs_review": sum(row["decision"].get("action") == "needs_review" for row in rows),
            "candidates_generated": sum(
                request["attempts"]
                for request in requests
                if request["operation"] in {"review.quality_retranslate", "review.quality_revise"}
            ),
            "candidates_compared": len(candidate_rows),
            "accepted": sum(bool(row.get("accepted")) for row in candidate_rows),
            "rejected": sum(not row.get("accepted") for row in candidate_rows),
            "published": sum(row.get("publication_status") == "published" for row in rows),
            "cache_reused": self._manifest["cache_reused"],
            "judge_requests": sum(row["attempts"] for row in requests if row["kind"] == "judge"),
            "generation_requests": sum(
                row["attempts"] for row in requests if row["kind"] == "generation"
            ),
        }
        summary.update(
            schema_version=1,
            mode=self.options.mode,
            status="degraded"
            if self.degraded
            else "incomplete"
            if self._manifest["incomplete"]
            else "completed",
            calibration_status="uncalibrated",
        )
        summary[scope] = values
        self.store.write_artifact(f"{self.prefix}/summary.json", summary)
        self._save_manifest()
        return summary
