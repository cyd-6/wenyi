"""Offline contracts for strict judgments made by ordinary generation providers."""

import json
from types import SimpleNamespace as NS

import httpx
import pytest
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.generated_judgments import DEFAULT_OUTPUT_TOKENS, judgment_messages
from wenyi_core.llm.judgments import (
    ChoiceQuestion,
    JudgmentProtocolError,
    JudgmentRequest,
    NoulQuestion,
    ScoreQuestion,
)
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.providers.fake import FakeProvider
from wenyi_core.llm.registry import provider_spec
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.llm.routing import inference_snapshot, resolve_routes
from wenyi_core.llm.usage import UsageSample


def request():
    return JudgmentRequest(
        state={"source": "Do not leave.", "target": "不要离开。"},
        questions={
            "quality": ScoreQuestion(instructions="Rate fidelity.", criteria=["Wrong", "Right"])
        },
    )


def body():
    return {
        "answers": {
            "quality": {
                "type": "score",
                "probabilities": {"0": 0.1, "1": 0.9},
                "confidence": 0.8,
            }
        }
    }


def test_deepseek_cheap_tier_can_judge_without_typesafe(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client = RoutedLLMClient(
        LLMConfig.model_validate(
            {"preset": "deepseek", "routes": {"review.quality_score": {"tier": "cheap"}}}
        )
    )
    seen = []

    def complete(**kwargs):
        seen.append(kwargs)
        return NS(
            choices=[NS(message=NS(content=json.dumps(body())))],
            usage=NS(prompt_tokens=31, completion_tokens=7, total_tokens=38),
        )

    client.adapter("default")._client = NS(chat=NS(completions=NS(create=complete)))
    result = client.evaluate(request(), operation="review.quality_score")
    assert result.answers["quality"].score == 0.9
    assert result.answers["quality"].legend == {"0": "Wrong", "1": "Right"}
    assert result.provenance.source == "generated"
    assert result.provenance.probabilities == "self_reported"
    assert result.provenance.model_identity == "requested"
    assert result.usage.input_tokens == 31
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[0]["max_tokens"] > 0
    actual_input = seen[0]["messages"][1]["content"].removesuffix("\n\nOutput must be valid json.")
    assert json.loads(actual_input) == request().model_dump(exclude_none=True)
    assert client.usage_summary()["totals"]["calls"] == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 38


def graph(**extra):
    return LLMConfig.model_validate(
        {
            "preset": "fake",
            "providers": {"default": {"kind": "fake", "max_retries": 0}},
            "routes": {"review.quality_score": {"tier": "cheap"}},
            **extra,
        }
    )


def stub_response(monkeypatch, payload, *, usage=UsageSample(31, 7, 38), config=None):
    seen = []

    def respond(self, messages, model, *, json_mode, context):
        seen.append({"messages": messages, "model": model, "context": context})
        assert json_mode
        context.record_usage(usage)
        return payload

    monkeypatch.setattr(FakeProvider, "_request", respond)
    return RoutedLLMClient(config or graph()), seen


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["answers"].clear(),
        lambda raw: raw["answers"].update(extra=raw["answers"]["quality"]),
        lambda raw: raw["answers"]["quality"].pop("type"),
        lambda raw: raw["answers"]["quality"].update(type="choice"),
        lambda raw: raw["answers"]["quality"].pop("confidence"),
        lambda raw: raw["answers"]["quality"].update(confidence=True),
        lambda raw: raw["answers"]["quality"].update(confidence="0.8"),
        lambda raw: raw["answers"]["quality"].update(confidence=float("nan")),
        lambda raw: raw["answers"]["quality"].update(confidence=float("inf")),
        lambda raw: raw["answers"]["quality"].update(confidence=-0.1),
        lambda raw: raw["answers"]["quality"].update(probabilities={"1": 1}),
        lambda raw: raw["answers"]["quality"].update(probabilities={"0": 0.1, "1": 0.8}),
        lambda raw: raw["answers"]["quality"].update(probabilities={"0": -0.1, "1": 1.1}),
        lambda raw: raw["answers"]["quality"].update(probabilities={"0": False, "1": 1}),
        lambda raw: raw["answers"]["quality"].update(probabilities={"0": "0.1", "1": 0.9}),
        lambda raw: raw["answers"]["quality"].update(probabilities={"0": 0.1, "1": 0.9, "2": 0}),
        lambda raw: raw["answers"]["quality"].update(score=1),
        lambda raw: raw["answers"]["quality"].update(legend={"0": "Wrong", "1": "Right"}),
        lambda raw: raw.update(model="fake-pinned"),
        lambda raw: raw.update(usage={"input_tokens": 0, "output_tokens": 0}),
        lambda raw: raw.update(provenance={"source": "native"}),
    ],
)
def test_invalid_json_answers_fail_closed_and_count_usage_once(monkeypatch, mutate):
    raw = body()
    mutate(raw)
    client, seen = stub_response(monkeypatch, json.dumps(raw))
    with pytest.raises(JudgmentProtocolError):
        client.evaluate(request(), operation="review.quality_score")
    assert len(seen) == 1
    assert client.usage_summary()["totals"]["calls"] == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 38
    assert client.limits.reserved_tokens == 38


