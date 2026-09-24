"""TypeSafe's native System One protocol, with no SDK or nested retries."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ..configuration import ModelConfig
from ..judgments import (
    JudgmentContextOverflow,
    JudgmentProtocolError,
    JudgmentRequest,
    JudgmentResult,
    JudgmentUsage,
    encoded_size,
)
from ..transport import Messages, ProviderAdapter, RequestContext, ResolvedModel


class TypeSafeOptions(BaseModel):
    """System One has no generation, temperature or reasoning parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _Object(dict):
    """Retain duplicate-key evidence so an independent valid usage block is still billed."""

    def __init__(self, pairs):
        super().__init__()
        self.duplicates: set[str] = set()
        for key, value in pairs:
            if key in self:
                self.duplicates.add(key)
            self[key] = value


def _duplicates(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(getattr(value, "duplicates", ())) or any(_duplicates(v) for v in value.values())
    if isinstance(value, list):
        return any(_duplicates(v) for v in value)
    return False


class TypeSafeClient(ProviderAdapter):
    default_base_url = "https://api.typesafe.ai/v1"
    default_api_key_env = "TYPESAFE_API_KEY"
    requires_api_key = True
    # Model documentation checked 2026-09-25: 64k total, 32k state plus longest question.
    total_context_tokens = 64_000
    single_context_tokens = 32_000

    @classmethod
    def validate_model(cls, model: ModelConfig) -> None:
        if not re.fullmatch(r"jev-\d+\.\d+\.\d+", model.model):
            raise ValueError("TypeSafe requires a fixed Jev version, for example jev-1.13.0")

    @classmethod
    def output_limit(cls, options: BaseModel, hint: int | None, explicit: int | None) -> None:
        if explicit is not None or hint is not None:
            raise ValueError("TypeSafe judgments do not accept max_output_tokens")
        return None

    def _ensure_client(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None:
                self.validate_credentials()
                # HTTPX defaults to zero retries. The shared provider_retry owns backoff.
                self._client = httpx.Client(timeout=self.cfg.timeout)
        return self._client

    def _request(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        raise ValueError("TypeSafe supports judgment evaluation, not text generation")

    def validate_judgment_request(self, request: JudgmentRequest, model: ResolvedModel) -> None:
        state = encoded_size(request.state)
        questions = [
            encoded_size(q.model_dump(exclude_none=True)) for q in request.questions.values()
        ]
        # Byte estimates deliberately leave headroom; this is not Jev's exact tokenizer.
        if (
            request.estimated_input_tokens() > self.total_context_tokens
            or state + max(questions) + 256 > self.single_context_tokens
        ):
            raise JudgmentContextOverflow(
                "context_overflow: complete evidence exceeds conservative Jev context estimates "
                "(64k total or 32k state plus longest question); reduce optional context"
            )

    def _evaluate(
        self, request: JudgmentRequest, model: ResolvedModel, *, context: RequestContext
    ) -> JudgmentResult:
        client = self._ensure_client()
        payload = {"model": model.model, **request.model_dump(exclude_none=True)}
        try:
            response = client.post(
                f"{self.base_url.rstrip('/')}/systemone",
                headers={
                    "Authorization": f"Bearer {os.environ.get(self.api_key_env or '', '')}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TransportError:
            context.emit("llm_request_ambiguous", reason="transport_interrupted")
            raise
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        context.emit(
            "llm_judgment_response", request_id=request_id, status_code=response.status_code
        )
        response.raise_for_status()
        try:
            raw = json.loads(response.content, object_pairs_hook=_Object)
        except (ValueError, UnicodeDecodeError):
            context.emit("llm_usage_unknown", reason="invalid_response_json")
            raise JudgmentProtocolError("TypeSafe returned invalid JSON") from None
        usage = None
        if (
            isinstance(raw, dict)
            and "usage" not in getattr(raw, "duplicates", ())
            and not _duplicates(raw.get("usage"))
        ):
            try:
                usage = JudgmentUsage.model_validate(raw.get("usage"))
            except ValidationError:
                pass
        # Account for successful requests before validating their business answers.
        context.record_usage(usage.sample() if usage else None)
        if usage is None:
            context.emit("llm_usage_unknown", reason="invalid_response_usage")
        if _duplicates(raw):
            raise JudgmentProtocolError("TypeSafe returned duplicate JSON keys")
        if usage is None or (isinstance(raw, dict) and "provenance" in raw):
            raise JudgmentProtocolError("TypeSafe returned invalid usage or provenance")
        try:
            result = JudgmentResult.model_validate(raw).validate_for(request)
        except (ValueError, TypeError):
            raise JudgmentProtocolError(
                "TypeSafe returned an invalid or incomplete judgment"
            ) from None
        if result.model != model.model:
            raise JudgmentProtocolError("TypeSafe response does not match the pinned model version")
        if request_id:
            result = result.model_copy(update={"request_id": request_id})
        context.emit(
            "llm_judgment_received", actual_model=result.model, request_id=result.request_id
        )
        return result
