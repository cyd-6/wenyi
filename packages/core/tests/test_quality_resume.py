"""Offline request reuse, budgets, full-array proposals and combined-neighbor gates."""

import json

import pytest
from wenyi_core.config import Config
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.llm.base import LLMClient
from wenyi_core.llm.judgments import (
    ChoiceAnswer,
    JudgmentResult,
    JudgmentUsage,
    ScoreAnswer,
)
from wenyi_core.pipeline.quality_workflow import QualityWorkflow
from wenyi_core.storage.file import FileStorage


class QualityFake(LLMClient):
    def __init__(self, *, reject_comparison=False, conflict=False):
        super().__init__()
        self.calls = []
        self.reject_comparison = reject_comparison
        self.conflict = conflict

    def inference_fingerprint(self, operation):
        return "test-model-v1:" + operation

    def evaluate(self, request, *, operation):
        self.calls.append((operation, request))
        answers = {}
        if operation == "review.quality_score":
            target = "".join(request.state["target_parts"])
            improved = "good" in target
            combined_conflict = (
                self.conflict
                and improved
                and any("good" in "".join(n["target_parts"]) for n in request.state["neighbors"])
            )
            for name, question in request.questions.items():
                score = (
                    (1 if combined_conflict else 4 if improved else 1) if name == "adequacy" else 3
                )
                answers[name] = ScoreAnswer(
                    score=float(score),
                    legend={str(i): criterion for i, criterion in enumerate(question.criteria)},
                    probabilities={str(i): float(i == score) for i in range(5)},
                    confidence=0.95,
                )
        else:
            choice = (
                "equivalent"
                if self.reject_comparison
                else "a_better"
                if "good" in "".join(request.state["a"])
                else "b_better"
            )
            answers["preference"] = ChoiceAnswer(
                choice=choice,
                confidence=0.95,
                probabilities={
                    name: float(name == choice) for name in request.questions["preference"].criteria
                },
            )
        return JudgmentResult(
            model="jev-1.13.0",
            answers=answers,
            usage=JudgmentUsage(input_tokens=10, output_tokens=2),
        )

    def complete(self, messages, *, operation, json_mode=False, max_tokens=None):
        self.calls.append((operation, messages))
        state = json.loads(messages[1]["content"])
        if operation in {"review.quality_diagnose", "review.quality_verify"}:
            return json.dumps({"status": "confirmed_issue", "constraints": ["Preserve negation."]})
        return json.dumps(
            {
                "members": state["members"],
                "targets": ["good " + str(i) for i in range(len(state["source_parts"]))],
            }
        )


def project(tmp_path, *, mode="optimize", segments=None, client=None, **settings):
    chapters = [
        Chapter(
            index=0,
            segments=segments
            or [
                Segment(index=20, source="Not yet.", target="bad 0", anchor="stable"),
                Segment(
                    index=41,
                    source=" He paused.",
                    target="bad 1",
                    cont=True,
                    meta={"babeldoc_id": "stable-id"},
                ),
            ],
        )
    ]
    store = FileStorage(str(tmp_path))
    store.save_manifest({"format": "epub", "chapters": [{"index": 0, "status": "done"}]})
    store.save_chapter(chapters[0])
    config = Config(
        source_lang="en",
        pipeline={"quality": {"mode": mode, "audit": {"sample_rate": 0}, **settings}},
    )
    client = client or QualityFake()
    flow = QualityWorkflow(config, client, store, "review-quality-test", [], {})
    return chapters, store, config, client, flow


def test_off_no_requests_no_artifacts(tmp_path):
    chapters, store, _, client, flow = project(tmp_path, mode="off")
    assert flow.prelude(chapters, {}) == []
    assert flow.final_scores(chapters, {}) == {}
    assert client.calls == []
    assert not store.list_artifacts(flow.prefix)


