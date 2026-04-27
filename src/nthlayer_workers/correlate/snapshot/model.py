"""Model interface — the ZFC judgment boundary.

Used by: the `serve` and `replay` CLI subcommands (continuous snapshot generation).
See also: reasoning.py which serves the `correlate` subcommand (live triggered).
"""
from __future__ import annotations

import json
import structlog
from pathlib import Path

from nthlayer_common.prompts import load_prompt
from nthlayer_workers.correlate.types import CorrelationGroup

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "snapshot.yaml"

logger = structlog.get_logger()


class ModelInterface:
    def __init__(self, model: str | None = None, max_tokens: int = 4096):
        self._model = model  # None → llm_call uses NTHLAYER_MODEL env var
        self._max_tokens = max_tokens

    async def interpret(
        self,
        prompt: str,
        groups: list[CorrelationGroup],
        verdict_store=None,
    ) -> list:
        """Call model, parse response into verdicts.

        Returns list of Verdict objects (children + parent).
        If model call fails, returns template-based verdicts with confidence 0.0.
        """
        from nthlayer_common.verdicts import create as verdict_create

        try:
            response = await self._call_model(prompt)
            assessments = self._parse_response(response, groups)
        except Exception as e:
            logger.warning("model_call_failed", error=str(e))
            return self._create_template_verdicts(groups)

        # Create per-correlation verdicts
        child_verdicts = []
        for assessment in assessments:
            v = verdict_create(
                subject={
                    "type": "correlation",
                    "service": assessment.get("service"),
                    "ref": assessment.get("group_id", ""),
                    "summary": assessment.get("summary", ""),
                },
                judgment={
                    "action": assessment.get("action", "flag"),
                    "confidence": assessment.get("confidence", 0.5),
                    "reasoning": assessment.get("reasoning", ""),
                    "tags": assessment.get("tags", []),
                },
                producer={"system": "nthlayer-correlate", "model": self._model},
            )
            child_verdicts.append(v)
            if verdict_store:
                verdict_store.put(v)

        # Create parent snapshot verdict
        overall_action = "escalate" if any(
            a.get("action") == "escalate" for a in assessments
        ) else "flag"
        overall_confidence = (
            sum(a.get("confidence", 0.5) for a in assessments) / len(assessments)
            if assessments else 0.0
        )

        # Collect state transition tags
        tags = []
        for a in assessments:
            tags.extend(a.get("tags", []))

        parent = verdict_create(
            subject={
                "type": "correlation",
                "service": None,
                "ref": "snapshot",
                "summary": f"Snapshot: {len(groups)} group(s), {len(assessments)} assessed",
            },
            judgment={
                "action": overall_action,
                "confidence": overall_confidence,
                "reasoning": f"Assessed {len(assessments)} correlation group(s)",
                "tags": list(set(tags)),
            },
            producer={"system": "nthlayer-correlate", "model": self._model},
        )
        parent.lineage.children = [v.id for v in child_verdicts]
        if verdict_store:
            verdict_store.put(parent)

        return child_verdicts + [parent]

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

        # Validate actions and clamp confidence before verdict creation
        valid_actions = {"flag", "escalate", "defer"}
        for a in assessments:
            if a.get("action") not in valid_actions:
                a["action"] = "flag"
            a["confidence"] = clamp(float(a.get("confidence", 0.5)))

        return assessments

    def _create_template_verdicts(self, groups: list[CorrelationGroup]) -> list:
        """Degraded mode: template-based verdicts with confidence 0.0."""
        from nthlayer_common.verdicts import create as verdict_create

        child_verdicts = []
        for group in groups:
            v = verdict_create(
                subject={
                    "type": "correlation",
                    "service": group.services[0] if group.services else None,
                    "ref": group.id,
                    "summary": group.summary,
                },
                judgment={
                    "action": "flag",
                    "confidence": 0.0,
                    "reasoning": "template-based, model unavailable",
                    "tags": ["degraded"],
                },
                producer={"system": "nthlayer-correlate"},
            )
            child_verdicts.append(v)

        parent = verdict_create(
            subject={
                "type": "correlation",
                "service": None,
                "ref": "snapshot-degraded",
                "summary": f"Degraded snapshot: {len(groups)} group(s), no model assessment",
            },
            judgment={
                "action": "flag",
                "confidence": 0.0,
                "reasoning": "template-based, model unavailable",
                "tags": ["degraded"],
            },
            producer={"system": "nthlayer-correlate"},
        )
        parent.lineage.children = [v.id for v in child_verdicts]

        return child_verdicts + [parent]
