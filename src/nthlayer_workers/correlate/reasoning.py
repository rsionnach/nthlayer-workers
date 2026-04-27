"""Reasoning layer for correlation groups.

Used by: the `correlate` CLI subcommand (live Prometheus-triggered correlation).
See also: snapshot/model.py which serves the `serve` and `replay` subcommands.

Sits between CorrelationEngine.correlate() group assembly and verdict creation.
Calls an LLM to assess causal relationships, root causes, and recommended actions.
Provider-agnostic via nthlayer-common wrapper (Anthropic, OpenAI, Ollama, etc.).
Additive: --no-reasoning produces identical output to pre-reasoning behavior.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import structlog

from nthlayer_common.prompts import load_prompt
from nthlayer_workers.correlate.types import CorrelationGroup

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "reasoning.yaml"

logger = structlog.get_logger(__name__)


def reasoning_available() -> bool:
    """Check if an LLM provider is configured and reachable.

    Returns True if NTHLAYER_MODEL is set (any provider, including keyless
    ones like Ollama) or if an API key for a cloud provider is set.
    """
    return bool(
        os.environ.get("NTHLAYER_MODEL")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


async def reason_about_correlations(
    groups: list[CorrelationGroup],
    dependency_graph: dict,
    slo_targets: dict | None = None,
    trace_evidence: object | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    timeout: int = 30,
) -> dict:
    """Call an LLM to reason about pre-correlated groups.

    Returns structured reasoning dict with keys:
      - groups: list of per-group reasoning (root_cause, confidence, reasoning, recommended_actions)
      - overall_assessment: str
      - overall_confidence: float

    Falls back to _degraded_reasoning() on any failure.
    """
    if not groups:
        return _degraded_reasoning(groups, reason="no correlation groups to assess")

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(groups, dependency_graph, slo_targets, trace_evidence=trace_evidence)

    try:
        response_text = await _call_model(
            system_prompt, user_prompt, model, max_tokens, timeout
        )
        result = _parse_reasoning_response(response_text, groups)
        logger.info(
            "reasoning_complete",
            groups=len(groups),
            overall_confidence=result.get("overall_confidence", 0.0),
        )
        return result
    except Exception as exc:
        logger.warning("reasoning_failed", error=str(exc))
        return _degraded_reasoning(groups, reason=str(exc))


def _degraded_reasoning(
    groups: list[CorrelationGroup],
    reason: str = "model unavailable",
) -> dict:
    """Fallback reasoning when API is unavailable.

    Confidence 0.0, tagged as degraded. Transport continues, judgment pauses.
    """
    group_assessments = []
    for g in groups:
        group_assessments.append({
            "group_id": g.id,
            "root_cause": None,
            "confidence": 0.0,
            "reasoning": f"Degraded mode: {reason}",
            "recommended_actions": [],
            "is_causal": None,
            "degraded": True,
        })

    return {
        "groups": group_assessments,
        "overall_assessment": f"Reasoning unavailable: {reason}",
        "overall_confidence": 0.0,
        "degraded": True,
    }


async def _call_model(
    system_prompt: str,
    user_prompt: str,
    model: str | None,
    max_tokens: int,
    timeout: int,
) -> str:
    """Call LLM via the shared nthlayer-common wrapper."""
    from nthlayer_common.llm import llm_call

    result = await asyncio.to_thread(
        llm_call,
        system=system_prompt,
        user=user_prompt,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return result.text


def _build_system_prompt() -> str:
    spec = load_prompt(_PROMPT_PATH)
    return spec.system


def _build_user_prompt(
    groups: list[CorrelationGroup],
    dependency_graph: dict,
    slo_targets: dict | None,
    *,
    trace_evidence: object | None = None,
) -> str:
    sections = []

    # Cap groups at 10 — drop P3 (lowest priority) first to stay within token budget
    MAX_GROUPS = 10
    if len(groups) > MAX_GROUPS:
        groups = sorted(groups, key=lambda g: g.priority)[:MAX_GROUPS]

    # Collect services mentioned in groups for dependency graph pruning
    relevant_services = set()
    for g in groups:
        relevant_services.update(g.services)
        for cc in g.change_candidates:
            relevant_services.add(cc.change.service)

    # Dependency graph — pruned to services in correlation groups + 1 hop
    if dependency_graph:
        # Add 1 hop of deps/dependents
        extended = set(relevant_services)
        for svc in relevant_services:
            info = dependency_graph.get(svc, {})
            extended.update(info.get("dependencies", []))
            extended.update(info.get("dependents", []))

        dep_lines = []
        for svc, info in sorted(dependency_graph.items()):
            if svc not in extended:
                continue
            deps = info.get("dependencies", [])
            dependents = info.get("dependents", [])
            tier = info.get("tier", "standard")
            dep_lines.append(
                f"  {svc} (tier={tier}): depends_on={deps}, depended_by={dependents}"
            )
        if dep_lines:
            sections.append("DEPENDENCY GRAPH:\n" + "\n".join(dep_lines))

    # SLO targets
    if slo_targets:
        slo_lines = []
        for svc, targets in sorted(slo_targets.items()):
            slo_lines.append(f"  {svc}: {targets}")
        sections.append("SLO TARGETS:\n" + "\n".join(slo_lines))

    # Correlation groups
    for g in groups:
        lines = [
            f"GROUP {g.id} (P{g.priority}):",
            f"  Services: {', '.join(g.services)}",
            f"  Event count: {g.event_count}",
            f"  Time range: {g.first_seen} to {g.last_updated}",
            f"  Summary: {g.summary}",
        ]

        # Topology
        if g.topology:
            lines.append(f"  Topology: primary={g.topology.primary_service}, "
                         f"related={g.topology.related_services}, "
                         f"path={g.topology.topology_path}")

        # Signals
        for sig in g.signals:
            lines.append(
                f"  Signal: {sig.service} — {sig.count} event(s), "
                f"peak_severity={sig.peak_severity:.2f}, "
                f"duration={sig.duration_seconds:.0f}s, "
                f"window={sig.time_window[0]} to {sig.time_window[1]}"
            )
            for evt in sig.events[:5]:  # cap per signal to stay within budget
                lines.append(
                    f"    Event: type={evt.type.value}, source={evt.source}, "
                    f"severity={evt.severity:.2f}, payload={_compact_payload(evt.payload)}"
                )

        # Change candidates
        for cc in g.change_candidates:
            lines.append(
                f"  Change candidate: service={cc.change.service}, "
                f"proximity={cc.temporal_proximity_seconds:.0f}s, "
                f"same_service={cc.same_service}, "
                f"dependency_related={cc.dependency_related}, "
                f"payload={_compact_payload(cc.change.payload)}"
            )

        sections.append("\n".join(lines))

    # Trace evidence section (optional)
    trace_section = _build_trace_evidence_section(trace_evidence)
    if trace_section:
        sections.append(trace_section)

    return "\n\n".join(sections)


def _build_trace_evidence_section(trace_evidence: object | None) -> str:
    """Format trace evidence for the reasoning prompt.

    NOTE: v0 renders trace evidence as flat text for the reasoning prompt.
    Future: support multi-register summaries (span-level detail for
    investigation agent, high-level for communication agent) via the
    Summaries(technical, plain, executive) pattern from decision records.
    """
    if trace_evidence is None:
        return ""

    from nthlayer_workers.correlate.traces.protocol import TraceEvidence
    if not isinstance(trace_evidence, TraceEvidence):
        return ""
    if not trace_evidence.services:
        return ""

    sections = ["TRACE EVIDENCE:"]

    for svc in trace_evidence.services:
        lines = [f"  {svc.service}:"]

        # Latency summary
        if svc.latency_change_pct is not None and abs(svc.latency_change_pct) > 10:
            lines.append(
                f"    Latency: p50={svc.p50_latency_ms:.0f}ms, "
                f"p99={svc.p99_latency_ms:.0f}ms "
                f"({svc.latency_change_pct:+.0f}% vs baseline)"
            )
        else:
            lines.append(
                f"    Latency: p50={svc.p50_latency_ms:.0f}ms, "
                f"p99={svc.p99_latency_ms:.0f}ms (within baseline)"
            )

        # Error summary
        if svc.error_rate > 0.01:
            lines.append(
                f"    Errors: {svc.error_rate:.1%} error rate "
                f"({svc.error_count}/{svc.total_request_count} requests)"
            )
            for err in svc.top_errors[:3]:
                lines.append(f"      - \"{err.error_message}\" (x{err.count})")

        # Slow operations
        for op in svc.slow_operations[:3]:
            if op.change_pct is not None and op.change_pct > 20:
                lines.append(
                    f"    Slow operation: {op.operation} "
                    f"p99={op.p99_ms:.0f}ms ({op.change_pct:+.0f}% vs baseline)"
                )

        # Call edges
        if svc.callers:
            caller_str = ", ".join(
                f"{c.source_service} ({c.request_count} reqs, p99={c.p99_latency_ms:.0f}ms)"
                for c in svc.callers[:3]
            )
            lines.append(f"    Called by: {caller_str}")

        if svc.callees:
            callee_str = ", ".join(
                f"{c.target_service} ({c.request_count} reqs, p99={c.p99_latency_ms:.0f}ms, "
                f"err={c.error_count}/{c.request_count})"
                for c in svc.callees[:3]
            )
            lines.append(f"    Calls: {callee_str}")

        sections.append("\n".join(lines))

    # Topology divergence
    if trace_evidence.topology_divergence:
        div = trace_evidence.topology_divergence
        if div.observed_not_declared:
            sections.append(
                "  Undeclared Dependencies (observed in traces, not in specs):\n"
                + "\n".join(f"    - {a} → {b}" for a, b in div.observed_not_declared)
            )
        if div.declared_not_observed:
            sections.append(
                "  Declared but Unobserved Dependencies (in specs, no traces):\n"
                + "\n".join(f"    - {a} → {b}" for a, b in div.declared_not_observed)
            )

    return "\n\n".join(sections)


def _compact_payload(payload: dict) -> str:
    """Compact payload representation to stay within token budget."""
    compact = {}
    for k, v in payload.items():
        if isinstance(v, dict) and len(str(v)) > 100:
            compact[k] = "{...}"
        elif isinstance(v, list) and len(v) > 5:
            compact[k] = f"[{len(v)} items]"
        else:
            compact[k] = v
    return json.dumps(compact, default=str, separators=(",", ":"))


def _parse_reasoning_response(
    response_text: str,
    groups: list[CorrelationGroup],
) -> dict:
    """Parse model JSON response into structured reasoning."""
    from nthlayer_common.parsing import clamp, strip_markdown_fences

    text = strip_markdown_fences(response_text)
    data = json.loads(text)

    # Validate and normalize group assessments
    group_assessments = data.get("groups", [])
    valid_group_ids = {g.id for g in groups}

    normalized = []
    for ga in group_assessments:
        gid = ga.get("group_id", "")
        if gid not in valid_group_ids:
            logger.debug("reasoning_unknown_group_id", group_id=gid)
            continue

        confidence = clamp(float(ga.get("confidence", 0.5)))

        normalized.append({
            "group_id": gid,
            "root_cause": ga.get("root_cause"),
            "confidence": confidence,
            "reasoning": ga.get("reasoning", ""),
            "recommended_actions": ga.get("recommended_actions", []),
            "is_causal": ga.get("is_causal"),
            "degraded": False,
        })

    overall_confidence = clamp(float(data.get("overall_confidence", 0.0)))

    return {
        "groups": normalized,
        "overall_assessment": data.get("overall_assessment", ""),
        "overall_confidence": overall_confidence,
        "degraded": False,
    }
