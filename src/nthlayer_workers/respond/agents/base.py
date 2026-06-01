# src/nthlayer_respond/agents/base.py
"""AgentBase ABC — ZFC boundary: transport here, judgment in subclasses."""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

import structlog
from nthlayer_common.llm_structured import structured_call_with_usage
from nthlayer_common.telemetry import emit_llm_event
from nthlayer_common.verdicts import Verdict
from nthlayer_common.verdicts import create as verdict_create

from nthlayer_workers.respond.types import AgentRole, IncidentContext

log = structlog.get_logger(__name__)


class AgentBase(ABC):
    """Abstract base class for all Mayday agents.

    Transport concerns (model calls, verdict emission, HTTP governance
    requests) live here.  Judgment concerns (prompt construction, response
    parsing, result application) are abstract and implemented by subclasses.
    """

    # Subclasses MUST declare these class-level attributes.
    role: AgentRole
    default_timeout: int = 30
    # Pydantic response model for the structured call (P3-E.2). When set,
    # ``execute`` routes through ``_call_model_structured`` (Instructor +
    # validation retry + OTel cost event) rather than the raw text path.
    response_model: type | None = None

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        model: str,
        max_tokens: int,
        verdict_store: Any | None = None,
        config: dict | None = None,
        timeout: int | None = None,
        decision_store: Any | None = None,
        *,
        client: Any | None = None,         # P3-E.1 worker mode: CoreAPIClient
        deployment_id: str | None = None,  # P3-E.1 worker mode
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._verdict_store = verdict_store
        self._config = config if config is not None else {}
        self._timeout = timeout if timeout is not None else self.default_timeout
        self._decision_store = decision_store
        self._client = client
        self._deployment_id = deployment_id
        if self._client is None and self._verdict_store is None:
            raise ValueError(
                f"{type(self).__name__}: must configure exactly one of "
                "client (worker mode) or verdict_store (legacy CLI mode)"
            )

    # ------------------------------------------------------------------ #
    # Transport: model                                                     #
    # ------------------------------------------------------------------ #

    async def _call_model(self, system_prompt: str, user_prompt: str) -> str:
        """Call the LLM via the shared nthlayer-common wrapper.

        Uses asyncio.to_thread for the sync httpx call, wrapped in
        asyncio.wait_for for timeout enforcement.
        """
        from nthlayer_common.llm import llm_call

        result = await asyncio.wait_for(
            asyncio.to_thread(
                llm_call,
                system=system_prompt,
                user=user_prompt,
                model=self._model,
                max_tokens=self._max_tokens,
                timeout=self._timeout,
            ),
            timeout=self._timeout,
        )
        return result.text

    async def _call_model_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type,
        max_retries: int = 3,
    ) -> str:
        """Call the LLM via Instructor (P3-E.2) and return validated JSON.

        Uses ``structured_call_with_usage`` so we can emit an OTel
        ``nthlayer.llm.call`` event with token counts (cost
        accounting). Instructor's ``max_retries`` retries the LLM on
        validation failure — when the model returns malformed JSON or a
        type-mismatched payload, the wrapper resubmits before raising.

        Returns the validated model serialised to JSON, so subclasses'
        existing ``parse_response`` (which accepts a JSON string and
        produces a dataclass result) can be reused without rewrite.
        """
        provider = self._model.split("/", 1)[0] if "/" in self._model else "unknown"
        caller = f"respond.{self.role.value}"
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    structured_call_with_usage,
                    system=system_prompt,
                    user=user_prompt,
                    response_model=response_model,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    timeout=self._timeout,
                    max_retries=max_retries,
                ),
                timeout=self._timeout,
            )
        except Exception as exc:
            # Cost-accounting parity with the success path: failed
            # structured calls (network error, exhausted Instructor
            # retries, timeout) still emit an OTel event so the
            # operator-facing telemetry shows the attempt.
            emit_llm_event(
                model=self._model,
                provider=provider,
                caller=caller,
                success=False,
                error=type(exc).__name__,
            )
            raise
        emit_llm_event(
            model=self._model,
            provider=provider,
            caller=caller,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            success=True,
        )
        return result.data.model_dump_json()

    # ------------------------------------------------------------------ #
    # Transport: verdict emission                                          #
    # ------------------------------------------------------------------ #

    async def _emit_verdict(
        self,
        context: IncidentContext,
        subject_summary: str,
        action: str,
        confidence: float,
        reasoning: str,
        tags: list[str] | None = None,
        dimensions: dict | None = None,
        metadata: dict | None = None,
    ) -> Verdict:
        """Create, wire lineage, persist, and register a verdict.

        P3-E.1: async. Persists via either a CoreAPIClient (worker mode) or
        a local SQLiteVerdictStore (legacy CLI mode). Exactly one is configured
        at construction time. Lineage and parent_ids are wired before persist;
        escalation flag is captured at write-time on the IncidentContext.

        ``metadata`` is forwarded to ``verdict_create`` unchanged. Subclasses
        attach role-specific structured fields (e.g. RemediationAgent populates
        ``metadata.custom.proposed_action`` / ``target`` so downstream
        consumers like bench's brief can read structured remediation data
        without parsing free-form summary strings).
        """
        judgment: dict[str, Any] = {
            "action": action,
            "confidence": confidence,
            "reasoning": reasoning,
        }
        if tags is not None:
            judgment["tags"] = tags
        if dimensions is not None:
            judgment["dimensions"] = dimensions

        v = verdict_create(
            subject={
                "type": self.role.value,
                "ref": context.id,
                "summary": subject_summary,
            },
            judgment=judgment,
            producer={"system": "nthlayer-respond", "model": self._model},
            metadata=metadata,
        )
        # Typed column for filtering (opensrm-saun.1.2). Matches subject.type
        # for respond agents — both are the role label, but verdict_type is
        # the queryable typed column whereas subject.type is a display field.
        v.verdict_type = self.role.value

        # Wire lineage (existing) + parent_ids (P3-E.1: cross-module ancestry)
        v.lineage.context = list(context.trigger_verdict_ids)
        if context.verdict_chain:
            v.lineage.parent = context.verdict_chain[-1]
            v.parent_ids = [context.verdict_chain[-1]]
        else:
            v.lineage.parent = None
            v.parent_ids = list(context.trigger_verdict_ids)

        # P3-E.1: capture-at-write-time escalation flag. Set on the in-memory
        # context regardless of whether the verdict reaches core — the agent's
        # judgment to escalate is local state and must drive the gate even if
        # the persistence layer is degraded. Operators can correlate via the
        # respond_verdict_submit_failed log + errors_total metric.
        if not isinstance(self._config, dict):
            raise RuntimeError(
                f"{type(self).__name__}: agent config must be a dict, got {type(self._config).__name__}"
            )
        threshold = self._config.get("escalation_threshold", 0.0)
        if (
            v.judgment.action == "escalate"
            and v.judgment.confidence < threshold
        ):
            context.metadata["escalation_pending"] = True

        # Persist via configured backend (exactly one is set; checked at __init__).
        # Worker mode: only append to verdict_chain on successful submission so
        # the in-memory chain stays consistent with core's lineage graph. A
        # failed submission must not produce a dangling parent_ids reference
        # for the next verdict; the next verdict will instead chain to the
        # previous successfully-submitted verdict (or to the trigger if none).
        if self._client is not None:
            from nthlayer_workers.respond.verdict_submission import submit_verdict_to_core
            ok = await submit_verdict_to_core(self._client, v, deployment_id=self._deployment_id)
            if not ok:
                return v  # not appended; next verdict chains to last successful predecessor
        else:
            self._verdict_store.put(v)
        context.verdict_chain.append(v.id)

        return v

    def _write_decision_verdict(
        self,
        context: IncidentContext,
        verdict: Verdict,
        system_prompt: str,
        user_prompt: str,
        response_text: str,
    ) -> None:
        """Write a content-addressed Verdict record if decision_store is configured.

        The ``verdict`` parameter is an ``nthlayer_learn.models.Verdict`` (dataclass).
        Uses the shared ``write_decision_verdict`` helper for chain management and retry.
        """
        if self._decision_store is None:
            return
        from nthlayer_common.records.verdict_bridge import write_decision_verdict

        write_decision_verdict(
            self._decision_store,
            agent=self.role.value,
            incident_id=context.id,
            timestamp=verdict.timestamp,
            model=self._model,
            reasoning=getattr(verdict.judgment, "reasoning", "") or "",
            action={"action": getattr(verdict.judgment, "action", ""), "subject_type": getattr(verdict.subject, "type", "")},
            prompt_text=system_prompt + "\n" + user_prompt,
            response_text=response_text,
            summaries_technical=f"{self.role.value}: {(getattr(verdict.subject, 'summary', '') or '')[:250]}",
            summaries_plain=(getattr(verdict.subject, "summary", "") or "")[:280],
            summaries_executive=f"{self.role.value} verdict emitted",
        )

    def _build_service_context_prompt(self, context: IncidentContext) -> str:
        """Build a service context section for agent prompts from OpenSRM spec
        and evaluation verdict data. This gives agents the domain context they
        need to reason correctly about AI vs infrastructure failures."""
        meta = getattr(context, "metadata", {}) or {}
        svc_ctx = meta.get("service_context", {})
        if not svc_ctx:
            return ""

        lines = ["\nService context:"]
        service = svc_ctx.get("service", "unknown")
        svc_type = svc_ctx.get("service_type", "unknown")
        is_ai = svc_ctx.get("is_ai_gate", False)
        spec = svc_ctx.get("spec", {})
        ev = svc_ctx.get("evaluation", {})

        type_desc = "AI decision service" if is_ai else "traditional service"
        lines.append(f"- Service: {service}")
        lines.append(f"- Type: {svc_type} ({type_desc})")

        if spec.get("tier"):
            lines.append(f"- Tier: {spec['tier']}")
        if spec.get("team"):
            lines.append(f"- Team: {spec['team']}")

        # SLO breach details from evaluation verdict
        if ev.get("slo_name"):
            slo_name = ev["slo_name"]
            slo_type = ev.get("slo_type", "unknown")
            target = ev.get("target")
            current = ev.get("current_value")
            slo_desc = "measures decision quality, not infrastructure health" if slo_type == "judgment" else "measures infrastructure reliability"
            lines.append(f"- Breached SLO: {slo_name} ({slo_type.upper()} SLO — {slo_desc})")
            if current is not None and target is not None:
                lines.append(f"- Current value: {current} (target: < {target})")

        # SLO definitions from spec
        slos = spec.get("slos", {})
        if slos:
            lines.append(f"- Declared SLOs: {', '.join(slos.keys())}")

        # Remediation guidance based on service type
        if is_ai:
            lines.append("- This is NOT an infrastructure issue. This is an AI model quality issue.")
            lines.append("- Appropriate remediation: model rollback, canary revert, autonomy reduction")
            lines.append("- Inappropriate remediation: scale_up, restart, increase resources")
        else:
            lines.append("- This is an infrastructure/availability issue.")
            lines.append("- Appropriate remediation: rollback, scale_up, restart, feature flag disable")

        return "\n".join(lines)

    def _prune_topology(self, topology: dict, relevant_services: list[str]) -> dict:
        """Prune topology to relevant services + 1 hop of dependencies.

        Reduces prompt token cost by excluding services not in the blast radius
        or its immediate neighbourhood.
        """
        if not topology or not relevant_services:
            return topology

        services = topology.get("services", [])
        if not services:
            return topology

        relevant = set(relevant_services)

        # Add 1 hop: dependencies and dependents of relevant services
        for svc in services:
            name = svc.get("name", "")
            if name in relevant:
                for dep in svc.get("dependencies", []):
                    dep_name = dep.get("name", dep) if isinstance(dep, dict) else dep
                    relevant.add(dep_name)

        pruned = [s for s in services if s.get("name", "") in relevant]
        return {**topology, "services": pruned}

    def _build_summary(self, context: IncidentContext, result) -> str:
        """Build summary strictly from the agent's actual LLM output.

        Every field must be traceable to the agent's response.
        No template-generated content that impersonates agent reasoning.
        """
        role = self.role.value
        reasoning = getattr(result, "reasoning", None) or ""

        if role == "triage":
            sev = getattr(result, "severity", None)
            blast = getattr(result, "blast_radius", None) or []
            team = getattr(result, "assigned_team", None)
            first_sentence = reasoning.split(".")[0].strip() if reasoning else ""
            if first_sentence:
                return f"SEV-{sev}: {first_sentence}"
            if sev is not None:
                parts = [f"SEV-{sev}"]
                if blast:
                    parts.append(f"{len(blast)} services in blast radius")
                if team:
                    parts.append(f"assigned to {team}")
                return " — ".join(parts)

        elif role == "investigation":
            rc = getattr(result, "root_cause", None)
            rc_conf = getattr(result, "root_cause_confidence", 0)
            if rc:
                return f"Root cause ({rc_conf:.0%} confidence): {rc[:90]}"
            hypotheses = getattr(result, "hypotheses", None) or []
            if hypotheses:
                h = hypotheses[0]
                desc = getattr(h, "description", str(h)) if hasattr(h, "description") else str(h)
                return f"Hypothesis: {desc[:90]}"

        elif role == "communication":
            updates = getattr(result, "updates_sent", None) or []
            if updates:
                u = updates[0]
                content = getattr(u, "content", "") if hasattr(u, "content") else str(u)
                channel = getattr(u, "channel", "") if hasattr(u, "channel") else ""
                return f"{'via ' + channel + ': ' if channel else ''}{content[:90]}"

        elif role == "remediation":
            action = getattr(result, "proposed_action", None)
            target = getattr(result, "target", None)
            if action and target:
                approval = getattr(result, "requires_human_approval", True)
                return f"{action} on {target}" + (" (requires approval)" if approval else "")
            if action:
                return f"Proposed: {action}"

        # If we got here, the agent-specific fields were empty.
        # Use first sentence of reasoning as last resort from actual output.
        if reasoning:
            return reasoning.split(".")[0].strip()[:90]

        # Genuinely empty — mark as unparseable
        log.warning("agent_summary_empty", role=role, incident=context.id)
        return "Agent response produced no summary — see raw output"

    async def _degraded_verdict(self, context: IncidentContext, reason: str) -> Verdict:
        """Emit a degraded escalation verdict when the agent cannot produce a
        normal judgment (e.g. model timeout, API error)."""
        summary = self._build_degraded_summary(context)
        return await self._emit_verdict(
            context,
            subject_summary=summary,
            action="escalate",
            confidence=0.0,
            reasoning=f"Agent operating in degraded mode: {reason}",
            tags=["degraded", "human-takeover-required"],
            metadata=self._build_degraded_metadata(),
        )

    def _build_metadata(self, result: Any) -> dict | None:
        """Hook: subclass returns structured metadata for the normal-path verdict.

        Default returns None (no metadata attached). RemediationAgent overrides
        to attach proposed_action/target so downstream consumers (bench brief,
        post-incident review) can read structured fields without parsing
        free-form summary strings. Subclasses narrow ``result`` to their own
        result type (e.g. ``RemediationResult``).
        """
        return None

    def _build_degraded_metadata(self) -> dict | None:
        """Hook: subclass returns structured metadata for the degraded-path verdict.

        RemediationAgent overrides to attach proposed_action=None / target=None
        so the field is explicitly absent — distinguishable by downstream
        consumers from a non-remediation verdict (where the metadata key
        is simply missing).
        """
        return None

    def _build_degraded_summary(self, context: IncidentContext) -> str:
        """Build an informative degraded summary using available context data."""
        role = self.role.value
        meta = getattr(context, "metadata", {}) or {}
        blast = meta.get("blast_radius", [])
        root_causes = meta.get("root_causes", [])
        severity = meta.get("severity", "?")
        incident_id = getattr(context, "id", "unknown")
        rc_service = root_causes[0].get("service", "unknown") if root_causes else "unknown"
        rc_type = root_causes[0].get("type", "unknown") if root_causes else "unknown"
        # Also try SLO info from the trigger verdicts
        slo_info = ""
        if rc_service != "unknown" and rc_type != "unknown":
            slo_info = f"{rc_service} {rc_type}"

        if role == "triage":
            slo_info = f"{rc_service} {rc_type}" if rc_service != "unknown" else "incident"
            return f"DEGRADED: SEV-{severity} — {slo_info}, {len(blast)} services in blast radius"
        elif role == "investigation":
            return f"DEGRADED: Manual investigation required — root cause from correlation: {rc_service} ({rc_type})"
        elif role == "communication":
            return f"DEGRADED: Draft status update required for {incident_id}"
        elif role == "remediation":
            return "DEGRADED: Manual remediation required — see correlation verdict for recommended actions"
        return f"DEGRADED: {role} — manual assessment required"

    # ------------------------------------------------------------------ #
    # Transport: governance                                                #
    # ------------------------------------------------------------------ #

    async def _request_autonomy_reduction(
        self,
        agent_name: str,
        arbiter_url: str,
        reason: str,
    ) -> dict:
        """POST to Arbiter's governance endpoint to reduce agent autonomy.

        Uses urllib (stdlib) in asyncio.to_thread; retries up to 3 times.
        """
        url = f"{arbiter_url}/api/v1/governance/reduce"
        payload = json.dumps({
            "agent": agent_name,
            "reason": reason,
        }).encode()

        def _sync_post() -> dict:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return json.loads(resp.read().decode())
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            raise RuntimeError(
                f"Autonomy reduction request failed after 3 attempts: {last_exc}"
            )

        return await asyncio.to_thread(_sync_post)

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    def _parse_json(self, response: str) -> dict:
        """Extract and parse JSON from a model response.

        Handles:
        - Clean JSON strings
        - Markdown fenced blocks (```json ... ```)
        - Preamble text before the first ``{``
        """
        text = response.strip()

        # Strip markdown fences
        fenced = re.sub(r"^```(?:json)?\s*", "", text)
        fenced = re.sub(r"\s*```$", "", fenced).strip()

        # Find a valid JSON object by matching braces
        brace_index = fenced.find("{")
        if brace_index == -1:
            raise ValueError(f"No JSON object found in response: {response!r}")

        # Try progressively from each '{' to find valid JSON
        for start in range(brace_index, len(fenced)):
            if fenced[start] != "{":
                continue
            depth = 0
            for end in range(start, len(fenced)):
                if fenced[end] == "{":
                    depth += 1
                elif fenced[end] == "}":
                    depth -= 1
                if depth == 0:
                    try:
                        return json.loads(fenced[start:end + 1])
                    except json.JSONDecodeError:
                        break  # try next '{'
                    break

        raise ValueError(f"Failed to parse JSON from response: {response!r}")

    # ------------------------------------------------------------------ #
    # Template method                                                      #
    # ------------------------------------------------------------------ #

    async def execute(self, context: IncidentContext) -> IncidentContext:
        """Run the agent against the given incident context.

        Template method — orchestrates the call sequence; subclasses
        provide judgment via build_prompt / parse_response / _apply_result.
        """
        try:
            system, user = self.build_prompt(context)
            if self.response_model is not None:
                response = await self._call_model_structured(
                    system, user, self.response_model
                )
            else:
                response = await self._call_model(system, user)
            body_preview = response if len(response) <= 800 else response[:800].rsplit(" ", 1)[0] + "..."
            log.info("agent_response", role=self.role.value,
                     response_length=len(response), body=body_preview)
            result = self.parse_response(response, context)
            context = self._apply_result(context, result)
            confidence = getattr(result, "root_cause_confidence", None) or getattr(result, "confidence", None) or 0.0
            summary = self._build_summary(context, result)
            verdict = await self._emit_verdict(
                context,
                subject_summary=summary,
                action="flag",
                confidence=confidence,
                reasoning=getattr(result, "reasoning", ""),
                metadata=self._build_metadata(result),
            )
            self._write_decision_verdict(context, verdict, system, user, response)
            # Slack notification (fail-open, opt-in via SLACK_WEBHOOK_URL)
            await self._notify_slack(context)
            context = await self._post_execute(context, result)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_execute_failed", role=self.role.value,
                        error=str(exc), exc_info=True)
            await self._degraded_verdict(context, str(exc))
        return context

    async def _post_execute(
        self, context: IncidentContext, result: Any
    ) -> IncidentContext:
        """Hook called after a successful execute cycle. No-op by default."""
        return context

    async def _notify_slack(self, context: IncidentContext) -> None:
        """Send Slack notification for this agent's verdict. Fail-open."""
        import os
        if not os.environ.get("SLACK_WEBHOOK_URL"):
            return
        try:
            from nthlayer_workers.respond.notifications import (
                build_remediation_blocks,
                build_triage_blocks,
                send_slack_notification,
            )

            role = self.role.value
            builders = {
                "triage": build_triage_blocks,
                "remediation": build_remediation_blocks,
            }
            builder = builders.get(role)
            if not builder or not context.verdict_chain:
                return

            # Get the most recent verdict (just emitted)
            last_vid = context.verdict_chain[-1]
            v = self._verdict_store.get(last_vid) if self._verdict_store else None
            if not v:
                return

            await send_slack_notification(
                v, builder,
                verdict_store=self._verdict_store,
                trigger_verdict_ids=context.trigger_verdict_ids,
            )
        except Exception:
            log.warning("Slack notification failed", role=self.role.value, exc_info=True)

    # ------------------------------------------------------------------ #
    # Abstract judgment interface                                          #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for this agent's judgment."""

    @abstractmethod
    def parse_response(self, response: str, context: IncidentContext) -> Any:
        """Parse the model's text response into a typed result object."""

    @abstractmethod
    def _apply_result(
        self, context: IncidentContext, result: Any
    ) -> IncidentContext:
        """Write the parsed result into the shared incident context."""