@pytest.mark.parametrize(
    "payload",
    [
        "",
        " ",
        "null",
        "[]",
        "{",
        "```json\n{}\n```",
        '{"answers":{},"answers":{}}',
        json.dumps(body()).replace('"confidence": 0.8', '"confidence": 0.8,"confidence": 0.9'),
        json.dumps(body()).replace('"0": 0.1', '"0": 0.1,"0": 0.1'),
    ],
)
def test_empty_malformed_or_duplicate_json_is_never_repaired(monkeypatch, payload):
    client, seen = stub_response(monkeypatch, payload)
    with pytest.raises(JudgmentProtocolError):
        client.evaluate(request(), operation="review.quality_score")
    assert len(seen) == 1
    assert client.usage_summary()["totals"]["calls"] == 1


def test_choice_noul_and_structured_score_criteria(monkeypatch):
    judgment = JudgmentRequest(
        state="Evidence.",
        questions={
            "score": ScoreQuestion(instructions="Rate.", criteria=[{"bad": "Wrong"}, "Right"]),
            "choice": ChoiceQuestion(instructions="Choose.", criteria={"A": "First", "B": "Other"}),
            "noul": NoulQuestion(instructions="Correct?"),
        },
    )
    payload = {
        "answers": {
            "score": {"type": "score", "probabilities": {"0": 0.25, "1": 0.75}, "confidence": 0.7},
            "choice": {
                "type": "choice",
                "choice": "A",
                "probabilities": {"A": 0.6, "B": 0.4},
                "confidence": 0.6,
            },
            "noul": {"type": "noul", "noul": 0.9},
        }
    }
    client, _ = stub_response(monkeypatch, json.dumps(payload))
    result = client.evaluate(judgment, operation="review.quality_compare")
    assert result.answers["score"].score == 0.75
    assert json.loads(result.answers["score"].legend["0"]) == {"bad": "Wrong"}
    assert result.answers["choice"].choice == "A"
    assert result.answers["noul"].noul == 0.9


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "choice", "choice": "A", "probabilities": {"A": 0.1, "B": 0.9}, "confidence": 0.8},
        {"type": "choice", "choice": "A", "probabilities": {"A": 1.0}, "confidence": 0.8},
        {"type": "choice", "choice": "C", "probabilities": {"A": 0.1, "B": 0.9}, "confidence": 0.8},
        {"type": "noul", "noul": True},
        {"type": "noul", "noul": 1.1},
        {"type": "noul", "noul": float("inf")},
    ],
)
def test_choice_and_noul_preserve_strict_contract(monkeypatch, answer):
    question = (
        ChoiceQuestion(instructions="Choose", criteria={"A": None, "B": None})
        if answer["type"] == "choice"
        else NoulQuestion(instructions="Correct?")
    )
    client, _ = stub_response(monkeypatch, json.dumps({"answers": {"q": answer}}))
    with pytest.raises(JudgmentProtocolError):
        client.evaluate(
            JudgmentRequest(state="Evidence", questions={"q": question}),
            operation="review.quality_compare",
        )


