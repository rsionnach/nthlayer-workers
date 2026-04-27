"""Slack block builders for nthlayer-correlate verdicts."""
from __future__ import annotations


def build_correlation_blocks(verdict) -> tuple[list[dict], str]:
    """Build Slack blocks for root cause identification.

    Returns (blocks, fallback_text).
    """
    custom = getattr(verdict.metadata, "custom", {}) or {}
    service = verdict.subject.ref or "unknown"
    root_causes = custom.get("root_causes", [])
    blast_radius = custom.get("blast_radius", [])
    confidence = verdict.judgment.confidence

    rc_text = root_causes[0].get("service", "unknown") if root_causes else "under investigation"
    blast_count = len(blast_radius)
    blast_services = ", ".join(
        b.get("service", b) if isinstance(b, dict) else b
        for b in blast_radius[:5]
    )

    text = f"\U0001f50d Root cause: {rc_text} \u2014 {blast_count} services in blast radius"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*\U0001f50d ROOT CAUSE IDENTIFIED \u00b7 {service}*",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Root cause:* {rc_text}\n"
                    f"*Blast radius:* {blast_count} services \u2014 {blast_services}\n"
                    "NthLayer correlated the breach with the service dependency graph."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"nthlayer-correlate \u00b7 confidence {confidence:.2f} \u00b7 {verdict.id}",
                },
            ],
        },
    ]

    return blocks, text


def find_slack_thread_ts(verdict_store, verdict_ids: list[str]) -> str | None:
    """Walk verdict lineage to find slack_thread_ts from the earliest verdict.

    Returns None if no thread_ts found in lineage.
    """
    for vid in verdict_ids:
        try:
            v = verdict_store.get(vid)
            if v is None:
                continue
            custom = getattr(v.metadata, "custom", {}) or {}
            ts = custom.get("slack_thread_ts")
            if ts:
                return ts
            # Walk up lineage
            for ctx_id in (v.lineage.context or []):
                try:
                    ctx_v = verdict_store.get(ctx_id)
                    if ctx_v:
                        ctx_custom = getattr(ctx_v.metadata, "custom", {}) or {}
                        ts = ctx_custom.get("slack_thread_ts")
                        if ts:
                            return ts
                except Exception:
                    pass
        except Exception:
            pass
    return None
