"""Offline contracts for native typed judgments and shared routing controls."""

import json

import httpx
import pytest
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.judgments import (
    ChoiceQuestion,
    JudgmentContextOverflow,
    JudgmentProtocolError,
    JudgmentRequest,
    NoulQuestion,
    ScoreQuestion,
)
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.operations import configured_operations
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.llm.routing import inference_snapshot, resolve_routes


def graph(**extra):
    return LLMConfig.model_validate(
        {
            "preset": "fake",
            "providers": {"judge": {"kind": "typesafe", "max_retries": 0}},
            "models": {"jev": {"provider": "judge", "model": "jev-1.13.0"}},
            "routes": {"review.quality_score": {"model": "jev"}},
            **extra,
        }
    )


def request():
    return JudgmentRequest(
        state={"source": "Do not leave.", "target": "不要离开。"},
        questions={
            "quality": ScoreQuestion(instructions="Rate fidelity.", criteria=["Wrong", "Right"]),
            "best": ChoiceQuestion(instructions="Select.", criteria={"A": "First", "B": "Second"}),
            "safe": NoulQuestion(instructions="Is the negation preserved?"),
        },
    )


def response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "quality": {
                "type": "score",
                "score": 0.9,
                "legend": {"0": "Wrong", "1": "Right"},
                "probabilities": {"0": 0.1, "1": 0.9},
                "confidence": 0.8,
            },
            "best": {
                "type": "choice",
                "choice": "B",
                "probabilities": {"A": 0.1, "B": 0.9},
                "confidence": 0.8,
            },
            "safe": {"type": "noul", "noul": 0.9},
        },
        "usage": {"input_tokens": 31, "output_tokens": 7},
    }


def client_with_response(monkeypatch, payload):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-token")
    client = RoutedLLMClient(graph())
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(200, content=payload, headers={"x-request-id": "test-request"})

    client.adapter("judge")._client = httpx.Client(transport=httpx.MockTransport(handle))
    return client, seen


def test_native_endpoint_payload_and_usage(monkeypatch):
    client, seen = client_with_response(monkeypatch, json.dumps(response()))
    result = client.evaluate(request(), operation="review.quality_score")
    assert str(seen[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert seen[0].headers["authorization"] == "Bearer test-only-token"
    assert json.loads(seen[0].content) == {
        "model": "jev-1.13.0",
        **request().model_dump(exclude_none=True),
    }
    assert result.answers["safe"].noul == 0.9
    assert result.request_id == "test-request"
    assert client.usage_summary()["totals"]["prompt_tokens"] == 31
    assert client.usage_summary()["totals"]["completion_tokens"] == 7


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["answers"].pop("safe"),
        lambda body: body["answers"].update(unknown={"type": "noul", "noul": 0.5}),
        lambda body: body["answers"].update(quality={"type": "noul", "noul": 0.5}),
        lambda body: body["answers"]["quality"].update(score=2),
        lambda body: body["answers"]["quality"].update(score=float("nan")),
        lambda body: body["answers"]["quality"].update(score=float("inf")),
        lambda body: body["answers"]["quality"].update(score=True),
        lambda body: body["answers"]["quality"].update(score="0.9"),
        lambda body: body["answers"]["quality"].update(score=0.1),
        lambda body: body["answers"]["quality"].update(confidence=-1),
        lambda body: body["answers"]["quality"].pop("confidence"),
        lambda body: body["answers"]["quality"].update(probabilities={"0": 1}),
        lambda body: body["answers"]["quality"].update(probabilities={"0": 0.1, "1": 0.7}),
        lambda body: body["answers"]["quality"].update(probabilities={"0": -0.1, "1": 1.1}),
        lambda body: body["answers"]["quality"].update(legend={"0": "Wrong"}),
        lambda body: body["answers"]["best"].update(choice="C"),
        lambda body: body["answers"]["best"].update(choice="A"),
        lambda body: body["answers"]["best"].update(probabilities={"A": 0.1, "B": 0.9, "C": 0}),
        lambda body: body["answers"]["safe"].update(noul=1.1),
        lambda body: body["answers"]["safe"].update(noul=False),
        lambda body: body.update(model="jev-1.14.0"),
    ],
)
def test_invalid_business_answers_still_record_valid_usage(monkeypatch, mutate):
    body = response()
    mutate(body)
    client, seen = client_with_response(monkeypatch, json.dumps(body))
    with pytest.raises(JudgmentProtocolError):
        client.evaluate(request(), operation="review.quality_score")
    assert len(seen) == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 38
    assert client.usage_summary()["totals"]["calls"] == 1
    assert client.limits.reserved_tokens == 38


def test_duplicate_answer_keys_are_rejected_but_unambiguous_usage_is_recorded(monkeypatch):
    body = json.dumps(response()).replace('"score": 0.9', '"score": 0.9, "score": 0.8')
    client, _ = client_with_response(monkeypatch, body)
    with pytest.raises(JudgmentProtocolError, match="duplicate JSON"):
        client.evaluate(request(), operation="review.quality_score")
    assert client.usage_summary()["totals"]["total_tokens"] == 38


