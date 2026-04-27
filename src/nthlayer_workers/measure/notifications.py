"""Slack block builders for nthlayer-measure verdicts."""
from __future__ import annotations


def build_breach_blocks(verdict) -> tuple[list[dict], str]:
    """Build Slack blocks for SLO breach notification.

    Returns (blocks, fallback_text).
    """
    custom = getattr(verdict.metadata, "custom", {}) or {}
    service = verdict.subject.ref or "unknown"
    slo_name = custom.get("slo_name", "SLO")
    current = custom.get("current_value")
    target = custom.get("target")
    consecutive = custom.get("consecutive")
    confidence = verdict.judgment.confidence

    current_pct = f"{current * 100:.1f}%" if current is not None else "?"
    target_pct = f"{target * 100:.1f}%" if target is not None else "?"

    text = f"\u26a0 SLO breach: {service} {slo_name} {current_pct} (target <{target_pct})"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*\u26a0 SLO BREACH \u00b7 {service}*",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{slo_name}:* {current_pct} (target <{target_pct})\n"
                    + (f"Consecutive breaches: {consecutive}\n" if consecutive else "")
                    + "NthLayer detected AI decision quality degradation on this judgment SLO."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"nthlayer-measure \u00b7 confidence {confidence:.2f} \u00b7 {verdict.id}",
                },
            ],
        },
    ]

    return blocks, text