def test_observe_scores_all_units_without_generation_or_formal_mutation(tmp_path):
    chapters, store, _, client, flow = project(tmp_path, mode="observe")
    before = store.load_chapter(0).model_dump()
    assert flow.prelude(chapters, {}) == []
    assert [operation for operation, _ in client.calls] == ["review.quality_score"]
    assert store.load_chapter(0).model_dump() == before
    keys = store.list_artifacts(flow.prefix)
    assert any("/units/ch0:text0:seg20/baseline.json" in key for key in keys)
    assert not any("/rounds/" in key for key in keys)
    row = store.read_artifact(f"{flow.prefix}/units/ch0:text0:seg20/baseline.json")
    assert row["members"] == [
        {"text_index": 0, "segment_index": 20},
        {"text_index": 1, "segment_index": 41},
    ]
    assert row["dimensions"]["terminology"]["status"] == "not_applicable"


def test_complete_unit_accepted_and_resume_reuses_every_response(tmp_path):
    chapters, store, config, client, flow = project(tmp_path)
    patches = flow.prelude(chapters, {})
    assert len(patches) == 2
    assert {patch["quality_unit_id"] for patch in patches} == {"ch0:text0:seg20"}
    assert [patch["after"] for patch in patches] == ["good 0", "good 1"]
    assert all(patch["issue_ids"] == [] for patch in patches)
    assert len([call for call in client.calls if call[0] == "review.quality_retranslate"]) == 1
    assert len([call for call in client.calls if call[0] == "review.quality_revise"]) == 1
    assert store.load_chapter(0).segments[1].meta == {"babeldoc_id": "stable-id"}
    resumed = QualityFake()
    flow2 = QualityWorkflow(config, resumed, store, flow.review_id, [])
    assert flow2.prelude(chapters, {}) == patches
    assert resumed.calls == []
    flow2.final_scores(chapters, {}, "formal")
    assert resumed.calls == []


@pytest.mark.parametrize("stage", ["baseline", "candidate", "decision", "overlay"])
def test_interrupt_after_durable_response_does_not_repurchase(tmp_path, monkeypatch, stage):
    chapters, store, config, client, flow = project(tmp_path)
    original = store.write_artifact
    interrupted = False
    marker = {
        "baseline": "/baseline.json",
        "candidate": "/candidates/",
        "decision": "/decision.json",
        "overlay": "/prelude.json",
    }[stage]

    def stop(key, value):
        nonlocal interrupted
        original(key, value)
        if marker in key and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(store, "write_artifact", stop)
    with pytest.raises(KeyboardInterrupt):
        flow.prelude(chapters, {})
    monkeypatch.setattr(store, "write_artifact", original)
    requested = len(client.calls)
    resumed = QualityFake()
    output = QualityWorkflow(config, resumed, store, flow.review_id, []).prelude(chapters, {})
    assert len(output) == 2
    fresh_chapters, _, _, fresh_client, fresh_flow = project(tmp_path / "fresh")
    fresh_flow.prelude(fresh_chapters, {})
    assert requested + len(resumed.calls) == len(fresh_client.calls)


def test_tie_retains_original(tmp_path):
    chapters, _, _, _, flow = project(tmp_path, client=QualityFake(reject_comparison=True))
    assert flow.prelude(chapters, {}) == []


def test_budget_counts_all_judge_requests_and_marks_incomplete(tmp_path):
    chapters, store, _, client, flow = project(tmp_path, max_judge_requests_per_run=1)
    assert flow.prelude(chapters, {}) == []
    assert len([call for call in client.calls if call[0].endswith(("score", "compare"))]) == 1
    assert store.read_artifact(f"{flow.prefix}/summary.json")["status"] == "incomplete"


def test_combined_neighbor_conflict_rejects_both_candidates(tmp_path):
    segments = [Segment(index=i, source=f"Not {i}.", target=f"bad {i}") for i in range(2)]
    chapters, _, _, _, flow = project(
        tmp_path, segments=segments, client=QualityFake(conflict=True)
    )
    assert flow.prelude(chapters, {}) == []


def test_gate_expands_single_review_fix_to_complete_unit_comparison(tmp_path):
    chapters, _, _, client, flow = project(tmp_path)
    accepted, decisions = flow.gate(chapters, {}, {(0, 0): "good replacement"})
    assert accepted == {(0, 0): "good replacement"}
    assert decisions[0]["members"] == [
        {"text_index": 0, "segment_index": 20},
        {"text_index": 1, "segment_index": 41},
    ]
    comparisons = [
        request for operation, request in client.calls if operation == "review.quality_compare"
    ]
    assert len(comparisons[0].state["a"]) == len(comparisons[0].state["b"]) == 2


