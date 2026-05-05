# src/nthlayer_respond/agents/remediation.py
"""RemediationAgent — safe action execution with approval ratchet."""
from __future__ import annotations

import json
import structlog
from pathlib import Path

from nthlayer_common.prompts import extract_confidence, load_prompt, render_user_prompt
from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.safe_actions.registry import SafeActionRegistry
from nthlayer_workers.respond.types import (
    AgentRole,
    IncidentContext,
    RemediationResult,
)

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "remediation.yaml"

logger = structlog.get_logger(__name__)


def _format_safe_actions(policy: dict) -> str:
    """Format safe action policy as context for the remediation prompt."""
    lines = ["Available safe actions (you MUST choose from this list):"]
    for name, spec in policy.items():
        desc = spec.get("description", "").strip()
        risk = spec.get("risk", "unknown")
        approval = "requires approval" if spec.get("requires_approval") else "auto-approved"
        applicable = spec.get("applicable_to", {})
        modes = applicable.get("failure_modes", [])
        lines.append(f"  - {name} ({risk} risk, {approval}): {desc}")
        if modes:
            lines.append(f"    Applicable to: {', '.join(modes)}")
        not_applicable = spec.get("not_applicable_to")
        if not_applicable:
            reason = not_applicable.get("reason", "").strip()
            lines.append(f"    NOT applicable to {not_applicable.get('service_types', [])}: {reason}")
    return "\n".join(lines)


