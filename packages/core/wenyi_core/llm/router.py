"""Route registered operations through reusable adapters and one usage ledger."""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import contextmanager
from threading import Lock
from uuid import uuid4

from .base import LLMClient, Messages
from .configuration import LLMConfig
from .generated_judgments import judgment_messages, parse_generated_judgment
from .judgments import JudgmentRequest, JudgmentResult
from .limits import RequestLimits
from .operations import require_operation
from .registry import provider_spec
from .retrying import is_retryable_provider_error
from .routing import ResolvedRoute, model_route, resolve_routes
from .transport import ProviderAdapter, RequestContext
from .usage import UsageSample


class RoutedLLMClient(LLMClient):
    """Freeze one routing plan per invocation; adapters never own cumulative usage."""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__()
        self.config = LLMConfig.model_validate(config.model_dump())
        self.routes = resolve_routes(self.config)
        self.limits = RequestLimits(self.config)
        self._adapters: dict[str, ProviderAdapter] = {}
        self._adapter_lock = Lock()

    def adapter(self, connection: str) -> ProviderAdapter:
        with self._adapter_lock:
            if connection not in self._adapters:
                cfg = self.config.providers[connection]
                self._adapters[connection] = provider_spec(cfg.kind).adapter_type()(cfg)
            return self._adapters[connection]

    def validate_credentials(self, operations: Iterable[str] | None = None) -> None:
        connections: set[str] = set()
        for operation in self.routes if operations is None else operations:
            require_operation(operation)
            route = self._route(operation)
            connections.add(route.provider)
            connections.update(self.config.models[profile].provider for profile in route.fallbacks)
            self._validate_token_reservation(route)
            for profile in route.fallbacks:
                self._validate_token_reservation(
                    model_route(self.config, operation, profile, origin="fallback")
                )
        for connection in sorted(connections):
            self.adapter(connection).validate_credentials()

    def cancel(self) -> None:
        self.limits.cancel()

    def _route(self, operation: str) -> ResolvedRoute:
        require_operation(operation)
        if operation not in self.routes:
            raise ValueError(
                f"{operation}: configure an explicit llm.routes.review.quality_score "
                "route to a generation model or a native judgment model"
            )
        return self.routes[operation]

    def complete(
        self,
        messages: Messages,
        *,
        operation: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        if require_operation(operation).capability != "generation":
            raise ValueError(f"{operation}: judgment operations require evaluate(), not complete()")
        primary = self._route(operation)
        result = self._execute(messages, primary, json_mode=json_mode, max_tokens=max_tokens)
        assert isinstance(result, str)
        return result

    def evaluate(self, request: JudgmentRequest, *, operation: str) -> JudgmentResult:
        if require_operation(operation).capability != "judgment":
            raise ValueError(
                f"{operation}: generation operations require complete(), not evaluate()"
            )
        result = self._execute(request, self._route(operation), json_mode=False)
        assert isinstance(result, JudgmentResult)
        return result

    def _execute(
        self,
        messages: Messages | JudgmentRequest,
        primary: ResolvedRoute,
        *,
        json_mode: bool,
        max_tokens: int | None = None,
    ) -> str | JudgmentResult:
        operation = primary.operation
        if max_tokens is not None:
            primary = model_route(
                self.config,
                operation,
                primary.profile,
                origin=primary.origin,
                tier=primary.tier,
                fallbacks=primary.fallbacks,
                output_hint=max_tokens,
            )
        routes = [
            primary,
            *(
                model_route(
                    self.config,
                    operation,
                    profile,
                    origin="explicit fallback",
                    output_hint=max_tokens,
                )
                for profile in primary.fallbacks
            ),
        ]
        call_id = uuid4().hex
        attempt_number = 0
        for position, route in enumerate(routes):
            self.limits.check()
            generated_messages = (
                judgment_messages(messages)
                if isinstance(messages, JudgmentRequest) and route.judgment_source == "generated"
                else None
            )
            metadata = {
                "call_id": call_id,
                "operation": operation,
                "stage": operation,
                "tier": route.tier or "direct",
                "profile": route.profile,
                "connection": route.provider,
                "provider": route.provider_kind,
                "model": route.model,
                "inference_fingerprint": route.fingerprint,
            }
            if route.judgment_source is not None:
                metadata["judgment_source"] = route.judgment_source

            def emit(event: str, **payload) -> None:
                self._emit_event(event, **{**metadata, "attempt": attempt_number, **payload})

            emit("llm_request_scheduled")
            active_reservation = None
            last_sample: UsageSample | None = None

            def record(sample: UsageSample | None) -> None:
                nonlocal last_sample
                last_sample = sample
                if sample is not None and active_reservation is not None:
                    active_reservation.actual_tokens = sample.total_tokens
                self.usage.record(
                    route.tier or "direct",
                    sample,
                    operation,
                    provider=route.provider_identity,
                    model=route.model_identity,
                    labels={
                        route.provider_identity: f"{route.provider_kind} {route.endpoint or ''}".strip(),
                        route.model_identity: f"{route.provider_kind} / {route.model}",
                    },
                )

            @contextmanager
            def attempt_scope():
                nonlocal active_reservation, attempt_number, last_sample
                self._validate_token_reservation(route)
                if isinstance(messages, JudgmentRequest) and generated_messages is None:
                    estimate = (
                        messages.estimated_input_tokens() + messages.estimated_output_tokens()
                    )
                else:
                    estimate = (
                        len(
                            json.dumps(generated_messages or messages, ensure_ascii=False).encode(
                                "utf-8"
                            )
                        )
                        + 256
                        + (route.max_output_tokens or 0)
                    )
                with self.limits.attempt(route.provider, estimate, emit) as reservation:
                    active_reservation = reservation
                    last_sample = None
                    attempt_number += 1
                    emit("llm_request_started", estimated_tokens=estimate)
                    try:
                        yield
                    finally:
                        active_reservation = None

            context = RequestContext(
                operation,
                route.tier or "direct",
                route.max_output_tokens,
                emit,
                record,
                attempt_scope,
                self.limits.wait_for_retry,
            )
            try:
                adapter = self.adapter(route.provider)
                if isinstance(messages, JudgmentRequest):
                    if generated_messages is not None:
                        content = adapter.generate(
                            generated_messages,
                            route.request_model(),
                            json_mode=True,
                            context=context,
                        )
                        emit("llm_judgment_response", request_id=None)
                        if last_sample is None:
                            emit("llm_usage_unknown", reason="missing_generation_usage")
                        result = parse_generated_judgment(
                            content, messages, model=route.model, usage=last_sample
                        )
                        emit(
                            "llm_judgment_received", actual_model=None, requested_model=route.model
                        )
                    else:
                        result = adapter.evaluate(
                            messages.model_copy(deep=True), route.request_model(), context=context
                        )
                else:
                    result = adapter.generate(
                        [dict(message) for message in messages],
                        route.request_model(),
                        json_mode=json_mode,
                        context=context,
                    )
            except Exception as error:
                emit("llm_request_failed", error_type=type(error).__name__)
                if position == len(routes) - 1 or not is_retryable_provider_error(error):
                    raise
                emit("llm_model_failover", next_profile=routes[position + 1].profile)
            else:
                emit("llm_request_completed")
                return result
        raise RuntimeError("No model route was selected")

    def _validate_token_reservation(self, route: ResolvedRoute) -> None:
        if route.judgment_source == "native":
            return  # Structured answers have a request-specific reservation, not a generation cap.
        connection = self.config.providers[route.provider]
        quota = self.config.quotas.get(connection.quota_group or "")
        if (
            self.config.budget.max_tokens or (quota and quota.tokens_per_minute)
        ) and route.max_output_tokens is None:
            raise ValueError(
                f"{route.operation}: token limits require an explicit max_output_tokens"
            )