def test_shadow_and_formal_scores_bound_to_distinct_target_and_context_hashes(tmp_path):
    chapters, store, _, _, flow = project(tmp_path)
    patches = flow.prelude(chapters, {})
    overrides = {(patch["chapter"], patch["index"]): patch["after"] for patch in patches}
    flow.final_scores(chapters, overrides, "shadow")
    flow.final_scores(chapters, {}, "formal")
    key = f"{flow.prefix}/final_scores"
    formal = store.read_artifact(f"{key}/formal/ch0:text0:seg20.json")
    shadow = store.read_artifact(f"{key}/shadow/ch0:text0:seg20.json")
    assert formal["target_hash"] != shadow["target_hash"]
    assert formal["target_hash"] == shadow["formal_base_hash"]


def test_continue_review_degrades_all_quality_and_restores_legacy_gate(tmp_path):
    class Broken(QualityFake):
        def evaluate(self, request, *, operation):
            raise ValueError("private source should not be logged")

    chapters, store, _, _, flow = project(tmp_path, client=Broken())
    assert flow.prelude(chapters, {}) == []
    proposed = {(0, 0): "existing review fix"}
    assert flow.gate(chapters, {}, proposed)[0] == proposed
    assert flow.degraded
    response = store.read_artifact(
        next(key for key in store.list_artifacts(flow.prefix) if "/responses/" in key)
    )
    assert "private source" not in json.dumps(response)
    assert store.read_artifact(f"{flow.prefix}/summary.json")["status"] == "degraded"


def test_stop_propagates_error_after_saving_response(tmp_path):
    class Broken(QualityFake):
        def evaluate(self, request, *, operation):
            raise ValueError("protocol failure")

    chapters, store, _, _, flow = project(tmp_path, client=Broken(), on_error="stop")
    with pytest.raises(ValueError, match="protocol"):
        flow.prelude(chapters, {})
    assert any("/responses/" in key for key in store.list_artifacts(flow.prefix))


def test_interrupted_request_is_ambiguous_and_can_be_retried(tmp_path):
    class Interrupted(QualityFake):
        def evaluate(self, request, *, operation):
            raise KeyboardInterrupt()

    chapters, store, config, _, flow = project(tmp_path, client=Interrupted())
    with pytest.raises(KeyboardInterrupt):
        flow.prelude(chapters, {})
    key = next(key for key in store.list_artifacts(flow.prefix) if "/responses/" in key)
    assert store.read_artifact(key)["status"] == "ambiguous"
    resumed = QualityWorkflow(config, QualityFake(), store, flow.review_id, [])
    assert len(resumed.prelude(chapters, {})) == 2
    manifest = store.read_artifact(f"{flow.prefix}/manifest.json")
    assert any(request["ambiguous_previous_attempt"] for request in manifest["requests"].values())


def test_exhausted_transient_failure_honors_continue_review(tmp_path):
    class Transient(QualityFake):
        def evaluate(self, request, *, operation):
            raise TimeoutError("timed out after shared retry budget")

    chapters, _, _, _, flow = project(tmp_path, client=Transient())
    assert flow.prelude(chapters, {}) == []
    assert flow.degraded


def test_combined_rejection_updates_all_display_projections(tmp_path):
    segments = [Segment(index=i, source=f"Not {i}.", target=f"bad {i}") for i in range(2)]
    chapters, store, _, _, flow = project(
        tmp_path, segments=segments, client=QualityFake(conflict=True)
    )
    assert flow.prelude(chapters, {}) == []
    for key in store.list_artifacts(flow.prefix):
        if "/candidates/" in key or key.endswith("/decision.json"):
            row = store.read_artifact(key)
            assert row["accepted"] is False
            assert row["reason_codes"] == ["combined_neighbor_regression"]
    assert store.read_artifact(f"{flow.prefix}/summary.json")["formal"]["accepted"] == 0


