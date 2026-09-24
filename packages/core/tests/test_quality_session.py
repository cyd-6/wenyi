"""Full offline Review/Autofix integration and journal recovery for quality modes."""

from pathlib import Path
from unittest.mock import patch

import pytest
from wenyi_core.config import Config
from wenyi_core.llm.usage import UsageSample
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.review.run_store import ReviewRunStore

from tests.test_orchestrator import _review_json
from tests.test_quality_resume import QualityFake, project
from tests.test_review_autofix import _agent_final, _fix_json
from tests.test_typesafe_provider import graph


class SessionFake(QualityFake):
    def evaluate(self, request, *, operation):
        result = super().evaluate(request, operation=operation)
        self.usage.record(
            "judge", result.usage.sample(), stage=operation, provider="judge", model=result.model
        )
        return result

    def complete(self, messages, *, operation, json_mode=False, max_tokens=None):
        self.usage.record(
            "generation", UsageSample(5, 3, 8), stage=operation, provider="fake", model="fake"
        )
        if operation.startswith("review.quality_"):
            return super().complete(messages, operation=operation, json_mode=json_mode)
        self.calls.append((operation, messages))
        if operation == "review.scan":
            return _review_json(messages[-1]["content"], [])
        raise AssertionError(operation)


def session(tmp_path, mode="optimize", autofix=False):
    chapters, store, _, _, _ = project(tmp_path)
    config = Config(
        source_lang="en",
        llm=graph(),
        pipeline={
            "quality": {"mode": mode, "audit": {"sample_rate": 0}},
            "review_autofix": autofix,
            "review_agent_loop": False,
            "review_fix_loop": False,
            "review_conflict_arbitration": False,
        },
    )
    client = SessionFake()
    return Orchestrator(config, client), store, client, chapters


@pytest.mark.parametrize("mode", ["off", "observe", "optimize"])
def test_modes_preserve_formal_text_until_explicit_publication(tmp_path, mode):
    orch, store, client, chapters = session(tmp_path, mode)
    before = store.load_chapter(0).model_dump()
    result = orch._review.run_session(store, [])
    assert store.load_chapter(0).model_dump() == before
    assert sum(op == "review.scan" for op, _ in client.calls) == 1
    quality_calls = [op for op, _ in client.calls if op.startswith("review.quality_")]
    if mode == "off":
        assert quality_calls == []
        assert result.changes == []
        assert "quality" not in result.result
    elif mode == "observe":
        assert set(quality_calls) == {"review.quality_score"}
        assert result.changes == []
    else:
        assert len(result.changes) == 2
        assert result.changes[0]["review_result"] == "accepted_by_comparison"
        assert result.changes[0]["provenance"][0]["blind_review_checked"] == 1
    assert not Path(store.run_dir, "usage-pending.json").exists()


@pytest.mark.parametrize("after", [False, True])
def test_quality_overlay_checkpoint_resume_keeps_decisions_and_usage(tmp_path, after):
    orch, store, first, chapters = session(tmp_path)
    original = ReviewRunStore.save_checkpoint
    interrupted = False

    def save(debug, value):
        nonlocal interrupted
        if not interrupted and value["phase"] == "quality_done":
            interrupted = True
            if after:
                original(debug, value)
            raise KeyboardInterrupt
        return original(debug, value)

    with patch.object(ReviewRunStore, "save_checkpoint", save), pytest.raises(KeyboardInterrupt):
        orch._review.run_session(store, [])
    before_usage = store.load_usage()["totals"]["calls"]
    resumed = Orchestrator(orch._runtime.config, SessionFake())
    result = resumed._review.run_session(store, [])
    assert len(result.changes) == 2
    assert all(op == "review.scan" for op, _ in resumed._runtime.client.calls)
    assert store.load_usage()["totals"]["calls"] == before_usage + 1
    assert [s.target for s in store.load_chapter(0).segments] == ["bad 0", "bad 1"]


def test_publish_refreshes_formal_scores_and_preserves_metadata(tmp_path):
    orch, store, _, chapters = session(tmp_path, autofix=True)
    before = store.load_chapter(0)
    result = orch._review.run_session(store, [])
    published = orch._review_autofix.run(store, result, [])
    after = store.load_chapter(0)
    assert [s.target for s in after.segments] == ["good 0", "good 1"]
    for old, new in zip(before.segments, after.segments):
        assert old.model_dump(exclude={"target"}) == new.model_dump(exclude={"target"})
    assert published.result["autofix"]["applied_segment_count"] == 2
    debug = ReviewRunStore.open_existing(result.run_dir, storage=store)
    assert debug.load_json("autofix/index.json")["quality_final_status"] == "completed"
    provenance = result.changes[0]["provenance"][0]
    decision = store.read_artifact(provenance["quality_decision_ref"])
    assert decision["origin"] == provenance["origin"]
    candidate = store.read_artifact(
        f"reviews/{debug.review_id}/quality/units/{provenance['quality_unit_id']}/candidates/{decision['candidate_id']}.json"
    )
    assert candidate["operation"] == "review.quality_retranslate"