def test_duplicate_usage_is_not_guessed(monkeypatch):
    body = json.dumps(response()).replace(
        '"input_tokens": 31', '"input_tokens": 31, "input_tokens": 1'
    )
    client, _ = client_with_response(monkeypatch, body)
    events = []
    client.set_event_sink(lambda event, **data: events.append(event))
    with pytest.raises(JudgmentProtocolError):
        client.evaluate(request(), operation="review.quality_score")
    assert client.usage_summary()["totals"]["calls"] == 0
    assert "llm_usage_unknown" in events


@pytest.mark.parametrize(
    "usage", [None, {}, {"input_tokens": 2}, {"input_tokens": True, "output_tokens": 2}]
)
def test_missing_or_invalid_usage_is_explicit(monkeypatch, usage):
    body = response()
    body["usage"] = usage
    client, _ = client_with_response(monkeypatch, json.dumps(body))
    with pytest.raises(JudgmentProtocolError):
        client.evaluate(request(), operation="review.quality_score")
    assert client.usage_summary()["totals"]["calls"] == 0


def test_native_wire_cannot_override_trusted_provenance(monkeypatch):
    raw = response()
    raw["provenance"] = {"source": "generated"}
    client, _ = client_with_response(monkeypatch, json.dumps(raw))
    with pytest.raises(JudgmentProtocolError, match="provenance"):
        client.evaluate(request(), operation="review.quality_score")
    assert client.usage_summary()["totals"]["calls"] == 1


def test_rounded_distribution_is_accepted_without_rounding_scores(monkeypatch):
    body = response()
    body["answers"]["quality"]["probabilities"] = {"0": 0.0999, "1": 0.9}
    client, _ = client_with_response(monkeypatch, json.dumps(body))
    result = client.evaluate(request(), operation="review.quality_score")
    assert result.answers["quality"].probabilities["0"] == 0.0999
    assert result.answers["quality"].score == 0.9


@pytest.mark.parametrize(
    "status, attempts", [(429, 2), (529, 2), (500, 2), (401, 1), (403, 1), (422, 1)]
)
def test_shared_retries_honor_status_and_retry_after(monkeypatch, status, attempts):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-token")
    client = RoutedLLMClient(graph(providers={"judge": {"kind": "typesafe", "max_retries": 1}}))
    seen, waits = [], []

    def handle(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(
                status, headers={"retry-after": "2"}, json={"error": "unavailable"}
            )
        return httpx.Response(200, json=response())

    def wait(delay):
        assert client.limits._active["judge"] == 0
        waits.append(delay)

    monkeypatch.setattr(client.limits, "wait_for_retry", wait)
    client.adapter("judge")._client = httpx.Client(transport=httpx.MockTransport(handle))
    if attempts == 1:
        with pytest.raises(httpx.HTTPStatusError):
            client.evaluate(request(), operation="review.quality_score")
        assert not waits
    else:
        client.evaluate(request(), operation="review.quality_score")
        assert waits == [2.0]
    assert len(seen) == attempts
    assert client.limits.requests == attempts


def test_timeout_records_ambiguous_request_without_logging_evidence(monkeypatch):
    client, _ = client_with_response(monkeypatch, "")

    def handle(req):
        raise httpx.ReadTimeout("timed out", request=req)

    client.adapter("judge")._client = httpx.Client(transport=httpx.MockTransport(handle))
    events = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))
    with pytest.raises(httpx.ReadTimeout):
        client.evaluate(request(), operation="review.quality_score")
    assert any(row["event"] == "llm_request_ambiguous" for row in events)
    assert len({row["call_id"] for row in events}) == 1
    assert "不要离开" not in json.dumps(events, ensure_ascii=False)
    assert "test-only-token" not in json.dumps(events)


def test_judgment_shares_global_budgets_and_cancellation(monkeypatch):
    client, seen = client_with_response(monkeypatch, json.dumps(response()))
    client.config.budget = client.config.budget.model_copy(
        update={"max_requests": 1, "max_tokens": 5000}
    )
    client.evaluate(request(), operation="review.quality_score")
    assert client.limits.reserved_tokens == 38
    with pytest.raises(RequestStopped, match="budget"):
        client.complete([], operation="translation.body", max_tokens=128)
    assert len(seen) == 1
    cancelled, seen = client_with_response(monkeypatch, json.dumps(response()))
    cancelled.cancel()
    with pytest.raises(RequestCancelled):
        cancelled.evaluate(request(), operation="review.quality_score")
    assert not seen


def test_judgment_budget_reservation_needs_no_fake_output_limit(monkeypatch):
    client, seen = client_with_response(monkeypatch, json.dumps(response()))
    client.config.budget = client.config.budget.model_copy(update={"max_tokens": 10})
    with pytest.raises(RequestStopped, match="budget"):
        client.evaluate(request(), operation="review.quality_score")
    assert not seen
    client.validate_credentials(("review.quality_score",))


