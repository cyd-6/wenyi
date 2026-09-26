"""Offline best-of-three generation, publication boundaries and restart contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from threading import Lock

import pytest
from wenyi_core.candidates import candidate_index_key, load_candidate_comparison
from wenyi_core.config import Config
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.choice import ChoiceResult, ChoiceUnavailable
from wenyi_core.llm.configuration import RouteConfig
from wenyi_core.llm.limits import RequestStopped
from wenyi_core.llm.operations import configured_operations
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.usage import UsageSample
from wenyi_core.pipeline.runtime import PipelineRuntime
from wenyi_core.pipeline.translation_batch import BatchPlan
from wenyi_core.pipeline.translation_candidates import TranslationCandidateService
from wenyi_core.storage.file import FileStorage


def candidate_config(*, polish=True):
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake", "routes": {"translation.judge": {"tier": "strong"}}},
            "pipeline": {"best_of_three": True, "polish": polish},
        }
    )


def candidate_plan(sources=("First sentence", "123", "Second sentence"), *, allow_empty=False):
    return BatchPlan.capture(
        0,
        0,
        [Segment(index=10 + i * 2, source=s) for i, s in enumerate(sources)],
        [],
        "Earlier translation",
        "Consistent style",
        "Book synopsis",
        "Chapter digest",
        [[] for _ in sources],
        "Following source",
        allow_empty_translations=allow_empty,
    )


def candidate_store(tmp_path, plan):
    source = tmp_path / "source.txt"
    source.write_text("Synthetic test content", encoding="utf-8")
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(
        Document(
            title="Synthetic",
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=[
                Chapter(
                    index=plan.chapter,
                    segments=[
                        Segment(index=i, source=s)
                        for i, s in zip(plan.segment_indices, plan.sources)
                    ],
                )
            ],
        )
    )
    return store


class CandidateClient(FakeClient):
    def __init__(self, config, *, identical=False, blank=False):
        super().__init__(config=config.llm)
        self.lock = Lock()
        self.generated = 0
        self.polished = 0
        self.judged = 0
        self.identical = identical
        self.blank = blank
        self.fail_judge = False
        self.stop_polish = False
        self.requests = []

    def complete(self, messages, *, operation, **kwargs):
        with self.lock:
            self.requests.append((operation, messages))
            self.usage.record("strong", UsageSample(10, 5, 15), operation)
            if operation == "translation.body":
                self.generated += 1
                number = 1 if self.identical else self.generated
                values = [""] if self.blank else [f"Draft {number} first", f"Draft {number} second"]
                return json.dumps({"translations": values})
            if operation == "polish.body":
                self.polished += 1
                if self.stop_polish:
                    raise RequestStopped("Test interruption")
                raw = json.loads(messages[2]["content"])["translations"]
                return json.dumps({"polished": [text.upper() for text in raw]})
        raise AssertionError(operation)

    def choose(self, request, *, operation):
        self.judged += 1
        self.usage.record("strong", UsageSample(20, 2, 22), operation)
        if self.fail_judge:
            raise ChoiceUnavailable("Test judge unavailable")
        self.last_choice_request = request
        return ChoiceResult("B", model="fake-judge")


def execute(config, client, store, plan):
    runtime = PipelineRuntime(config, client)
    return TranslationCandidateService(client, config).execute(
        plan, store, flush_usage=lambda: runtime.flush_usage(store, scope="candidate-test")
    )


@pytest.mark.parametrize("polish", [False, True])
def test_three_independent_candidates_choose_entire_batch_and_preserve_source(tmp_path, polish):
    config = candidate_config(polish=polish)
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    client = CandidateClient(config)
    result, record = execute(config, client, store, plan)
    assert (client.generated, client.polished, client.judged) == (3, 3 if polish else 0, 1)
    assert result.targets == tuple(record.candidates[1].targets)
    assert result.targets[1] == "123"
    assert result.before_polish == (
        tuple(record.candidates[1].raw_targets) if polish else (None, None, None)
    )
    translation_requests = [m for op, m in client.requests if op == "translation.body"]
    assert translation_requests[0] == translation_requests[1] == translation_requests[2]
    assert all(s.target is None for s in store.load_chapter(0).segments)
    assert store.load_context() is None
    assert client.last_choice_request.options == {c.id: c.targets for c in record.candidates}
    assert store.load_usage()["totals"]["calls"] == 4 + (3 if polish else 0)
    comparison = load_candidate_comparison(store, 0, 10)
    assert comparison.decision.choice == "B"
    assert "turn" not in comparison.model_dump()["candidates"][0]
    assert len(store.read_artifact(candidate_index_key(0))) == 3


def test_duplicate_retry_budget_survives_failed_judge_and_resume(tmp_path):
    config = candidate_config(polish=False)
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    client = CandidateClient(config, identical=True)
    client.fail_judge = True
    with pytest.raises(ChoiceUnavailable):
        execute(config, client, store, plan)
    assert client.generated == 5
    saved = load_candidate_comparison(store, 0, 10)
    assert saved.extra_generations == 2
    assert saved.candidates[1].duplicate_of == "A"
    assert all(s.target is None for s in store.load_chapter(0).segments)
    resumed = CandidateClient(config, identical=True)
    result, record = execute(config, resumed, store, plan)
    assert (resumed.generated, resumed.judged) == (0, 1)
    assert record.extra_generations == 2
    assert result.targets == tuple(saved.candidates[1].targets)
    assert (
        "Avoid repeating"
        in [m for op, m in client.requests if op == "translation.body"][-1][1]["content"]
    )


def test_polish_interruption_preserves_raw_candidates(tmp_path):
    config = candidate_config()
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    client = CandidateClient(config)
    client.stop_polish = True
    with pytest.raises(RequestStopped):
        execute(config, client, store, plan)
    resumed = CandidateClient(config)
    result, record = execute(config, resumed, store, plan)
    assert resumed.generated == 0
    assert resumed.polished == 3
    assert record.status == "selected"
    assert result.targets == tuple(record.candidates[1].targets)


def test_changing_only_judge_reuses_candidates_and_selected_cache(tmp_path):
    config = candidate_config()
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    client = CandidateClient(config)
    execute(config, client, store, plan)
    calls = store.load_usage()["totals"]["calls"]
    resumed = CandidateClient(config)
    execute(config, resumed, store, plan)
    assert (resumed.generated, resumed.polished, resumed.judged) == (0, 0, 0)
    assert store.load_usage()["totals"]["calls"] == calls
    config.llm.routes["translation.judge"] = RouteConfig(tier="cheap")
    # A different tier pointing at the same physical model preserves the saved judgment.
    execute(config, CandidateClient(config), store, plan)
    assert store.load_usage()["totals"]["calls"] == calls
    config.llm.models["default_cheap"] = config.llm.models["default_cheap"].model_copy(
        update={"model": "another-judge"}
    )
    changed = CandidateClient(config)
    execute(config, changed, store, plan)
    assert (changed.generated, changed.polished, changed.judged) == (0, 0, 1)


def test_changed_inputs_invalidate_pending_candidates_but_not_old_records(tmp_path):
    config = candidate_config(polish=False)
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    execute(config, CandidateClient(config), store, plan)
    changed = CandidateClient(config)
    execute(config, changed, store, replace(plan, context="Changed context"))
    assert changed.generated == 3
    assert len(store.list_artifacts("translation-candidates/0/")) == 3


def test_mineru_blank_is_a_completed_candidate_and_symbols_skip_models(tmp_path):
    config = candidate_config(polish=False)
    plan = candidate_plan(("OCR noise",), allow_empty=True)
    store = candidate_store(tmp_path, plan)
    result, record = execute(config, CandidateClient(config, blank=True), store, plan)
    assert result.targets == ("",)
    assert record.decision.choice == "B"
    symbols = candidate_plan(("123", "---"))
    client = CandidateClient(config)
    result, record = execute(config, client, store, symbols)
    assert result.targets == symbols.sources
    assert record is None
    assert client.generated == client.judged == 0


def test_comparison_rejects_source_replacement_and_keeps_manual_edits_separate(tmp_path):
    config = candidate_config(polish=False)
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    execute(config, CandidateClient(config), store, plan)
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "Manual revision"
    store.save_chapter(chapter)
    assert load_candidate_comparison(store, 0, 10).candidates[1].targets[0] != "Manual revision"
    manifest = store.load_manifest()
    manifest["source_sha256"] = "0" * 64
    store.save_manifest(manifest)
    assert load_candidate_comparison(store, 0, 10) is None


def test_mode_requires_explicit_judge_and_is_reachable_only_for_body_translation():
    assert Config().pipeline.best_of_three is False
    with pytest.raises(ValueError, match="explicit.*translation.judge"):
        Config.from_dict({"pipeline": {"best_of_three": True}})
    config = candidate_config()
    assert "translation.judge" in configured_operations(config, "translate")
    for workflow in ("prepare", "review", "srt"):
        assert "translation.judge" not in configured_operations(config, workflow)


def test_formal_save_interruption_resumes_publication_without_model_calls(tmp_path):
    from wenyi_core.candidates import recover_candidate_publications

    config = candidate_config()
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    result, _record = execute(config, CandidateClient(config), store, plan)
    chapter = store.load_chapter(0)
    for segment, target, before in zip(chapter.segments, result.targets, result.before_polish):
        segment.target, segment.target_before_polish = target, before
    store.save_chapter(chapter)
    assert load_candidate_comparison(store, 0, 10).status == "selected"
    recover_candidate_publications(store, 0)
    assert load_candidate_comparison(store, 0, 10).status == "published"
    resumed = CandidateClient(config)
    execute(config, resumed, store, plan)
    assert resumed.generated == resumed.polished == resumed.judged == 0


def test_changing_polish_route_reuses_initial_translations(tmp_path):
    config = candidate_config()
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    execute(config, CandidateClient(config), store, plan)
    config.llm.routes["polish.body"] = RouteConfig(tier="cheap")
    config.llm.models["default_cheap"] = config.llm.models["default_cheap"].model_copy(
        update={"model": "other-polisher"}
    )
    changed = CandidateClient(config)
    execute(config, changed, store, plan)
    assert (changed.generated, changed.polished, changed.judged) == (0, 3, 1)


def test_generation_failure_reuses_the_other_completed_candidates(tmp_path, monkeypatch):
    from wenyi_core.pipeline.translation_batch import TranslationBatchExecutor

    config = candidate_config(polish=False)
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    original = TranslationBatchExecutor.translate
    calls = []
    lock = Lock()

    def fail_first(executor, plan):
        with lock:
            calls.append(True)
            first = len(calls) == 1
        if first:
            raise RuntimeError("Interrupted before receiving a candidate")
        return original(executor, plan)

    with monkeypatch.context() as patch:
        patch.setattr(TranslationBatchExecutor, "translate", fail_first)
        with pytest.raises(RuntimeError, match="Interrupted"):
            execute(config, CandidateClient(config), store, plan)
    saved = load_candidate_comparison(store, 0, 10)
    assert sum(c.targets is not None for c in saved.candidates) == 2
    resumed = CandidateClient(config)
    resumed.generated = 20  # Distinguish this synthetic response from the first client's responses.
    execute(config, resumed, store, plan)
    assert resumed.generated == 21 and resumed.judged == 1


def test_only_winners_advance_context_glossary_and_progress(tmp_path, monkeypatch):
    from wenyi_core.pipeline.orchestrator import Orchestrator

    config = candidate_config()
    config.segment.max_tokens_per_batch = 4
    config.pipeline.annotation_alignment = False
    plan = candidate_plan(("First sentence", "Second sentence", "Third sentence", "Last sentence"))
    store = candidate_store(tmp_path, plan)
    client = CandidateClient(config)
    service = Orchestrator(config, client)._translation
    extracted = []

    def extract(_glossary, _source, target, *_args, **_kwargs):
        extracted.append(target)
        return {"inserted": 0}

    monkeypatch.setattr(service._runtime.extractor, "extract_and_store", extract)
    monkeypatch.setattr(service._titles, "run", lambda *_a, **_kw: None)
    progress = []
    service.run(
        store, book_synopsis="", progress=lambda done, total, _: progress.append((done, total))
    )
    records = [load_candidate_comparison(store, 0, index) for index in (10, 14)]
    assert len({record.batch_id for record in records}) == 2
    winners = [text for record in records for text in record.candidates[1].targets]
    assert [segment.target for segment in store.load_chapter(0).segments] == winners
    assert store.load_context()["recent_targets"] == winners
    assert extracted == ["\n".join(winners[:2]), "\n".join(winners[2:]), "\n".join(winners)]
    assert set(progress) == {(0, 4), (2, 4), (4, 4)}
    assert all(record.status == "published" for record in records)
    requests = [
        messages for operation, messages in client.requests if operation == "translation.body"
    ]
    assert len(requests) == 6
    assert all(winners[0] in messages[1]["content"] for messages in requests[3:])
    losers = [c.targets[0] for c in records[0].candidates if c.id != "B"]
    assert all(text not in messages[1]["content"] for text in losers for messages in requests[3:])
    count = (client.generated, client.polished, client.judged)
    service.run(store, book_synopsis="")
    assert (client.generated, client.polished, client.judged) == count


def test_candidate_alignment_falls_back_per_paragraph_without_losing_symbols(tmp_path):
    from tests.test_translation_context import _numbered_sources

    config = candidate_config(polish=False)
    config.pipeline.align_retry_limit = 0
    plan = candidate_plan()
    store = candidate_store(tmp_path, plan)
    responses = []
    lock = Lock()

    def handler(messages, *_):
        if messages[0]["content"].startswith("Select exactly one supplied option"):
            return '{"choice":"C"}'
        with lock:
            sources = _numbered_sources(messages[1]["content"])
            if len(sources) > 1:
                return '{"translations":[]}'
            responses.append(sources)
            return json.dumps({"translations": [f"Translation {len(responses)}"]})

    client = FakeClient(handler=handler, config=config.llm)
    result, record = execute(config, client, store, plan)
    assert result.targets == tuple(record.candidates[2].targets)
    assert result.targets[1] == "123"
    assert len(responses) == 6