def test_missing_generation_usage_is_unknown_and_reservation_retained(monkeypatch):
    client, _ = stub_response(monkeypatch, json.dumps(body()), usage=None)
    events = []
    client.set_event_sink(lambda event, **data: events.append((event, data)))
    result = client.evaluate(request(), operation="review.quality_score")
    assert result.usage is None
    assert client.usage_summary()["totals"]["calls"] == 0
    assert client.limits.reserved_tokens > DEFAULT_OUTPUT_TOKENS
    assert any(event == "llm_usage_unknown" for event, _ in events)
    assert "不要离开" not in json.dumps(events, ensure_ascii=False)


def test_output_limit_and_complete_bridge_input_are_reserved_before_call(monkeypatch):
    config = graph(
        models={
            "default_cheap": {"provider": "default", "model": "cheap", "max_output_tokens": 1234}
        }
    )
    client, seen = stub_response(monkeypatch, json.dumps(body()), config=config)
    events = []
    client.set_event_sink(lambda event, **data: events.append((event, data)))
    client.evaluate(request(), operation="review.quality_score")
    started = next(data for event, data in events if event == "llm_request_started")
    expected = (
        len(json.dumps(judgment_messages(request()), ensure_ascii=False).encode("utf-8"))
        + 256
        + 1234
    )
    assert started["estimated_tokens"] == expected
    assert seen[0]["context"].max_tokens == 1234
    blocked, seen = stub_response(
        monkeypatch, json.dumps(body()), config=graph(budget={"max_tokens": 8192})
    )
    with pytest.raises(RequestStopped, match="token reservation"):
        blocked.evaluate(request(), operation="review.quality_score")
    assert not seen


def test_request_budget_and_cancel_apply_to_generated_judgments(monkeypatch):
    client, seen = stub_response(
        monkeypatch, json.dumps(body()), config=graph(budget={"max_requests": 1})
    )
    client.evaluate(request(), operation="review.quality_score")
    with pytest.raises(RequestStopped, match="request budget"):
        client.evaluate(request(), operation="review.quality_score")
    assert len(seen) == 1
    cancelled, seen = stub_response(monkeypatch, json.dumps(body()))
    cancelled.cancel()
    with pytest.raises(RequestCancelled):
        cancelled.evaluate(request(), operation="review.quality_score")
    assert not seen


def test_bridge_prompt_changes_only_generated_judgment_identity(monkeypatch):
    from wenyi_core.llm import generated_judgments

    config = graph()
    before = inference_snapshot(config, ("translation.body", "review.quality_score"))
    original = generated_judgments.read_text
    monkeypatch.setattr(
        generated_judgments, "read_text", lambda path: original(path) + " Revision."
    )
    after = inference_snapshot(config, before)
    assert before["translation.body"] == after["translation.body"]
    assert before["review.quality_score"] != after["review.quality_score"]


def test_native_capability_and_generated_judgment_route_description():
    assert provider_spec("deepseek").capabilities == ("generation",)
    assert provider_spec("deepseek").effective_capabilities == ("generation", "judgment")
    assert provider_spec("typesafe").effective_capabilities == ("judgment",)
    routes = resolve_routes(graph())
    assert routes["review.quality_compare"].tier == "cheap"
    assert routes["review.quality_score"].describe()["judgment_source"] == "generated"
    assert "judgment_source" not in routes["translation.body"].describe()


def test_generated_transport_retries_share_usage_and_release_permits(monkeypatch):
    client = RoutedLLMClient(graph(providers={"default": {"kind": "fake", "max_retries": 1}}))
    calls, waits = [], []

    def respond(self, messages, model, *, json_mode, context):
        calls.append(context)
        if len(calls) == 1:
            response = httpx.Response(
                429,
                headers={"retry-after": "1"},
                request=httpx.Request("POST", "https://test.invalid"),
            )
            response.raise_for_status()
        context.record_usage(UsageSample(31, 7, 38))
        return json.dumps(body())

    def wait(delay):
        assert client.limits._active["default"] == 0
        waits.append(delay)

    monkeypatch.setattr(FakeProvider, "_request", respond)
    monkeypatch.setattr(client.limits, "wait_for_retry", wait)
    client.evaluate(request(), operation="review.quality_score")
    assert waits == [1.0]
    assert client.limits.requests == 2
    assert client.usage_summary()["totals"]["calls"] == 1


