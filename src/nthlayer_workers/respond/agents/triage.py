# src/nthlayer_respond/agents/triage.py
"""TriageAgent — first concrete agent in the Mayday pipeline."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nthlayer_common.prompts import extract_confidence, load_prompt, render_user_prompt

from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.types import AgentRole, IncidentContext, TriageResult

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "triage.yaml"


class TriageAgent(AgentBase):
    """Assess severity, blast radius, affected SLOs, and team assignment.

    Judgment SLO: severity reversal rate < 10%.
    """

    role = AgentRole.TRIAGE
    default_timeout = 15

    # ------------------------------------------------------------------ #
    # Judgment interface                                                   #
    # ------------------------------------------------------------------ #

    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        spec = load_prompt(_PROMPT_PATH)

        parts: list[str] = []

        if context.trigger_source == "nthlayer-correlate":
            parts.append(
                "You have pre-correlated context from nthlayer-correlate. "
                "The following correlation verdicts informed this incident:"
            )
            for vid in context.trigger_verdict_ids:
                try:
                    v = self._verdict_store.get(vid)
                    if v is not None:
                        parts.append(
                            f"  - service={v.subject.service!r}  "
                            f"summary={v.subject.summary!r}  "
                            f"confidence={v.judgment.confidence}  "
                            f"reasoning={v.judgment.reasoning!r}"
                        )
                except Exception:  # noqa: BLE001
                    pass
        else:
            parts.append(
                "No pre-correlation available. Raw alert only. "
                "Assess based solely on the topology information below."
            )

        svc_ctx = self._build_service_context_prompt(context)
        if svc_ctx:
            parts.append(svc_ctx)

        # Prune topology to trigger service + 1 hop (reduces prompt tokens)
        trigger_svc = (context.metadata or {}).get("trigger_service", "")
        pruned = self._prune_topology(context.topology, [trigger_svc]) if trigger_svc else context.topology
        parts.append(f"\nTopology: {json.dumps(pruned)}")

        user = render_user_prompt(spec.user_template, context="\n".join(parts))
        return spec.system, user

    def parse_response(self, response: str, context: IncidentContext) -> TriageResult:
        data = self._parse_json(response)

        raw_severity = data.get("severity", 2)
        severity = max(0, min(4, int(raw_severity)))

        raw_blast = data.get("blast_radius") or []
        blast_radius: list[str] = raw_blast if isinstance(raw_blast, list) else [raw_blast] if raw_blast else []
        affected_slos: list[str] = data.get("affected_slos") or []
        assigned_team: str | None = data.get("assigned_team") or data.get("team_assignment")
        reasoning: str = data.get("reasoning", "") or data.get("rationale", "")
        confidence = extract_confidence(data)

        return TriageResult(
            severity=severity,
            blast_radius=blast_radius,
            affected_slos=affected_slos,
            assigned_team=assigned_team,
            reasoning=reasoning,
            confidence=confidence,
        )

    def _apply_result(
        self, context: IncidentContext, result: TriageResult
    ) -> IncidentContext:
        context.triage = result
        return context

    # ------------------------------------------------------------------ #
    # Post-execute hook: autonomy reduction                               #
    # ------------------------------------------------------------------ #

    async def _post_execute(
        self, context: IncidentContext, result: Any
    ) -> IncidentContext:
        """Trigger autonomy reduction when a model-update signal is present
        and severity is low enough that the update may have distorted judgment.

        Condition: any trigger verdict carries tag "agent_model_update"
        AND result.severity <= 2.
        """
        if not (hasattr(result, "severity") and result.severity <= 2):
            return context

        for vid in context.trigger_verdict_ids:
            try:
                v = self._verdict_store.get(vid)
                tags = (v.judgment.tags or []) if v is not None else []
                if "agent_model_update" in tags:
                    arbiter_url: str = self._config.get("arbiter_url", "")
                    await self._request_autonomy_reduction(
                        agent_name="triage",
                        arbiter_url=arbiter_url,
                        reason=(
                            f"Trigger verdict {vid} flagged agent_model_update; "
                            f"incident {context.id} severity={result.severity}"
                        ),
                    )
                    break
            except Exception:  # noqa: BLE001
                pass

        return context