def test_postpublication_score_tracks_actual_formal_members(tmp_path):
    chapters, store, _, _, flow = project(tmp_path)
    patches = flow.prelude(chapters, {})
    locations = [
        {
            "chapter": patch["chapter"],
            "index": patch["index"],
            "target": patch["after"],
            "status": "applied",
        }
        for patch in patches
    ]
    store.write_artifact(f"reviews/{flow.review_id}/autofix/index.json", {"locations": locations})
    for patch in patches:
        chapters[0].text_segments[patch["index"]].target = patch["after"]
    summary = flow.final_scores(chapters, {}, "formal")
    assert summary["formal"]["published"] == 1
    chapters[0].text_segments[0].target = "manual edit"
    summary = flow.final_scores(chapters, {}, "formal")
    assert summary["formal"]["published"] == 0


def test_threshold_change_reuses_raw_pinned_score_across_runs(tmp_path):
    from types import SimpleNamespace

    chapters, store, config, client, flow = project(tmp_path, mode="observe")
    client.routes = {"review.quality_score": SimpleNamespace(model="jev-1.13.0")}
    flow.prelude(chapters, {})
    resumed = QualityFake()
    resumed.routes = client.routes
    config.pipeline.quality.thresholds.adequacy_min = 0
    flow2 = QualityWorkflow(config, resumed, store, "review-threshold-change", [])
    flow2.prelude(chapters, {})
    assert resumed.calls == []
    row = store.read_artifact(f"{flow2.prefix}/units/ch0:text0:seg20/baseline.json")
    assert row["judge"]["resolved_model"] == "jev-1.13.0"


def test_response_and_usage_commit_use_same_callback(tmp_path):
    chapters, store, config, client, flow = project(tmp_path, mode="observe")
    receipts = []

    def commit(artifacts):
        receipts.append(artifacts)
        for key, value in artifacts.items():
            assert "/quality/responses/" in key
            store.write_artifact(key, value)

    flow = QualityWorkflow(config, client, store, flow.review_id, [], flush_usage=commit)
    flow.prelude(chapters, {})
    assert len(receipts) == 1
    response = next(iter(receipts[0].values()))
    assert response["response"]["usage"]["input_tokens"] == 10


def test_no_confirmed_issue_never_forces_candidate_generation(tmp_path):
    class NoIssue(QualityFake):
        def complete(self, messages, *, operation, json_mode=False, max_tokens=None):
            assert operation == "review.quality_diagnose"
            self.calls.append((operation, messages))
            return json.dumps({"status": "no_confirmed_issue", "constraints": []})

    chapters, _, _, client, flow = project(tmp_path, client=NoIssue())
    assert flow.prelude(chapters, {}) == []
    assert not any(operation.endswith(("retranslate", "revise")) for operation, _ in client.calls)


def test_uncertain_candidate_tournament_keeps_original(tmp_path):
    class Distinct(QualityFake):
        def complete(self, messages, *, operation, json_mode=False, max_tokens=None):
            raw = super().complete(
                messages, operation=operation, json_mode=json_mode, max_tokens=max_tokens
            )
            value = json.loads(raw)
            if operation == "review.quality_revise":
                value["targets"] = [target + " revised" for target in value["targets"]]
            return json.dumps(value)

    chapters, _, _, _, flow = project(tmp_path, client=Distinct())
    # Both candidates beat the bad original; the mock's always-first preference conflicts
    # when comparing two good candidates in swapped orders.
    assert flow.prelude(chapters, {}) == []


def test_mineru_empty_result_remains_not_applicable_without_judge_call(tmp_path):
    segments = [Segment(index=0, source="image caption", target="")]
    chapters, store, config, client, flow = project(tmp_path, mode="observe", segments=segments)
    store.save_manifest({"format": "pdf", "pdf_backend": "mineru"})
    flow = QualityWorkflow(config, client, store, flow.review_id, [])
    flow.prelude(chapters, {})
    assert client.calls == []
    row = store.read_artifact(f"{flow.prefix}/units/ch0:text0:seg0/baseline.json")
    assert row["status"] == "not_applicable"
    assert row["reason_codes"] == ["empty_target"]


