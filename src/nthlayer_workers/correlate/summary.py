"""NL summary generation for correlation snapshots.

Uses structured_call() from nthlayer-common with Instructor for validated
Pydantic output. LLM failure is non-blocking — returns None on timeout,
validation error, or LLM unavailability.

structured_call() is synchronous in v1.5; wrapped with asyncio.to_thread().
v2 LLM class refactor (V2-F deferred) makes this native-async.
"""

from __future__ import annotations

import asyncio

import structlog
from nthlayer_common.llm_structured import structured_call
from pydantic import BaseModel, Field

from nthlayer_workers.correlate.types import SitRepEvent

logger = structlog.get_logger()

SYSTEM_PROMPT = (
    "You are summarizing a correlation window for an on-call SRE operator. "
    "Describe what happened in 2-4 sentences. Be specific about services, "
    "metrics, and timeline. Do NOT speculate about root cause — describe "
    "observations only. If you lack context to be specific, say what's "
    "missing in notable_omissions. Provide a confidence score from 0.0 to "
    "1.0 reflecting how grounded the summary is in the evidence: 1.0 means "
    "every claim cites a sample event; 0.0 means the summary is generic. "
    "When notable_omissions is non-empty, confidence should drop accordingly."
)

SUMMARY_TIMEOUT = 5.0  # seconds


class SnapshotSummary(BaseModel):
    """Operator-legible summary of a correlation window.

    2-4 sentences describing what happened. No root-cause speculation —
    summary describes observations, not conclusions. Confidence reflects
    how grounded the summary is in the supplied sample events
    (opensrm-w7q.3 acceptance: 0.0-1.0).
    """

    summary: str = Field(max_length=500)
    notable_omissions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


async def generate_summary(
    snapshot_data: dict,
    events: list[SitRepEvent],
    model: str | None = None,
) -> dict | None:
    """Generate NL summary for a correlation snapshot.

    Returns {"summary": "...", "notable_omissions": [...]} on success,
    or None on any failure (timeout, LLM unavailable, validation error).
    """
    user_prompt = _build_user_prompt(snapshot_data, events)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                structured_call,
                system=SYSTEM_PROMPT,
                user=user_prompt,
                response_model=SnapshotSummary,
                model=model,
                timeout=SUMMARY_TIMEOUT,
                max_retries=1,  # single attempt — retry not worth operator latency
            ),
            # Outer timeout is safety net — inner structured_call timeout
            # should fire first so Instructor can surface a clean LLMError.
            timeout=SUMMARY_TIMEOUT + 1.0,
        )
        return {
            "summary": result.summary,
            "notable_omissions": result.notable_omissions,
            "confidence": result.confidence,
        }
    except Exception as e:
        _record_failure(e, snapshot_data.get("domain", {}).get("service", "unknown"), model)
        return None


def _build_user_prompt(snapshot_data: dict, events: list[SitRepEvent]) -> str:
    """Build the user prompt from snapshot data and sample events."""
    domain = snapshot_data.get("domain", {})
    window = snapshot_data.get("window", {})
    samples = _select_sample_events(events)

    lines = [
        f"Service: {domain.get('service', 'unknown')} ({domain.get('environment', 'unknown')})",
        f"Window: {window.get('duration_seconds', 0):.0f}s, closed by {window.get('close_reason', 'unknown')}",
        f"Events: {snapshot_data.get('event_count', 0)} total",
        f"Event types: {snapshot_data.get('event_types', {})}",
        f"Peak severity: {snapshot_data.get('peak_severity', 0.0)}",
        f"Affected services: {', '.join(snapshot_data.get('affected_services', []))}",
        "",
        "Sample events:",
    ]
    for s in samples:
        lines.append(
            f"  [{s['timestamp']}] {s['service']} {s['type']} severity={s['severity']} source={s['source']}"
        )

    return "\n".join(lines)


def _select_sample_events(events: list[SitRepEvent], max_samples: int = 10) -> list[dict]:
    """Select representative events for the LLM prompt.

    Includes: first event, most severe event, most recent per service.
    Deduplicates by ID. Caps at max_samples.
    """
    if not events:
        return []

    samples: dict[str, SitRepEvent] = {}

    # First event (chronologically)
    sorted_events = sorted(events, key=lambda e: e.timestamp)
    samples[sorted_events[0].id] = sorted_events[0]

    # Most severe event
    most_severe = max(events, key=lambda e: e.severity)
    samples[most_severe.id] = most_severe

    # Most recent per service
    by_service: dict[str, SitRepEvent] = {}
    for e in sorted_events:
        by_service[e.service] = e  # last wins = most recent
    for e in by_service.values():
        samples[e.id] = e

    result = list(samples.values())[:max_samples]
    return [
        {
            "service": e.service,
            "type": e.type.value,
            "severity": e.severity,
            "timestamp": e.timestamp,
            "source": e.source,
        }
        for e in result
    ]


def _record_failure(error: Exception, snapshot_service: str, model: str | None = None) -> None:
    """Record summary generation failure via logging + metrics."""
    reason = _classify_failure(error)
    resolved_model = model or "default"
    logger.warning(
        "correlate_summary_failed",
        reason=reason,
        service=snapshot_service,
        error=str(error),
    )
    try:
        from nthlayer_common.metrics import errors_total
        errors_total.labels(component="correlate", error_type=f"summary_{reason}").inc()
    except Exception:
        pass  # metrics infra may not be configured
    try:
        from nthlayer_common.telemetry import emit_llm_event
        emit_llm_event(
            model=resolved_model,
            provider="unknown",
            caller="correlate.generate_summary",
            success=False,
            error=str(error),
        )
    except Exception:
        pass  # OTel may not be configured


def _classify_failure(error: Exception) -> str:
    """Classify a summary generation failure for metrics."""
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    error_str = type(error).__name__.lower()
    if "validation" in error_str or "pydantic" in error_str:
        return "validation_error"
    return "llm_unavailable"
