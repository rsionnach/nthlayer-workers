# src/nthlayer_respond/agents/communication.py
"""CommunicationAgent — two-phase incident communication drafting."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nthlayer_common.prompts import extract_confidence, load_prompt
from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.types import (
    AgentRole,
    CommunicationResult,
    CommunicationUpdate,
    IncidentContext,
)

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "communication.yaml"


class CommunicationAgent(AgentBase):
    """Draft incident communications in two pipeline phases.

    Phase 1 (context.remediation is None): draft initial status update.
    Phase 2 (context.remediation is not None): draft resolution update.

    Judgment SLO: human edit rate < 15%.
    """

    role = AgentRole.COMMUNICATION
    default_timeout = 20

    # ------------------------------------------------------------------ #
    # Judgment interface                                                   #
    # ------------------------------------------------------------------ #

    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        spec = load_prompt(_PROMPT_PATH)

        # Service context from OpenSRM spec
        svc_ctx = self._build_service_context_prompt(context)

        if context.remediation is None:
            # Phase 1 — initial status update
            triage = context.triage
            severity = triage.severity if triage is not None else "unknown"
            blast_radius = triage.blast_radius if triage is not None else []
            user = (
                f"Draft an initial status update. "
                f"We know: severity={severity}, "
                f"affected services={blast_radius}. "
                f"Investigation is ongoing."
            )
        else:
            # Phase 2 — resolution update
            inv = context.investigation
            rem = context.remediation
            root_cause = inv.root_cause if inv is not None else "under investigation"
            user = (
                f"Draft a resolution update. "
                f"Root cause: {root_cause}. "
                f"Remediation: {rem.proposed_action} on {rem.target}. "
                f"Outcome: {rem.execution_result}."
            )

        if svc_ctx:
            user = user + "\n" + svc_ctx

        return spec.system, user

    def parse_response(
        self, response: str, context: IncidentContext
    ) -> CommunicationResult:
        data = self._parse_json(response)

        timestamp = datetime.now(tz=timezone.utc).isoformat()

        updates: list[CommunicationUpdate] = []
        for raw in data.get("updates") or data.get("messages") or []:
            updates.append(
                CommunicationUpdate(
                    channel=raw.get("channel", ""),
                    timestamp=timestamp,
                    update_type=raw.get("update_type", raw.get("type", "")),
                    content=raw.get("content", raw.get("message", "")),
                )
            )

        # If no structured updates, synthesize from flat response fields
        if not updates:
            content_parts = []
            for key in ("title", "impact_description", "current_status", "summary", "message"):
                if data.get(key):
                    content_parts.append(str(data[key]))
            if content_parts:
                updates.append(CommunicationUpdate(
                    channel=data.get("channel", "status_page"),
                    timestamp=timestamp,
                    update_type=data.get("status", "initial"),
                    content=" — ".join(content_parts),
                ))

        reasoning: str = data.get("reasoning", "") or data.get("rationale", "")
        confidence = extract_confidence(data)
        return CommunicationResult(updates_sent=updates, reasoning=reasoning, confidence=confidence)

    def _apply_result(
        self, context: IncidentContext, result: CommunicationResult
    ) -> IncidentContext:
        if context.communication is None:
            context.communication = result
        else:
            # APPEND — do not replace; phase 2 adds to phase 1's updates
            context.communication.updates_sent.extend(result.updates_sent)
            context.communication.reasoning = result.reasoning
        return context