def test_neighbor_gate_still_checks_adjacency_when_scoring_context_is_disabled(tmp_path):
    segments = [Segment(index=i, source=f"Not {i}.", target=f"bad {i}") for i in range(2)]
    chapters, _, _, _, flow = project(
        tmp_path,
        segments=segments,
        client=QualityFake(conflict=True),
        context={"preceding_units": 0, "following_units": 0},
    )
    assert flow.prelude(chapters, {}) == []


class AuthorizationFailure(Exception):
    status_code = 401


class UnauthorizedQualityFake(QualityFake):
    def evaluate(self, request, *, operation):
        self.calls.append((operation, request))
        raise AuthorizationFailure("private server response must stay out of artifacts")


def test_fresh_workflow_can_retry_saved_failure_without_repeating_in_same_run(tmp_path):
    chapters, store, config, client, flow = project(
        tmp_path, mode="observe", client=UnauthorizedQualityFake(), on_error="stop"
    )
    with pytest.raises(AuthorizationFailure):
        flow.prelude(chapters, {})
    with pytest.raises(ValueError):
        flow.prelude(chapters, {})
    assert len(client.calls) == 1
    resumed = QualityFake()
    fresh = QualityWorkflow(config, resumed, store, flow.review_id, [])
    assert fresh.prelude(chapters, {}) == []
    assert len(resumed.calls) == 1
    key = next(key for key in store.list_artifacts(flow.prefix) if "/responses/" in key)
    record = store.read_artifact(key)
    assert record["status"] == "completed"
    assert record["attempt_history"][0]["error_type"] == "AuthorizationFailure"
    assert "private server" not in json.dumps(record)
    manifest = store.read_artifact(f"{flow.prefix}/manifest.json")
    assert next(iter(manifest["requests"].values()))["attempts"] == 2


def test_explicit_failure_resumes_remain_within_shared_quality_request_limit(tmp_path):
    chapters, store, config, client, flow = project(
        tmp_path,
        mode="observe",
        client=UnauthorizedQualityFake(),
        on_error="stop",
        max_judge_requests_per_run=2,
    )
    with pytest.raises(AuthorizationFailure):
        flow.prelude(chapters, {})
    repeated = UnauthorizedQualityFake()
    with pytest.raises(AuthorizationFailure):
        QualityWorkflow(config, repeated, store, flow.review_id, []).prelude(chapters, {})
    assert len(repeated.calls) == 1
    healthy = QualityFake()
    with pytest.raises(ValueError, match="budget_exhausted"):
        QualityWorkflow(config, healthy, store, flow.review_id, []).prelude(chapters, {})
    assert healthy.calls == []


class GeneratedQualityFake(QualityFake):
    """Return uncalibrated generated judgments through the shared result contract."""

    def evaluate(self, request, *, operation):
        result = super().evaluate(request, operation=operation).model_dump(mode="json")
        result["model"] = "local-judge-1.2.3"
        result["provenance"] = {
            "source": "generated",
            "confidence": "self_reported",
            "probabilities": "self_reported",
            "model_identity": "requested",
        }
        return result


def test_generated_judge_provenance_survives_scores_comparisons_and_resume(tmp_path):
    chapters, store, config, client, flow = project(tmp_path, client=GeneratedQualityFake())
    patches = flow.prelude(chapters, {})
    assert len(patches) == 2
    row = store.read_artifact(f"{flow.prefix}/units/ch0:text0:seg20/baseline.json")
    assert row["judge"]["provenance"]["source"] == "generated"
    assert row["judge"]["model"] == "local-judge-1.2.3"
    assert row["judge"]["model_identity_kind"] == "requested"
    assert "resolved_model" not in row["judge"]
    decision = store.read_artifact(patches[0]["quality_decision_ref"])
    assert all(
        comparison["provenance"]["probabilities"] == "self_reported"
        and comparison["model"] == "local-judge-1.2.3"
        and "resolved_model" not in comparison
        for comparison in decision["comparisons"]
    )
    resumed = GeneratedQualityFake()
    fresh = QualityWorkflow(config, resumed, store, flow.review_id, [])
    assert fresh.prelude(chapters, {}) == patches
    fresh.final_scores(chapters, {}, "formal")
    assert resumed.calls == []
    final = store.read_artifact(f"{flow.prefix}/final_scores/formal/ch0:text0:seg20.json")
    assert final["judge"]["provenance"] == row["judge"]["provenance"]