class RemediationAgent(AgentBase):
    """Suggest and execute pre-approved safe actions.

    Judgment SLO: 80% fix success rate.

    Safety properties:
    - Only actions in the closed SafeActionRegistry may be proposed.
    - Approval ratchet: registry.requires_approval=True can never be downgraded by
      the model.  The model may always escalate (say True when registry says False).
    - Novel actions (not in registry) are rejected: proposed_action set to None and
      requires_human_approval forced to True.
    """

    role = AgentRole.REMEDIATION
    default_timeout = 30

    def __init__(
        self,
        *args,
        safe_action_registry: SafeActionRegistry | None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Worker mode currently passes None pending opensrm P3-E.3 (registry
        # wiring). When the registry is absent we cannot verify whether the
        # model's proposed action is hallucinated or whether the policy
        # requires approval — so parse_response forces approval to be
        # required, and _post_execute skips automatic execution.
        self._registry = safe_action_registry

    # ------------------------------------------------------------------ #
    # Judgment interface                                                   #
    # ------------------------------------------------------------------ #

    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        from nthlayer_workers.respond.safe_actions.actions import load_safe_action_policy

        policy = load_safe_action_policy()
        safe_actions_text = _format_safe_actions(policy)

        spec = load_prompt(_PROMPT_PATH)
        system = render_user_prompt(spec.system, safe_actions=safe_actions_text)

        parts: list[str] = []

        # Investigation context
        if context.investigation is not None:
            inv = context.investigation
            parts.append("Investigation findings:")
            if inv.root_cause is not None:
                parts.append(f"  root_cause={inv.root_cause!r}")
                parts.append(f"  root_cause_confidence={inv.root_cause_confidence}")
            if inv.hypotheses:
                parts.append("  hypotheses:")
                for h in inv.hypotheses:
                    parts.append(
                        f"    - {h.description!r} (confidence={h.confidence}, "
                        f"change_candidate={h.change_candidate!r})"
                    )

        # Triage context
        if context.triage is not None:
            t = context.triage
            parts.append("Triage results:")
            parts.append(f"  severity={t.severity}")
            parts.append(f"  blast_radius={t.blast_radius}")
            parts.append(f"  affected_slos={t.affected_slos}")

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
    ) -> RemediationResult:
        data = self._parse_json(response)

        proposed_action: str | None = data.get("proposed_action") or data.get("recommended_action") or data.get("action")
        target: str | None = data.get("target") or data.get("target_service")
        risk_assessment: str = data.get("risk_assessment", "") or data.get("risk", "")
        requires_human_approval: bool = bool(data.get("requires_human_approval", True))
        autonomy_reduction: dict = data.get("autonomy_reduction") or {}
        reasoning: str = data.get("reasoning", "") or data.get("rationale", "")

        # Critical validation: reject hallucinated actions
        if proposed_action is not None:
            if self._registry is None:
                # Worker mode without a wired registry (P3-E.3 pending) —
                # we can't verify the action against the safe-action policy,
                # so force human approval as the safest default. The
                # proposal is preserved for the operator to review.
                logger.warning(
                    "RemediationAgent: no safe-action registry configured — "
                    "cannot verify proposed action %r; forcing requires_human_approval=True.",
                    proposed_action,
                )
                requires_human_approval = True
                registry_action = None
            else:
                try:
                    registry_action = self._registry.get(proposed_action)
                except KeyError:
                    logger.warning(
                        "RemediationAgent: model proposed unknown action %r — rejecting "
                        "(hallucinated action name). Forcing requires_human_approval=True.",
                        proposed_action,
                    )
                    proposed_action = None
                    requires_human_approval = True
                    registry_action = None
                else:
                    # Approval ratchet: registry can only escalate, never downgrade.
                    # If registry says approval required, model cannot override it to False.
                    if registry_action.requires_approval and not requires_human_approval:
                        requires_human_approval = True
        else:
            registry_action = None

        confidence = extract_confidence(data)

        result = RemediationResult(
            proposed_action=proposed_action,
            target=target,
            risk_assessment=risk_assessment,
            requires_human_approval=requires_human_approval,
            reasoning=reasoning,
            confidence=confidence,
        )

        # Stash autonomy_reduction on the result for use in _post_execute
        result.autonomy_reduction = autonomy_reduction

        return result

    def _apply_result(
        self, context: IncidentContext, result: RemediationResult
    ) -> IncidentContext:
        context.remediation = result
        return context

    def _build_metadata(self, result: RemediationResult) -> dict:
        # Structured fields for downstream consumers (bench brief,
        # post-incident review, P3-E.3 safe-action execution, audit tooling).
        # Both keys always present — None values disambiguate "rejected
        # hallucinated action" / "model omitted target" from "non-remediation
        # verdict" at the consumer site.
        return {
            "custom": {
                "proposed_action": result.proposed_action,
                "target": result.target,
            }
        }

    def _build_degraded_metadata(self) -> dict:
        # Degraded path: model never returned a result. Both fields
        # explicitly None so brief can render "manual intervention required"
        # without inferring degradation from absence.
        return {"custom": {"proposed_action": None, "target": None}}

    # ------------------------------------------------------------------ #
    # Post-execute: safe action + autonomy reduction                      #
    # ------------------------------------------------------------------ #

    async def _post_execute(
        self, context: IncidentContext, result: RemediationResult
    ) -> IncidentContext:
        """Execute safe action (if approved) then handle autonomy reduction.

        Strict ordering:
        1. Execute safe action (if not requires_human_approval and action is set)
        2. Autonomy reduction (if recommended)
        """
        # Step 1: Execute safe action. When registry is None (worker mode
        # pending P3-E.3) parse_response has already forced
        # requires_human_approval=True, so this branch is unreachable; the
        # `is not None` check is a defensive belt-and-braces guard.
        if (
            not result.requires_human_approval
            and result.proposed_action is not None
            and self._registry is not None
        ):
            try:
                exec_result = await self._registry.execute(
                    result.proposed_action, result.target, context
                )
                result.executed = True
                result.execution_result = exec_result.get("detail")
            except Exception as exc:  # noqa: BLE001
                result.executed = False
                result.execution_result = str(exc)

        # Step 2: Autonomy reduction (if recommended by model)
        autonomy_reduction: dict = result.autonomy_reduction or {}
        if autonomy_reduction.get("recommended"):
            target_agent: str = autonomy_reduction.get("target_agent", "")
            arbiter_url: str = self._config.get("arbiter_url", "")
            reason: str = autonomy_reduction.get("reason", "")

            try:
                gov_response = await self._request_autonomy_reduction(
                    target_agent, arbiter_url, reason
                )
                result.autonomy_reduced = True
                result.autonomy_target = target_agent
                result.previous_autonomy_level = gov_response.get("previous_level")
                result.new_autonomy_level = gov_response.get("new_level")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "RemediationAgent: autonomy reduction request failed: %s", exc
                )

        return context
