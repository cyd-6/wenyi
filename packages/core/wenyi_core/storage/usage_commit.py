"""Narrow destinations shared by both recoverable usage journal adapters."""

from __future__ import annotations

import re
from typing import Any

from ..llm.usage import validate_usage


def usage_commit_key(key: str) -> str:
    """Allow ledgers and content-addressed quality response receipts, never arbitrary state."""
    if key == "usage.json" or re.fullmatch(r"reviews/review-[A-Za-z0-9_-]+/usage\.json", key):
        return key
    if re.fullmatch(r"reviews/review-[A-Za-z0-9_-]+/quality/responses/[a-f0-9]{64}\.json", key):
        return key
    raise ValueError("Invalid usage journal destination")


def usage_commit_value(key: str, value: Any) -> dict:
    """Validate ledgers while preserving the response paired with their actual usage."""
    usage_commit_key(key)
    if "/quality/responses/" in key:
        if not isinstance(value, dict):
            raise ValueError("Invalid quality response receipt")
        return value
    return validate_usage(value)