@pytest.mark.parametrize("first_payload", ["empty", "invalid"])
def test_generation_response_failure_keeps_usage_and_retry_bound(monkeypatch, first_payload):
    client = RoutedLLMClient(
        LLMConfig.model_validate(
            {
                "preset": "deepseek",
                "providers": {"default": {"kind": "deepseek", "max_retries": 1}},
                "routes": {"review.quality_score": {"tier": "cheap"}},
            }
        )
    )
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        content = (
            ("" if first_payload == "empty" else "{}") if len(calls) == 1 else json.dumps(body())
        )
        return NS(
            choices=[NS(message=NS(content=content))],
            usage=NS(prompt_tokens=31, completion_tokens=7, total_tokens=38),
        )

    client.adapter("default")._client = NS(chat=NS(completions=NS(create=complete)))
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda delay: None)
    if first_payload == "empty":
        result = client.evaluate(request(), operation="review.quality_score")
        assert result.usage.input_tokens == 31
        assert len(calls) == 2
    else:
        with pytest.raises(JudgmentProtocolError):
            client.evaluate(request(), operation="review.quality_score")
        assert len(calls) == 1
    assert client.usage_summary()["totals"]["calls"] == len(calls)
    assert client.usage_summary()["totals"]["total_tokens"] == 38 * len(calls)


@pytest.mark.parametrize("native_first", [False, True])
def test_native_and_generated_judgment_fallback_share_one_call_identity(monkeypatch, native_first):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-token")
    primary, fallback = ("jev", "default_cheap") if native_first else ("default_cheap", "jev")
    client = RoutedLLMClient(
        graph(
            providers={
                "default": {"kind": "fake", "max_retries": 0},
                "native": {"kind": "typesafe", "max_retries": 0},
            },
            models={"jev": {"provider": "native", "model": "jev-1.13.0"}},
            routes={"review.quality_score": {"model": primary, "fallbacks": [fallback]}},
        )
    )
    attempts, events = [], []

    def respond(self, messages, model, *, json_mode, context):
        attempts.append("generated")
        if not native_first:
            httpx.Response(
                503, request=httpx.Request("POST", "https://test.invalid")
            ).raise_for_status()
        context.record_usage(UsageSample(31, 7, 38))
        return json.dumps(body())

    def native(req):
        attempts.append("native")
        if native_first:
            return httpx.Response(503)
        raw = body()
        raw["answers"]["quality"].update(score=0.9, legend={"0": "Wrong", "1": "Right"})
        raw.update(model="jev-1.13.0", usage={"input_tokens": 31, "output_tokens": 7})
        return httpx.Response(200, json=raw)

    monkeypatch.setattr(FakeProvider, "_request", respond)
    client.adapter("native")._client = httpx.Client(transport=httpx.MockTransport(native))
    client.set_event_sink(lambda event, **data: events.append((event, data)))
    result = client.evaluate(request(), operation="review.quality_score")
    assert result.provenance.source == ("generated" if native_first else "native")
    assert attempts == (["native", "generated"] if native_first else ["generated", "native"])
    assert client.usage_summary()["totals"]["calls"] == 1
    assert client.limits.requests == 2
    assert len({data["call_id"] for _, data in events}) == 1
    assert len({data["inference_fingerprint"] for _, data in events}) == 2


def test_retry_cannot_reuse_previous_attempts_usage(monkeypatch):
    client = RoutedLLMClient(
        LLMConfig.model_validate(
            {
                "preset": "deepseek",
                "providers": {"default": {"kind": "deepseek", "max_retries": 1}},
                "routes": {"review.quality_score": {"tier": "cheap"}},
            }
        )
    )
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return NS(
            choices=[NS(message=NS(content="" if len(calls) == 1 else json.dumps(body())))],
            usage=NS(prompt_tokens=31, completion_tokens=7, total_tokens=38)
            if len(calls) == 1
            else None,
        )

    client.adapter("default")._client = NS(chat=NS(completions=NS(create=complete)))
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda delay: None)
    result = client.evaluate(request(), operation="review.quality_score")
    assert result.usage is None
    assert len(calls) == 2
    assert client.usage_summary()["totals"]["calls"] == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 38
