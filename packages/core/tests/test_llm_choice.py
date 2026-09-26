"""Native JEV and generic JSON choices share routing, accounting and cancellation."""

import json

import httpx
import pytest
from wenyi_core.llm.choice import (
    ChoiceProtocolError,
    ChoiceRequest,
    ChoiceUnavailable,
    parse_choice,
)
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.providers.fake import FakeProvider
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.llm.usage import UsageSample


def choice_config(**extra):
    return LLMConfig.model_validate(
        {
            "preset": "fake",
            "providers": {"jev": {"kind": "typesafe", "max_retries": 0}},
            "models": {"judge": {"provider": "jev", "model": "jev-1.13.0"}},
            "routes": {"translation.judge": {"model": "judge"}},
            **extra,
        }
    )


def request():
    return ChoiceRequest(
        {"source": ["Original"]},
        "Select the best complete translation",
        {
            "A": ["Alpha"],
            "B": ["Beta"],
            "C": ["Gamma"],
        },
    )


def install_transport(client, handler):
    adapter = client.adapter("jev")
    adapter._client = httpx.Client(transport=httpx.MockTransport(handler))
    return adapter


def fake_choice(self, messages, model, *, json_mode, context):
    context.record_usage(UsageSample(3, 2, 5))
    return '{"choice":"B"}'


def test_native_choice_protocol_and_usage(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-placeholder")
    client = RoutedLLMClient(choice_config())
    seen = []

    def respond(req):
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "selection": {
                        "type": "choice",
                        "choice": "C",
                        "confidence": 0.8,
                        "probabilities": {"A": 0.1, "B": 0.1, "C": 0.8},
                    }
                },
                "usage": {"input_tokens": 123, "output_tokens": 12},
            },
        )

    install_transport(client, respond)
    result = client.choose(request(), operation="translation.judge")
    assert result.choice == "C" and result.confidence == 0.8
    assert result.model == "jev-1.13.0" and result.provider == "typesafe"
    assert not result.fallback_used
    payload = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert payload["questions"]["selection"]["criteria"] == request().options
    assert set(payload) == {"state", "model", "questions"}
    usage = client.usage_summary()["by_stage"]["translation.judge"]
    assert (usage["calls"], usage["prompt_tokens"], usage["completion_tokens"]) == (1, 123, 12)


@pytest.mark.parametrize("failure", ["credential", "http", "malformed", "oversize"])
def test_unavailable_judge_falls_back_without_truncating(monkeypatch, failure):
    monkeypatch.setattr(FakeProvider, "_request", fake_choice)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    if failure != "credential":
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-placeholder")
    client = RoutedLLMClient(choice_config())
    events = []
    client.set_event_sink(lambda name, **data: events.append((name, data)))
    seen = []

    def respond(req):
        seen.append(req)
        status = {"http": 503, "oversize": 413}.get(failure, 200)
        return httpx.Response(status, json={"answers": {}})

    install_transport(client, respond)
    decision = request()
    if failure == "oversize":
        decision.state["source"] = ["x" * 40_000]
    client.validate_credentials(["translation.judge"])
    result = client.choose(decision, operation="translation.judge")
    assert result.choice == "B" and result.fallback_used
    assert result.provider == "fake"
    assert any(name == "llm_model_failover" for name, _ in events)
    if failure == "credential":
        assert seen == []
    if failure == "oversize":
        assert json.loads(seen[0].content)["state"] == decision.state
    assert all("test-only-placeholder" not in str(data) for _, data in events)


def test_fallback_order_preview_and_physical_deduplication():
    config = choice_config(
        models={
            "judge": {"provider": "jev", "model": "jev-1.13.0"},
            "other": {"provider": "default", "model": "other"},
            "strong_alias": {"provider": "default", "model": "fake"},
        },
        routes={"translation.judge": {"model": "judge", "fallbacks": ["other", "strong_alias"]}},
    )
    client = RoutedLLMClient(config)
    assert client.routes["translation.judge"].fallbacks == ("other", "strong_alias")