def test_resume_after_targets_committed_before_formal_scores(tmp_path):
    orch, store, _, _ = session(tmp_path, autofix=True)
    result = orch._review.run_session(store, [])
    from wenyi_core.pipeline.quality_workflow import QualityWorkflow

    original = QualityWorkflow.final_scores

    def stop_after_publish(flow, chapters, overrides, scope="shadow"):
        if scope == "formal" and any(s.target == "good 0" for s in store.load_chapter(0).segments):
            raise KeyboardInterrupt
        return original(flow, chapters, overrides, scope)

    with (
        patch.object(QualityWorkflow, "final_scores", stop_after_publish),
        pytest.raises(KeyboardInterrupt),
    ):
        orch._review_autofix.run(store, result, [])
    debug = ReviewRunStore.open_existing(result.run_dir, storage=store)
    assert debug.load_json("autofix/index.json")["status"] == "completed"
    assert debug.load_json("autofix/index.json")["quality_final_status"] == "pending"
    resumed = Orchestrator(orch._runtime.config, SessionFake())
    published = resumed._review_autofix.resume_pending(store)
    assert published is not None
    assert published.result["autofix"]["applied_segment_count"] == 2
    assert resumed._runtime.client.calls == []
    assert debug.load_json("autofix/index.json")["quality_final_status"] == "completed"


def test_no_autofix_does_not_resume_pending_publication(tmp_path):
    orch, store, _, _ = session(tmp_path, autofix=True)
    result = orch._review.run_session(store, [])
    with (
        patch.object(orch._review_autofix._publisher, "apply", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        orch._review_autofix.run(store, result, [])
    debug = ReviewRunStore.open_existing(result.run_dir, storage=store)
    assert debug.load_json("autofix/index.json")["status"] == "applying"
    config = orch._runtime.config.model_copy(deep=True)
    config.pipeline.review_autofix = False
    resumed = Orchestrator(config, SessionFake())
    assert resumed._review_autofix.resume_pending(store) is None
    assert [s.target for s in store.load_chapter(0).segments] == ["bad 0", "bad 1"]


def test_manual_edit_before_planning_blocks_quality_unit(tmp_path):
    orch, store, _, _ = session(tmp_path, autofix=True)
    result = orch._review.run_session(store, [])
    chapter = store.load_chapter(0)
    chapter.segments[1].target = "Human edit"
    store.save_chapter(chapter)
    published = orch._review_autofix.run(store, result, [])
    assert [s.target for s in store.load_chapter(0).segments] == ["bad 0", "Human edit"]
    assert published.result["autofix"]["status"] == "partial"


@pytest.mark.parametrize("after_prepare", [False, True])
def test_usage_prepare_interruption_cannot_count_same_response_twice(tmp_path, after_prepare):
    orch, store, client, _ = session(tmp_path)
    runtime = orch._runtime
    client.usage.record("judge", UsageSample(7, 3, 10), stage="review.quality_score")
    key = "reviews/review-quality/quality/responses/" + "e" * 64 + ".json"
    receipt = {"status": "completed", "response": {"value": 1}}
    prepare = store.prepare_usage_commit

    def interrupted(ledgers):
        if after_prepare:
            prepare(ledgers)
        raise KeyboardInterrupt

    with patch.object(store, "prepare_usage_commit", interrupted), pytest.raises(KeyboardInterrupt):
        runtime.flush_usage(store, scope="quality", artifacts={key: receipt})
    runtime.flush_usage(store, scope="recovery", artifacts={key: receipt})
    runtime.flush_usage(store, scope="second_recovery")
    assert store.load_usage()["totals"]["calls"] == 1
    assert store.load_usage()["totals"]["total_tokens"] == 10
    assert store.read_artifact(key) == receipt


def test_explicit_rerun_retries_degraded_quality_instead_of_skipping_forever(tmp_path):
    orch, store, _, _ = session(tmp_path, mode="observe")
    with patch.object(orch._runtime.client, "evaluate", side_effect=ValueError("invalid answer")):
        degraded = orch._review.run_session(store, [])
    fresh = Orchestrator(orch._runtime.config, SessionFake())
    retried = fresh._review.run_session(store, [])
    assert degraded.run_dir != retried.run_dir
    assert any(operation == "review.quality_score" for operation, _ in fresh._runtime.client.calls)


class ReviewFixFake(SessionFake):
    def complete(self, messages, *, operation, json_mode=False, max_tokens=None):
        if operation.startswith("review.quality_"):
            return super().complete(messages, operation=operation, json_mode=json_mode)
        self.calls.append((operation, messages))
        user = messages[-1]["content"]
        if operation == "review.scan":
            return _review_json(
                user,
                [
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "A condition is missing.",
                        "suggestion": "Restore the condition.",
                    }
                ],
            )
        if operation in {"review.fix", "autofix.fix"}:
            return _fix_json(user, "good repaired condition")
        if operation == "autofix.verify":
            return _agent_final(user)
        raise AssertionError(operation)


@pytest.mark.parametrize("autofix", [False, True])
def test_comparison_rejection_keeps_real_review_issue_unresolved(tmp_path, autofix):
    orch, store, _, _ = session(tmp_path, autofix=autofix)
    config = orch._runtime.config
    config.pipeline.quality.max_units_to_optimize_per_run = 0
    config.pipeline.review_fix_loop = not autofix
    client = ReviewFixFake(reject_comparison=True)
    orch = Orchestrator(config, client)
    result = orch._review.run_session(store, [])
    if autofix:
        result = orch._review_autofix.run(store, result, [])
        assert result.result["autofix"]["failed_issue_count"] >= 1
    assert result.issues
    assert result.result["termination"] != "clean_confirmed"
    assert result.changes == []
    assert store.load_chapter(0).segments[0].target == "bad 0"
    assert any(
        operation == ("autofix.fix" if autofix else "review.fix") for operation, _ in client.calls
    )