def test_generated_judgments_do_not_claim_pinned_native_cache_reuse(tmp_path):
    from types import SimpleNamespace

    client = GeneratedQualityFake()
    client.routes = {"review.quality_score": SimpleNamespace(model="local-judge-1.2.3")}
    chapters, store, config, _, flow = project(tmp_path, mode="observe", client=client)
    flow.prelude(chapters, {})
    assert not flow.degraded
    resumed = GeneratedQualityFake()
    resumed.routes = client.routes
    fresh = QualityWorkflow(config, resumed, store, "review-generated-next", [])
    fresh.prelude(chapters, {})
    assert len(resumed.calls) == 1
    assert not fresh.degraded


@pytest.mark.parametrize("mode", ["observe", "optimize"])
def test_deepseek_route_drives_quality_without_jev_credentials(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace as NS

    from wenyi_core.llm.configuration import LLMConfig
    from wenyi_core.llm.judgments import JudgmentRequest
    from wenyi_core.llm.operations import configured_operations
    from wenyi_core.llm.router import RoutedLLMClient

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-token")
    chapters, store, config, _, initial = project(tmp_path, mode=mode, on_error="stop")
    config.llm = LLMConfig.model_validate(
        {
            "preset": "deepseek",
            "routes": {"review.quality_score": {"tier": "cheap"}},
        }
    )
    client = RoutedLLMClient(config.llm)
    client.validate_credentials(configured_operations(config, "review"))
    fake, requests = QualityFake(), []

    def generate(**kwargs):
        requests.append(kwargs)
        assert kwargs["response_format"] == {"type": "json_object"}
        messages = [dict(message) for message in kwargs["messages"]]
        messages[1]["content"] = messages[1]["content"].removesuffix(
            "\n\nOutput must be valid json."
        )
        value = json.loads(messages[1]["content"])
        if "questions" in value:
            assert kwargs["max_tokens"] > 0
            request = JudgmentRequest.model_validate(value)
            operation = (
                "review.quality_compare"
                if "preference" in request.questions
                else "review.quality_score"
            )
            result = fake.evaluate(request, operation=operation)
            answers = {}
            for name, answer in result.answers.items():
                compact = answer.model_dump(mode="json")
                compact.pop("score", None)
                compact.pop("legend", None)
                answers[name] = compact
            content = json.dumps({"answers": answers})
        else:
            # The candidate fixture has the same bounded result for both strategies.
            operation = (
                "review.quality_diagnose"
                if "confirmed_issue" in messages[0]["content"]
                else "review.quality_retranslate"
            )
            content = fake.complete(messages, operation=operation, json_mode=True)
        return NS(
            choices=[NS(message=NS(content=content), finish_reason="stop")],
            usage=NS(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        )

    client.adapter("default")._client = NS(chat=NS(completions=NS(create=generate)))
    flow = QualityWorkflow(config, client, store, initial.review_id, [])
    before = store.load_chapter(0).model_dump()
    patches = flow.prelude(chapters, {})
    assert not flow.degraded
    assert len(patches) == (2 if mode == "optimize" else 0)
    assert store.load_chapter(0).model_dump() == before
    row = store.read_artifact(f"{flow.prefix}/units/ch0:text0:seg20/baseline.json")
    assert row["judge"]["provenance"]["source"] == "generated"
    assert row["judge"]["provenance"]["confidence"] == "self_reported"
    assert row["status"] == "scored"
    assert client.usage_summary()["totals"]["calls"] == len(requests)
    assert client.usage_summary()["totals"]["total_tokens"] == len(requests) * 10
    count = len(requests)
    resumed = QualityWorkflow(config, client, store, flow.review_id, [])
    assert resumed.prelude(chapters, {}) == patches
    resumed.final_scores(chapters, {}, "formal")
    assert len(requests) == count
    assert client.usage_summary()["totals"]["calls"] == count
