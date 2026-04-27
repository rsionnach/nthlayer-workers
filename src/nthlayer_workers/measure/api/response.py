"""Response builder — converts internal verdicts to simplified external responses.

Orchestrators don't need to understand the full verdict schema.
They need: action, score, confidence, reasoning, and optionally governance.
"""
from __future__ import annotations

from typing import Any


def build_response(
    verdict,
    governance: dict | None = None,
) -> dict[str, Any]:
    """Build simplified external response from a verdict.

    Returns dict with: verdict_id, action, score, confidence,
    dimensions, reasoning, risk_tier, and optionally governance block.
    """
    judgment = verdict.judgment
    metadata = getattr(verdict.metadata, "custom", {}) or {}

    response: dict[str, Any] = {
        "verdict_id": verdict.id,
        "action": judgment.action,
        "score": judgment.score,
        "confidence": judgment.confidence,
        "dimensions": judgment.dimensions or {},
        "reasoning": judgment.reasoning or "",
        "risk_tier": metadata.get("risk_tier", "standard"),
    }

    if governance is not None:
        response["governance"] = governance

    return response


def build_error_response(
    status_code: int,
    message: str,
    details: dict | None = None,
) -> dict[str, Any]:
    """Standard error response format."""
    response: dict[str, Any] = {
        "error": message,
        "status": status_code,
    }
    if details is not None:
        response["details"] = details
    return response