@pytest.mark.parametrize("case", ["state", "total"])
def test_both_context_limits_checked_without_truncating_evidence(monkeypatch, case):
    client, seen = client_with_response(monkeypatch, json.dumps(response()))
    if case == "state":
        oversized = JudgmentRequest(state="x" * 32_000, questions=request().questions)
    else:
        oversized = JudgmentRequest(
            state="short",
            questions={str(i): NoulQuestion(instructions="x" * 10_000) for i in range(7)},
        )
    with pytest.raises(JudgmentContextOverflow, match="context_overflow"):
        client.evaluate(oversized, operation="review.quality_score")
    assert client.limits.requests == 0
    assert not seen


@pytest.mark.parametrize(
    "routes",
    [
        {"translation.body": {"model": "jev"}},
        {"translation.body": {"model": "default_strong", "fallbacks": ["jev"]}},
    ],
)
def test_capability_mismatch_fails_before_requests(routes):
    with pytest.raises(ValueError, match="requires.*supports"):
        graph(routes=routes)


def test_pinned_model_and_native_options_are_required():
    for model in (
        {"provider": "judge", "model": "jev-latest"},
        {"provider": "judge", "model": "jev-1.13.0", "max_output_tokens": 100},
        {"provider": "judge", "model": "jev-1.13.0", "options": {"temperature": 0}},
    ):
        with pytest.raises(ValueError):
            graph(models={"jev": model})


def test_evaluate_and_complete_do_not_interchange_capabilities():
    client = RoutedLLMClient(graph())
    with pytest.raises(ValueError, match="require evaluate"):
        client.complete([], operation="review.quality_score")
    with pytest.raises(ValueError, match="require complete"):
        client.evaluate(request(), operation="translation.body")
    assert not client._adapters


def test_missing_explicit_route_is_only_required_when_reachable(monkeypatch):
    from wenyi_core.config import Config

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = Config.from_dict({"llm": graph(routes={}).model_dump()})
    operations = configured_operations(config, "review")
    assert not any(operation.startswith("review.quality_") for operation in operations)
    client = RoutedLLMClient(config.llm)
    client.validate_credentials(operations)
    assert "judge" not in client._adapters
    assert "review.quality_score" not in resolve_routes(config.llm)
    with pytest.raises(ValueError, match="explicit"):
        inference_snapshot(config.llm, ("review.quality_score",))


def test_unused_judgment_model_does_not_change_old_inference_identity():
    from wenyi_core.config import Config

    before = Config.from_dict({"llm": {"preset": "fake"}})
    after = Config.from_dict({"llm": graph().model_dump()})
    reachable = configured_operations(before, "review")
    assert inference_snapshot(before.llm, reachable) == inference_snapshot(after.llm, reachable)
    assert resolve_routes(after.llm)["review.quality_compare"].model == "jev-1.13.0"


def test_observe_excludes_optimize_credentials_and_inference(monkeypatch):
    from wenyi_core.config import Config

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-token")
    monkeypatch.delenv("UNUSED_QUALITY_GENERATION_KEY", raising=False)
    raw = graph().model_dump()
    raw["providers"]["editor"] = {"kind": "openai", "api_key_env": "UNUSED_QUALITY_GENERATION_KEY"}
    raw["models"]["editor"] = {"provider": "editor", "model": "editor-v1"}
    raw["routes"]["review.quality_verify"] = {"model": "editor"}
    config = Config.from_dict({"llm": raw, "pipeline": {"quality": {"mode": "observe"}}})
    operations = configured_operations(config, "review")
    assert {op for op in operations if op.startswith("review.quality_")} == {"review.quality_score"}
    client = RoutedLLMClient(config.llm)
    client.validate_credentials(operations)
    assert "editor" not in client._adapters
    first_identity = inference_snapshot(config.llm, operations)
    raw["models"]["editor"]["model"] = "editor-v2"
    changed = LLMConfig.model_validate(raw)
    assert inference_snapshot(changed, operations) == first_identity
    config.pipeline.quality.mode = "optimize"
    optimize_operations = configured_operations(config, "review")
    assert len({op for op in optimize_operations if op.startswith("review.quality_")}) == 6
    assert inference_snapshot(config.llm, optimize_operations) != inference_snapshot(
        changed, optimize_operations
    )
    with pytest.raises(RuntimeError, match="UNUSED_QUALITY_GENERATION_KEY"):
        client.validate_credentials(optimize_operations)


def test_judgment_fingerprint_includes_fallback_model_identity():
    raw = graph().model_dump()
    first = RoutedLLMClient(LLMConfig.model_validate(raw)).inference_fingerprint(
        "review.quality_score"
    )
    raw["models"]["other"] = {"provider": "judge", "model": "jev-1.13.1"}
    raw["routes"]["review.quality_score"]["fallbacks"] = ["other"]
    second = RoutedLLMClient(LLMConfig.model_validate(raw)).inference_fingerprint(
        "review.quality_score"
    )
    assert first != second
