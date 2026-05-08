# src/nthlayer_respond/agents/investigation.py
"""InvestigationAgent — hypothesis generation and root cause declaration."""
from __future__ import annotations

import json
from pathlib import Path

from nthlayer_common.prompts import extract_confidence, load_prompt, render_user_prompt
from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.agents.response_models import InvestigationResponse
from nthlayer_workers.respond.types import (
    AgentRole,
    Hypothesis,
    IncidentContext,
    InvestigationResult,
)

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "investigation.yaml"


class InvestigationAgent(AgentBase):
    """Form hypotheses about root cause and rank by confidence.

    Judgment SLO: 70% post-incident agreement on declared root causes.
    Root cause is only declared when confidence exceeds root_cause_threshold.
    """

    role = AgentRole.INVESTIGATION
    default_timeout = 60
    response_model = InvestigationResponse

    # ------------------------------------------------------------------ #
    # Judgment interface                                                   #
    # ------------------------------------------------------------------ #

    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        threshold = self._config.get("root_cause_threshold", 0.7)

        spec = load_prompt(_PROMPT_PATH)
        system = render_user_prompt(spec.system, root_cause_threshold=str(threshold))

        parts: list[str] = []

        # Triage context
        if context.triage is not None:
            t = context.triage
            parts.append("Triage results:")
            parts.append(f"  severity={t.severity}")
            parts.append(f"  blast_radius={t.blast_radius}")
            parts.append(f"  affected_slos={t.affected_slos}")

        # nthlayer-correlate correlation verdicts
        if context.trigger_verdict_ids:
            parts.append("\nnthlayer-correlate correlation verdicts:")
            for vid in context.trigger_verdict_ids:
                try:
                    v = self._verdict_store.get(vid)
                    if v is not None:
                        parts.append(
                            f"  - ref={v.subject.ref!r}  "
                            f"summary={v.subject.summary!r}  "
                            f"confidence={v.judgment.confidence}  "
                            f"reasoning={v.judgment.reasoning!r}"
                        )
                except Exception:  # noqa: BLE001
                    pass

        # Service context from OpenSRM spec + evaluation verdict
        svc_ctx = self._build_service_context_prompt(context)
        if svc_ctx:
            parts.append(svc_ctx)

        # Topology
        # Prune topology to blast radius + 1 hop
        relevant = context.triage.blast_radius if context.triage else []
        pruned = self._prune_topology(context.topology, relevant) if relevant else context.topology
        parts.append(f"\nTopology: {json.dumps(pruned)}")

        user = render_user_prompt(spec.user_template, context="\n".join(parts))
        return system, user

    def parse_response(
        self, response: str, context: IncidentContext
    ) -> InvestigationResult:
        data = self._parse_json(response)
        threshold = self._config.get("root_cause_threshold", 0.7)

        # Build hypotheses list — accept both "description" and "hypothesis" field names
        hypotheses: list[Hypothesis] = []
        for h in data.get("hypotheses") or []:
            desc = h.get("description") or h.get("hypothesis") or h.get("summary", "")
            raw_evidence = h.get("evidence") or []
            if not raw_evidence and h.get("reasoning"):
                raw_evidence = [h["reasoning"]]
            hypotheses.append(
                Hypothesis(
                    description=desc,
                    confidence=float(h.get("confidence", 0.0)),
                    evidence=list(raw_evidence),
                    change_candidate=h.get("change_candidate"),
                )
            )

        root_cause: str | None = data.get("root_cause")
        root_cause_confidence: float = float(data.get("root_cause_confidence") or data.get("confidence") or 0.0)
        reasoning: str = data.get("reasoning", "") or data.get("analysis", "")

        # Mechanical threshold check: clear root_cause if confidence is below threshold
        if root_cause is not None and root_cause_confidence < threshold:
            root_cause = None

        confidence = extract_confidence(data)

        return InvestigationResult(
            hypotheses=hypotheses,
            root_cause=root_cause,
            root_cause_confidence=root_cause_confidence,
            reasoning=reasoning,
            confidence=confidence,
        )

    def _apply_result(
        self, context: IncidentContext, result: InvestigationResult
    ) -> IncidentContext:
        context.investigation = result
        return context
