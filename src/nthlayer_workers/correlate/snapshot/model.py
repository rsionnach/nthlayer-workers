"""Model interface — the ZFC judgment boundary.

Used by: the `serve` and `replay` CLI subcommands (continuous snapshot generation).
See also: reasoning.py which serves the `correlate` subcommand (live triggered).

Output shape (opensrm-saun.1.2.1): correlation_snapshot **assessments**, not
verdicts. ``correlation_snapshot`` is in ASSESSMENT_KINDS per the v1.5
architectural decision (verdicts are decisions, assessments are continuous
observations). The hot-path worker (correlate/worker.py) emits the same
shape via ``submit_assessment``; this module aligns the cold-path CLI with
that primitive.

Returns ``list[Assessment]`` (the dataclass from observe/assessment.py),
which means an ``assessment_store`` argument can be a real
``SQLiteAssessmentStore`` — its ``.put()`` accepts the dataclass directly.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

from nthlayer_common.prompts import load_prompt
from nthlayer_workers.correlate.types import CorrelationGroup
from nthlayer_workers.observe.assessment import Assessment

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "snapshot.yaml"

logger = structlog.get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(service: str | None) -> str:
    """Build a correlation_snapshot assessment id matching the worker's pattern."""
    svc = service or "snapshot"
    return f"csn-{svc}-{uuid.uuid4().hex[:8]}"


def _build_child_assessment(parsed: dict) -> Assessment:
    """One assessment per correlation group (the child rows)."""
    service = parsed.get("service") or "unknown"
    return Assessment(
        id=_new_id(service),
        created_at=_now(),
        kind="correlation_snapshot",
        service=service,
        producer="nthlayer-correlate",
        data={
            "group_id": parsed.get("group_id", ""),
            "summary": parsed.get("summary", ""),
            "action": parsed.get("action", "flag"),
            "confidence": parsed.get("confidence", 0.5),
            "reasoning": parsed.get("reasoning", ""),
            "tags": parsed.get("tags", []),
        },
    )


def _build_parent_assessment(
    children: list[Assessment],
    *,
    group_count: int,
    degraded: bool = False,
) -> Assessment:
    """Roll-up assessment summarising the snapshot across all groups."""
    if degraded:
        overall_action = "flag"
        overall_confidence = 0.0
        reasoning = "template-based, model unavailable"
        rolled_tags: list[str] = ["degraded"]
        ref_summary = f"Degraded snapshot: {group_count} group(s), no model assessment"
    else:
        overall_action = "escalate" if any(
            c.data.get("action") == "escalate" for c in children
        ) else "flag"
        overall_confidence = (
            sum(c.data.get("confidence", 0.5) for c in children) / len(children)
            if children else 0.0
        )
        reasoning = f"Assessed {len(children)} correlation group(s)"
        collected: list[str] = []
        for c in children:
            collected.extend(c.data.get("tags", []))
        rolled_tags = sorted(set(collected))
        ref_summary = f"Snapshot: {group_count} group(s), {len(children)} assessed"

    return Assessment(
        id=_new_id(None),
        created_at=_now(),
        kind="correlation_snapshot",
        # Sentinel matching the hot-path worker's portfolio_status convention:
        # rolled-up assessments have no single owning service.
        service="__snapshot__",
        producer="nthlayer-correlate",
        data={
            "summary": ref_summary,
            "action": overall_action,
            "confidence": overall_confidence,
            "reasoning": reasoning,
            "tags": rolled_tags,
            # parent_ids: list of child assessment ids (lineage anchor for
            # downstream consumers walking from parent to child rows).
            "parent_ids": [c.id for c in children],
        },
    )


class ModelInterface:
    def __init__(self, model: str | None = None, max_tokens: int = 4096):
        self._model = model  # None → llm_call uses NTHLAYER_MODEL env var
        self._max_tokens = max_tokens

    async def interpret(
        self,
        prompt: str,
        groups: list[CorrelationGroup],
        assessment_store=None,
    ) -> list[Assessment]:
        """Call model, parse response into correlation_snapshot assessments.

        ``assessment_store`` is any object with a ``put(Assessment)`` method
        (typically ``SQLiteAssessmentStore``). When ``None``, the caller
        receives the assessments and is responsible for persistence. Pre-
        saun.1.2.1 callers passed a ``verdict_store``; the parameter rename
        is not source-compatible with legacy callers (which were already
        broken — they relied on a deprecated ``nthlayer_learn.store``
        import).

        Returns ``list[Assessment]`` (children + parent). When ``groups``
        is empty, returns ``[]`` — no parent assessment is emitted because
        a roll-up over zero children is vacuous.
        """
        if not groups:
            return []

        try:
            response = await self._call_model(prompt)
            parsed = self._parse_response(response, groups)
        except Exception as e:
            logger.warning("model_call_failed", error=str(e))
            return self._create_template_assessments(groups)

        children = [_build_child_assessment(p) for p in parsed]
        if assessment_store is not None:
            for c in children:
                assessment_store.put(c)

        parent = _build_parent_assessment(children, group_count=len(groups))
        if assessment_store is not None:
            assessment_store.put(parent)

        return children + [parent]

    async def _call_model(self, prompt: str) -> str:
        """Call the LLM via the shared nthlayer-common wrapper."""
        import asyncio
        from nthlayer_common.llm import llm_call

        result = await asyncio.to_thread(
            llm_call,
            system=self._build_system_prompt(),
            user=prompt,
            model=self._model,
            max_tokens=self._max_tokens,
        )
        return result.text

    def _build_system_prompt(self) -> str:
        spec = load_prompt(_PROMPT_PATH)
        return spec.system

    def _parse_response(self, response_text: str, groups: list[CorrelationGroup]) -> list[dict]:
        """Parse model JSON response."""
        from nthlayer_common.parsing import clamp, strip_markdown_fences

        text = strip_markdown_fences(response_text)
        data = json.loads(text)
        assessments = data.get("assessments", [])

        # Validate actions and clamp confidence before assessment construction.
        valid_actions = {"flag", "escalate", "defer"}
        for a in assessments:
            if a.get("action") not in valid_actions:
                a["action"] = "flag"
            a["confidence"] = clamp(float(a.get("confidence", 0.5)))

        return assessments

    def _create_template_assessments(self, groups: list[CorrelationGroup]) -> list[Assessment]:
        """Degraded mode: template-based assessments with confidence 0.0."""
        children: list[Assessment] = []
        for group in groups:
            service = group.services[0] if group.services else "unknown"
            children.append(Assessment(
                id=_new_id(service),
                created_at=_now(),
                kind="correlation_snapshot",
                service=service,
                producer="nthlayer-correlate",
                data={
                    "group_id": group.id,
                    "summary": group.summary,
                    "action": "flag",
                    "confidence": 0.0,
                    "reasoning": "template-based, model unavailable",
                    "tags": ["degraded"],
                },
            ))
        parent = _build_parent_assessment(children, group_count=len(groups), degraded=True)
        return children + [parent]
