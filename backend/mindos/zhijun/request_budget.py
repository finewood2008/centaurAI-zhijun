"""Canonical output budgets, applied before request fingerprints and consent.

DeepSeek reasoning shares the completion budget with visible text. Very small
task budgets can therefore finish without an answer. Keep this compatibility
floor narrow; it is neither a retry nor a change to the requested effort.
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import ChatRequest


DEEPSEEK_MIN_OUTPUT_TOKENS = 4096


def normalize_request_budget(request: ChatRequest, provider) -> ChatRequest:
    model = getattr(provider, "model", "")
    if (
        not getattr(provider, "external", False)
        or getattr(provider, "name", "") != "openai"
        or not isinstance(model, str)
        or not model.rsplit("/", 1)[-1].lower().startswith("deepseek")
    ):
        return request
    # Leave malformed inputs to normal request validation, without coercion.
    if (type(request.max_tokens) is not int or request.max_tokens <= 0
            or request.max_tokens >= DEEPSEEK_MIN_OUTPUT_TOKENS):
        return request
    return replace(request, max_tokens=DEEPSEEK_MIN_OUTPUT_TOKENS)
