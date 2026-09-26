"""Route registered operations through reusable adapters and one usage ledger."""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from uuid import uuid4

from .base import LLMClient, Messages
from .choice import (
    ChoiceInputTooLarge,
    ChoiceProtocolError,
    ChoiceRequest,
    ChoiceResult,
    ChoiceUnavailable,
)
from .configuration import LLMConfig
from .limits import RequestLimits
from .operations import require_operation
from .registry import provider_spec
from .retrying import error_status_code, is_retryable_provider_error
from .routing import ResolvedRoute, model_route, resolve_routes
from .transport import ProviderAdapter, ProviderCredentialsError, RequestContext
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
            spec = require_operation(operation)
            route = self.routes[operation]
            if spec.request_kind == "choice":
                errors = []
                for profile in (route.profile, *route.fallbacks):
                    candidate = model_route(self.config, operation, profile, origin="check")
                    self._validate_token_reservation(candidate)
                    try:
                        self.adapter(candidate.provider).validate_credentials()
                    except ProviderCredentialsError as error:
                        errors.append(error)
                    else:
                        break
                else:
                    details = "; ".join(str(error) for error in errors)
                    raise ChoiceUnavailable(
                        f"No judge has usable credentials: {details}"
                    ) from errors[-1]
                continue
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

    def complete(
        self,
        messages: Messages,
        *,
        operation: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        require_operation(operation)
        primary = self.routes[operation]
        if require_operation(operation).request_kind != "text":
            raise ValueError(f"{operation} requires the choice interface")
        result = self._complete(messages, primary, json_mode=json_mode, max_tokens=max_tokens)
        assert isinstance(result, str)
        return result

    def choose(self, request: ChoiceRequest, *, operation: str) -> ChoiceResult:
        if require_operation(operation).request_kind != "choice":
            raise ValueError(f"{operation} is not a choice operation")
        result = self._complete(
            request.messages(), self.routes[operation], json_mode=True, choice=request
        )
        assert isinstance(result, ChoiceResult)
        return result

    def _complete(
        self,
        messages: Messages,
        primary: ResolvedRoute,
        *,
        json_mode: bool,
        max_tokens: int | None = None,
        choice: ChoiceRequest | None = None,
    ) -> str | ChoiceResult:
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

            def emit(event: str, **payload) -> None:
                self._emit_event(event, **{**metadata, "attempt": attempt_number, **payload})

            emit("llm_request_scheduled")
            active_reservation = None

            def record(sample: UsageSample | None) -> None:
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
                nonlocal active_reservation, attempt_number
                self._validate_token_reservation(route)
                estimate = (
                    len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
                    + 256
                    + (route.max_output_tokens or 0)
                )
                with self.limits.attempt(route.provider, estimate, emit) as reservation:
                    active_reservation = reservation
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
                if choice is not None:
                    # A missing local credential is not a remote attempt and consumes no quota.
                    adapter.validate_credentials()
                    result = adapter.choose(choice, route.request_model(), context=context)
                    result = replace(
                        result,
                        model=result.model or route.model,
                        provider=route.provider_kind,
                        fingerprint=route.fingerprint,
                        fallback_used=position > 0,
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
                recoverable = is_retryable_provider_error(error)
                if choice is not None:
                    recoverable = (
                        recoverable
                        or isinstance(
                            error,
                            (ChoiceProtocolError, ChoiceInputTooLarge, ProviderCredentialsError),
                        )
                        or error_status_code(error) is not None
                    )
                if not recoverable:
                    raise
                if position == len(routes) - 1:
                    if choice is not None:
                        raise ChoiceUnavailable(
                            "All translation judges failed; saved candidates remain resumable. "
                            "Check credentials and service availability; reduce "
                            "segment.max_tokens_per_batch if the complete evidence is too large."
                        ) from error
                    raise
                emit("llm_model_failover", next_profile=routes[position + 1].profile)
            else:
                emit("llm_request_completed")
                return result
        raise RuntimeError("No model route was selected")

    def _validate_token_reservation(self, route: ResolvedRoute) -> None:
        connection = self.config.providers[route.provider]
        quota = self.config.quotas.get(connection.quota_group or "")
        if (
            self.config.budget.max_tokens or (quota and quota.tokens_per_minute)
        ) and route.max_output_tokens is None:
            raise ValueError(
                f"{route.operation}: token limits require an explicit max_output_tokens"
            )