@pytest.mark.parametrize("where", ["model", "fallback", "tier"])
def test_jev_cannot_generate_text(where):
    override = {"routes": {"translation.body": {"model": "judge"}}}
    if where == "fallback":
        override = {"routes": {"translation.body": {"tier": "strong", "fallbacks": ["judge"]}}}
    if where == "tier":
        override = {"tiers": {"strong": "judge"}}
    with pytest.raises(ValueError, match="supports choices only"):
        choice_config(**override)


@pytest.mark.parametrize("stopped", ["cancelled", "budget"])
def test_stop_does_not_trigger_a_second_model(monkeypatch, stopped):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-placeholder")
    config = choice_config(budget={"max_requests": 1})
    client = RoutedLLMClient(config)
    seen = []
    install_transport(client, lambda req: seen.append(req) or httpx.Response(503))
    if stopped == "cancelled":
        client.cancel()
        with pytest.raises(RequestCancelled):
            client.choose(request(), operation="translation.judge")
        assert not seen
    else:
        with pytest.raises(RequestStopped):
            client.choose(request(), operation="translation.judge")
        assert len(seen) == 1
    assert client.limits.requests <= 1


def test_all_judges_fail_without_picking_a_default(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client = RoutedLLMClient(choice_config())
    with pytest.raises(ChoiceUnavailable):
        client.choose(request(), operation="translation.judge")


def test_missing_credentials_do_not_spend_the_fallback_request_budget(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(FakeProvider, "_request", fake_choice)
    client = RoutedLLMClient(choice_config(budget={"max_requests": 1}))
    result = client.choose(request(), operation="translation.judge")
    assert result.fallback_used and result.choice == "B"
    assert client.limits.requests == 1


def test_explicit_fallback_precedes_strong_and_invalid_decision_advances(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    attempts = []

    def respond(self, messages, model, *, json_mode, context):
        attempts.append(model.model)
        return '{"choice":"D"}' if model.model == "backup" else '{"choice":"C"}'

    monkeypatch.setattr(FakeProvider, "_request", respond)
    config = choice_config(
        models={
            "judge": {"provider": "jev", "model": "jev-1.13.0"},
            "backup": {"provider": "default", "model": "backup"},
        },
        routes={"translation.judge": {"model": "judge", "fallbacks": ["backup"]}},
    )
    client = RoutedLLMClient(config)
    client.validate_credentials(["translation.judge"])
    result = client.choose(request(), operation="translation.judge")
    assert result.choice == "C" and result.fallback_used
    assert attempts == ["backup", "fake"]


def test_local_choice_configuration_error_never_uses_fallback(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-placeholder")
    client = RoutedLLMClient(choice_config())
    monkeypatch.setattr(FakeProvider, "_request", lambda *_a, **_kw: pytest.fail("Used fallback"))
    invalid = ChoiceRequest({}, "Choose one", {str(i): None for i in range(256)})
    with pytest.raises(ValueError, match="255 options"):
        client.choose(invalid, operation="translation.judge")


@pytest.mark.parametrize(
    "value",
    [
        {"choice": []},
        {"choice": "D"},
        {"choice": "A", "confidence": True},
        {"choice": "B", "probabilities": {"A": 1}},
        {"choice": "A", "confidence": float("nan")},
    ],
)
def test_invalid_choice_is_rejected(value):
    with pytest.raises(ChoiceProtocolError):
        parse_choice(value, request())


def test_native_retries_share_limits_and_retry_events(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-placeholder")
    config = choice_config(providers={"jev": {"kind": "typesafe", "max_retries": 1}})
    client = RoutedLLMClient(config)
    attempts = []

    def respond(req):
        attempts.append(req)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {
                    "selection": {
                        "type": "choice",
                        "choice": "A",
                        "confidence": 1,
                        "probabilities": {"A": 1, "B": 0, "C": 0},
                    }
                },
            },
        )

    install_transport(client, respond)
    result = client.choose(request(), operation="translation.judge")
    assert result.choice == "A" and not result.fallback_used
    assert client.limits.requests == 2
