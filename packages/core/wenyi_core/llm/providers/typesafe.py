"""TypeSafe's native System One Choice API, without a chat compatibility shim."""

from __future__ import annotations

import os
from dataclasses import replace

import httpx
from pydantic import BaseModel, ConfigDict

from ..choice import (
    ChoiceInputTooLarge,
    ChoiceProtocolError,
    ChoiceRequest,
    ChoiceResult,
    parse_choice,
)
from ..transport import Messages, ProviderAdapter, RequestContext, ResolvedModel
from ..usage import UsageSample, read_usage_int


class TypeSafeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class TypeSafeClient(ProviderAdapter):
    default_base_url = "https://api.typesafe.ai/v1"
    default_api_key_env = "TYPESAFE_API_KEY"
    requires_api_key = True
    supports_text = False

    def _request(
        self, messages: Messages, model: ResolvedModel, *, json_mode: bool, context: RequestContext
    ) -> str:
        raise ValueError("TypeSafe supports closed-set choices, not text generation")

    def _choose(
        self, request: ChoiceRequest, model: ResolvedModel, *, context: RequestContext
    ) -> ChoiceResult:
        self.validate_credentials()
        if len(request.options) > 255:
            raise ValueError("TypeSafe Choice supports at most 255 options")
        payload = {
            "model": model.model,
            "state": request.state,
            "questions": {
                "selection": {
                    "type": "choice",
                    "instructions": request.instructions,
                    "criteria": request.options,
                }
            },
        }
        # Send all evidence; let the service enforce its actual tokenizer and model window.
        # A local byte cutoff would unnecessarily reject many valid multilingual requests.
        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.cfg.timeout)
        response = self._client.post(
            f"{self.base_url.rstrip('/')}/systemone",
            headers={"Authorization": f"Bearer {os.environ[self.api_key_env]}"},
            json=payload,
        )
        if response.status_code == 413:
            raise ChoiceInputTooLarge(
                "The complete choice exceeds the TypeSafe context window; "
                "reduce segment.max_tokens_per_batch or select another judge"
            )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as error:
            context.record_usage(None)
            raise ChoiceProtocolError("TypeSafe returned invalid JSON") from error
        usage = data.get("usage") if isinstance(data, dict) else None
        context.record_usage(
            UsageSample(
                read_usage_int(usage, "input_tokens"),
                read_usage_int(usage, "output_tokens"),
                read_usage_int(usage, "input_tokens") + read_usage_int(usage, "output_tokens"),
            )
            if isinstance(usage, dict)
            else None
        )
        answers = data.get("answers") if isinstance(data, dict) else None
        answer = answers.get("selection") if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ChoiceProtocolError("TypeSafe did not return a Choice answer")
        if answer.get("confidence") is None or answer.get("probabilities") is None:
            raise ChoiceProtocolError("TypeSafe Choice is missing its probability distribution")
        result = parse_choice(answer, request)
        actual_model = data.get("model")
        if not isinstance(actual_model, str) or not actual_model:
            raise ChoiceProtocolError("TypeSafe did not identify the evaluated model")
        return replace(result, model=actual_model)
