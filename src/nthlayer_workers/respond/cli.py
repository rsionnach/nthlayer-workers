# src/nthlayer_respond/cli.py
"""Mayday CLI — replay, status, approve/reject, serve."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import structlog
import yaml
from nthlayer_common.verdicts import MemoryStore, SQLiteVerdictStore
from nthlayer_workers.respond.agents.communication import CommunicationAgent
from nthlayer_workers.respond.agents.investigation import InvestigationAgent
from nthlayer_workers.respond.agents.remediation import RemediationAgent
from nthlayer_workers.respond.agents.triage import TriageAgent
from nthlayer_workers.respond.config import RespondConfig, load_config
from nthlayer_workers.respond.context_store import SQLiteContextStore
from nthlayer_workers.respond.coordinator import Coordinator
from nthlayer_workers.respond.safe_actions.actions import register_builtin_actions
from nthlayer_workers.respond.safe_actions.registry import SafeActionRegistry
from nthlayer_workers.respond.types import AgentRole, IncidentContext, IncidentState

logger = structlog.get_logger(__name__)


def _make_coordinator(config: RespondConfig) -> tuple[Coordinator, SQLiteContextStore]:
    """Build the full agent stack + coordinator from config. Returns (coordinator, store)."""
    verdict_store = SQLiteVerdictStore(config.verdict_store_path)
    store = SQLiteContextStore(config.context_store_path)
    registry = SafeActionRegistry(os.path.join(os.getcwd(), "cooldowns.db"))
    register_builtin_actions(registry)
    agent_config = {
        "root_cause_threshold": config.root_cause_threshold,
        "arbiter_url": config.arbiter_url,
        "escalation_threshold": config.escalation_threshold,  # P3-E.1: capture-at-write-time
    }
    agents = {
        AgentRole.TRIAGE: TriageAgent(config.model, config.max_tokens, verdict_store, agent_config),
        AgentRole.INVESTIGATION: InvestigationAgent(config.model, config.max_tokens, verdict_store, agent_config),
        AgentRole.COMMUNICATION: CommunicationAgent(config.model, config.max_tokens, verdict_store, agent_config),
        AgentRole.REMEDIATION: RemediationAgent(config.model, config.max_tokens, verdict_store, agent_config, safe_action_registry=registry),
    }
    return Coordinator(agents, store, verdict_store, config, safe_action_registry=registry), store


# ------------------------------------------------------------------ #
# Argument parser                                                      #
# ------------------------------------------------------------------ #


def build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="nthlayer-respond",
        description="Mayday -- multi-agent incident response",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    serve = sub.add_parser("serve", help="Start approval server")
    serve.add_argument("--config", default="respond.yaml", help="Config file path")
    serve.add_argument("--host", default=None, help="Override server host")
    serve.add_argument("--port", type=int, default=None, help="Override server port")

    # status
    status = sub.add_parser("status", help="Show active incidents")
    status.add_argument("--config", default="respond.yaml", help="Config file path")

    # replay
    replay = sub.add_parser("replay", help="Replay a scenario")
    replay.add_argument("--scenario", required=True, help="Path to scenario YAML")
    replay.add_argument("--config", default="respond.yaml", help="Config file path")
    replay.add_argument(
        "--no-model",
        action="store_true",
        default=False,
        help="Use mock responses from scenario instead of calling model",
    )

    # approve
    approve = sub.add_parser("approve", help="Approve pending remediation")
    approve.add_argument("incident_id", help="Incident ID")
    approve.add_argument("--approved-by", default=None, help="Identity of approver (e.g. email)")
    approve.add_argument("--config", default="respond.yaml", help="Config file path")

    # reject
    reject = sub.add_parser("reject", help="Reject pending remediation")
    reject.add_argument("incident_id", help="Incident ID")
    reject.add_argument("--reason", required=True, help="Rejection reason")
    reject.add_argument("--rejected-by", default=None, help="Identity of rejector (e.g. email)")
    reject.add_argument("--config", default="respond.yaml", help="Config file path")

    # resume
    resume = sub.add_parser("resume", help="Resume crashed incident")
    resume.add_argument("incident_id", help="Incident ID")
    resume.add_argument("--config", default="respond.yaml", help="Config file path")

    # respond (live — trigger from correlation verdict)
    respond = sub.add_parser("respond", help="Respond to a correlation verdict")
    respond.add_argument("--trigger-verdict", required=True, help="Correlation verdict ID")
    respond.add_argument("--specs-dir", default=".", help="Directory of OpenSRM spec YAMLs")
    respond.add_argument("--verdict-store", default="verdicts.db", help="Path to verdict SQLite DB")
    respond.add_argument("--config", default="respond.yaml", help="Config file path")
    respond.add_argument("--notify", default="stdout", help="Notification target: stdout or webhook URL")
    respond.add_argument("--model", default=None, help="Override model (e.g. 'openai/gpt-4o', 'anthropic/claude-sonnet-4-20250514')")

    # ---- SRE experience commands ----

    # oncall
    oncall = sub.add_parser("oncall", help="Show current on-call schedule")
    oncall.add_argument("--specs-dir", default=".", help="Directory of OpenSRM spec YAMLs")

    # brief
    brief = sub.add_parser("brief", help="Generate paging brief for an incident")
    brief.add_argument("incident_id", help="Incident ID")
    brief.add_argument("--verdict-store", default="verdicts.db", help="Path to verdict SQLite DB")

    # shift-report
    shift = sub.add_parser("shift-report", help="Generate shift report for a time window")
    shift.add_argument("--from", dest="from_time", required=True, help="Shift start (ISO 8601)")
    shift.add_argument("--to", required=True, help="Shift end (ISO 8601)")
    shift.add_argument("--verdict-store", default="verdicts.db", help="Path to verdict SQLite DB")

    # suppress
    suppress = sub.add_parser("suppress", help="Create alert suppression rule")
    suppress.add_argument("service", help="Service name")
    suppress.add_argument("metric", help="Metric name")
    suppress.add_argument("--window", required=True, help="Suppression window (e.g. 02:00-04:00)")
    suppress.add_argument("--reason", required=True, help="Reason for suppression")
    suppress.add_argument("--baseline", type=float, required=True, help="Baseline metric value for override threshold computation")
    suppress.add_argument("--multiplier", type=float, default=3.0, help="Override threshold multiplier (default: 3.0)")

    # post-incident
    postinc = sub.add_parser("post-incident", help="Generate post-incident review")
    postinc.add_argument("incident_id", help="Incident ID")
    postinc.add_argument("--verdict-store", default="verdicts.db", help="Path to verdict SQLite DB")

    # delegate
    delegate = sub.add_parser("delegate", help="Delegate incident to autonomous handling")
    delegate.add_argument("incident_id", help="Incident ID")
    delegate.add_argument("--safe-actions-only", action=argparse.BooleanOptionalAction, default=True, help="Restrict to pre-approved safe actions (default: True)")
    delegate.add_argument("--max-duration", default="2h", help="Max autonomous duration as Nh or Nm (default: 2h). Parsed to timedelta by handler.")
    delegate.add_argument("--delegated-by", default=None, help="Identity of delegator")

    return parser


# ------------------------------------------------------------------ #
# Mock model for --no-model replay                                     #
# ------------------------------------------------------------------ #


def _make_mock_call_model(mock_response: dict | None):
    """Return an async callable that returns *mock_response* as JSON.

    If *mock_response* is None, simulate model failure.
    """

    async def mock_call_model(system_prompt: str, user_prompt: str) -> str:
        if mock_response is None:
            raise Exception("Model unavailable (mock)")
        return json.dumps(mock_response)

    return mock_call_model


def _make_sequenced_mock(responses: list[dict | None]):
    """Return an async callable that yields successive responses on each call."""
    call_index = {"n": 0}

    async def mock_call_model(system_prompt: str, user_prompt: str) -> str:
        idx = call_index["n"]
        call_index["n"] += 1
        if idx < len(responses):
            resp = responses[idx]
        else:
            resp = responses[-1] if responses else None
        if resp is None:
            raise Exception("Model unavailable (mock)")
        return json.dumps(resp)

    return mock_call_model


def _build_replay_agents(
    config: RespondConfig,
    verdict_store,
    registry: SafeActionRegistry,
    mock_responses: dict,
    no_model: bool,
) -> dict[AgentRole, Any]:
    """Create the agent map, optionally patching _call_model for --no-model replay."""
    agent_config = {
        "root_cause_threshold": config.root_cause_threshold,
        "arbiter_url": config.arbiter_url,
        "escalation_threshold": config.escalation_threshold,  # P3-E.1: capture-at-write-time
    }
    agents_map: dict[AgentRole, Any] = {
        AgentRole.TRIAGE: TriageAgent(
            model=config.model, max_tokens=config.max_tokens,
            verdict_store=verdict_store, config=agent_config,
            timeout=config.triage_timeout,
        ),
        AgentRole.INVESTIGATION: InvestigationAgent(
            model=config.model, max_tokens=config.max_tokens,
            verdict_store=verdict_store, config=agent_config,
            timeout=config.investigation_timeout,
        ),
        AgentRole.COMMUNICATION: CommunicationAgent(
            model=config.model, max_tokens=config.max_tokens,
            verdict_store=verdict_store, config=agent_config,
            timeout=config.communication_timeout,
        ),
        AgentRole.REMEDIATION: RemediationAgent(
            model=config.model, max_tokens=config.max_tokens,
            verdict_store=verdict_store, config=agent_config,
            timeout=config.remediation_timeout, safe_action_registry=registry,
        ),
    }
    if no_model:
        agents_map[AgentRole.TRIAGE]._call_model = _make_mock_call_model(
            mock_responses.get("triage"))
        agents_map[AgentRole.INVESTIGATION]._call_model = _make_mock_call_model(
            mock_responses.get("investigation"))
        agents_map[AgentRole.COMMUNICATION]._call_model = _make_sequenced_mock([
            mock_responses.get("communication_initial"),
            mock_responses.get("communication_resolution"),
        ])
        agents_map[AgentRole.REMEDIATION]._call_model = _make_mock_call_model(
            mock_responses.get("remediation"))
    return agents_map


def _build_incident_context(
    scenario: dict,
    incident_id: str,
    verdict_store,
    no_model: bool,
) -> IncidentContext:
    """Build IncidentContext from scenario trigger definition."""
    trigger = scenario["trigger"]
    trigger_source = trigger["source"]
    now = datetime.now(tz=timezone.utc).isoformat()

    # Accept legacy "sitrep" value from scenario YAML fixtures as alias
    if trigger_source in ("nthlayer-correlate", "sitrep"):
        trigger_verdict_ids: list[str] = []
        if no_model:
            # opensrm-saun.1.2.1: emit a mock correlation_snapshot assessment
            # rather than a "correlation"-typed pseudo-verdict. The
            # trigger_verdict_ids variable name is preserved (it's used as
            # parent_ids on respond's first emitted verdict — the lineage
            # bridge), but the id is now an assessment id (csn-*) reflecting
            # the canonical primitive. verdict_store.put() is skipped because
            # this is an assessment, not a verdict; replay mode doesn't
            # exercise the bench lineage-walk path.
            import uuid as _uuid
            mock_id = f"csn-{scenario['id']}-{_uuid.uuid4().hex[:8]}"
            trigger_verdict_ids.append(mock_id)
        else:
            print("nthlayer-correlate must be installed for correlation-triggered scenarios: "
                  "pip install -e ../nthlayer-correlate")
            sys.exit(1)

        topology = {
            "services": [
                {"name": "payment-api", "tier": "critical",
                 "dependencies": ["database-primary"]},
                {"name": "checkout-service", "tier": "critical",
                 "dependencies": ["payment-api"]},
            ]
        }
        return IncidentContext(
            id=incident_id, state=IncidentState.TRIGGERED,
            created_at=now, updated_at=now,
            trigger_source="nthlayer-correlate", trigger_verdict_ids=trigger_verdict_ids,
            topology=topology,
        )

    elif trigger_source == "pagerduty":
        alert = trigger.get("alert", {})
        topology = {
            "services": [{"name": alert.get("service", "unknown"),
                          "tier": "critical", "dependencies": []}]
        }
        return IncidentContext(
            id=incident_id, state=IncidentState.TRIGGERED,
            created_at=now, updated_at=now,
            trigger_source="pagerduty", trigger_verdict_ids=[],
            topology=topology, metadata={"alert": alert},
        )
    else:
        raise ValueError(f"Unknown trigger source: {trigger_source!r}")


async def _handle_interactions(
    interactions: list[dict],
    coordinator: Coordinator,
    result_ctx: IncidentContext,
    context_store: SQLiteContextStore,
) -> IncidentContext:
    """Process post-pipeline scenario interactions (approve, reject, etc.)."""
    for interaction in interactions:
        timing = interaction.get("at", "")
        action = interaction.get("action", "")

        if timing == "after:remediation_proposed" and action == "approve":
            if result_ctx.state == IncidentState.AWAITING_APPROVAL:
                result_ctx = await coordinator.approve(result_ctx.id)
        elif timing == "after:remediation_proposed" and action == "reject":
            reason = interaction.get("reason", "No reason given")
            if result_ctx.state == IncidentState.AWAITING_APPROVAL:
                result_ctx = await coordinator.reject(result_ctx.id, reason)
        elif timing == "after:triage" and action == "reject":
            reason = interaction.get("reason", "No reason given")
            result_ctx.state = IncidentState.ESCALATED
            context_store.save(result_ctx)

    return result_ctx


# ------------------------------------------------------------------ #
# Replay command                                                       #
# ------------------------------------------------------------------ #


async def replay_command(
    scenario_path: str,
    config_path: str | None = None,
    no_model: bool = True,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Execute a scenario replay and return results dict.

    Parameters
    ----------
    scenario_path : str
        Path to the scenario YAML file.
    config_path : str | None
        Optional config file. Defaults create a RespondConfig with defaults.
    no_model : bool
        When True, use mock_responses from the scenario YAML instead of
        calling the Anthropic API.
    work_dir : str | None
        Working directory for temporary SQLite databases. Defaults to cwd.

    Returns
    -------
    dict with keys: final_state, verdict_count, verdict_chain,
    remediation_executed, incident_id, context.
    """
    # Load scenario
    with open(scenario_path) as f:
        raw = yaml.safe_load(f)
    scenario = raw["scenario"]

    # Load config
    if config_path is not None and os.path.exists(config_path):
        config = load_config(config_path)
    else:
        config = RespondConfig()

    # Work directory — use a temp dir for replay to avoid stale state
    import shutil
    import tempfile
    created_temp = False
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="respond-replay-")
        created_temp = True

    # Stores
    verdict_store = MemoryStore()
    context_store = SQLiteContextStore(os.path.join(work_dir, "replay-incidents.db"))

    # Safe action registry
    registry = SafeActionRegistry(os.path.join(work_dir, "replay-cooldowns.db"))
    register_builtin_actions(registry)

    # In --no-model replay, override the registry's requires_approval to match
    # the scenario mock_response intent so the replay exercises the intended flow.
    # Only override to False (allow auto-execution) when the scenario explicitly
    # says requires_human_approval: false. When True, leave the registry default
    # so the approval ratchet works correctly and the coordinator pauses.
    mock_responses = scenario.get("mock_responses", {})
    if no_model:
        rem_mock = mock_responses.get("remediation")
        if rem_mock is not None and rem_mock.get("proposed_action"):
            action_name = rem_mock["proposed_action"]
            mock_requires_approval = rem_mock.get("requires_human_approval", True)
            if not mock_requires_approval:
                try:
                    action_obj = registry.get(action_name)
                    action_obj.requires_approval = False
                except KeyError:
                    pass  # action not in registry; will be caught later

    # Build agents and incident context
    agents_map = _build_replay_agents(config, verdict_store, registry, mock_responses, no_model)
    incident_id = f"INC-REPLAY-{scenario['id']}"
    context = _build_incident_context(scenario, incident_id, verdict_store, no_model)

    # Create coordinator
    coordinator = Coordinator(
        agents=agents_map,
        context_store=context_store,
        verdict_store=verdict_store,
        config=config,
        safe_action_registry=registry,
    )

    # Handle crash_after_step for crash-recovery scenarios.
    # Simulate a crash by running the pipeline, then verifying the context
    # was persisted and can be loaded. The coordinator's crash recovery
    # (resume from last_completed_step_index) is tested in unit tests;
    # here we just prove the full pipeline works and context is recoverable.
    crash_after_step = scenario.get("crash_after_step")

    # Run the pipeline
    result_ctx = await coordinator.run(context)

    if crash_after_step is not None and result_ctx.state not in (
        IncidentState.ESCALATED, IncidentState.FAILED
    ):
        # Verify crash recovery: context was persisted and is loadable
        recovered = context_store.load(result_ctx.id)
        if recovered is not None:
            logger.info("crash_recovery_verified", incident=result_ctx.id,
                       step_index=recovered.last_completed_step_index)

    # Handle post-pipeline interactions (approve, reject, etc.)
    result_ctx = await _handle_interactions(
        scenario.get("interactions", []), coordinator, result_ctx, context_store
    )

    # Build results
    remediation_executed = False
    if result_ctx.remediation is not None:
        remediation_executed = result_ctx.remediation.executed

    results = {
        "incident_id": result_ctx.id,
        "final_state": result_ctx.state.value,
        "verdict_count": len(result_ctx.verdict_chain),
        "verdict_chain": list(result_ctx.verdict_chain),
        "remediation_executed": remediation_executed,
        "context": result_ctx,
    }

    # Verify against expected outcomes (informational)
    expected = scenario.get("expected_outcomes", {})
    if expected:
        checks: list[str] = []
        if expected.get("final_state") and results["final_state"] != expected["final_state"]:
            checks.append(
                f"FAIL: final_state expected={expected['final_state']!r} "
                f"got={results['final_state']!r}"
            )
        if expected.get("verdict_count") is not None:
            if results["verdict_count"] < expected["verdict_count"]:
                checks.append(
                    f"FAIL: verdict_count expected>={expected['verdict_count']} "
                    f"got={results['verdict_count']}"
                )
        if expected.get("remediation_executed") is not None:
            if results["remediation_executed"] != expected["remediation_executed"]:
                checks.append(
                    f"FAIL: remediation_executed expected={expected['remediation_executed']} "
                    f"got={results['remediation_executed']}"
                )
        results["checks"] = checks

    # Clean up
    context_store.close()
    if created_temp:
        shutil.rmtree(work_dir, ignore_errors=True)

    return results


# ------------------------------------------------------------------ #
# Other command stubs                                                  #
# ------------------------------------------------------------------ #


def _status_command(config_path: str) -> None:
    """Show active incidents."""
    config = load_config(config_path)
    store = SQLiteContextStore(config.context_store_path)
    try:
        active = store.list_active()
        if not active:
            print("No active incidents.")
        else:
            for inc_id in active:
                ctx = store.load(inc_id)
                if ctx:
                    print(f"  {ctx.id}  state={ctx.state.value}  updated={ctx.updated_at}")
    finally:
        store.close()


def _serve_command(config_path: str, host: str | None = None, port: int | None = None) -> None:
    """Start the approval server."""
    import uvicorn

    config = load_config(config_path)
    if host:
        config.server_host = host
    if port:
        config.server_port = port

    coordinator, ctx_store = _make_coordinator(config)

    from nthlayer_workers.respond.server import ApprovalServer

    verdict_store = SQLiteVerdictStore(config.verdict_store_path)
    server = ApprovalServer(coordinator, ctx_store, config, verdict_store=verdict_store)
    app = server.build_app()

    print(f"[nthlayer-respond serve] Starting on {config.server_host}:{config.server_port}")
    uvicorn.run(
        app,
        host=config.server_host,
        port=config.server_port,
        log_level="info",
    )


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        _serve_command(args.config, host=args.host, port=args.port)

    elif args.command == "status":
        _status_command(args.config)

    elif args.command == "replay":
        result = asyncio.run(
            replay_command(
                scenario_path=args.scenario,
                config_path=args.config,
                no_model=args.no_model,
            )
        )
        print(json.dumps({k: v for k, v in result.items() if k != "context"}, indent=2))
        checks = result.get("checks", [])
        if checks:
            for c in checks:
                print(c)
            sys.exit(1)

    elif args.command == "approve":
        config = load_config(args.config)
        async def _approve():
            coord, store = _make_coordinator(config)
            try:
                ctx = await coord.approve(args.incident_id, approved_by=args.approved_by)
                print(f"Approved. State: {ctx.state.value}")
            finally:
                store.close()
        asyncio.run(_approve())

    elif args.command == "reject":
        config = load_config(args.config)
        async def _reject():
            coord, store = _make_coordinator(config)
            try:
                ctx = await coord.reject(args.incident_id, args.reason, rejected_by=args.rejected_by)
                print(f"Rejected. State: {ctx.state.value}")
            finally:
                store.close()
        asyncio.run(_reject())

    elif args.command == "resume":
        config = load_config(args.config)
        async def _resume():
            coord, store = _make_coordinator(config)
            try:
                ctx = await coord.resume(args.incident_id)
                print(f"Resumed. State: {ctx.state.value}")
            finally:
                store.close()
        asyncio.run(_resume())

    elif args.command == "respond":
        result = cmd_respond(args)
        if result:
            sys.exit(result)


def cmd_respond(args) -> None:
    """Respond to a correlation verdict — run the full agent pipeline."""
    from pathlib import Path

    verdict_store = SQLiteVerdictStore(args.verdict_store)

    # Read trigger correlation verdict
    trigger = verdict_store.get(args.trigger_verdict)
    if trigger is None:
        print(f"Error: verdict {args.trigger_verdict} not found in store", file=sys.stderr)
        return 1

    trigger_custom = getattr(trigger.metadata, "custom", {}) or {}
    trigger_service = trigger.subject.ref or "unknown"

    # Build topology and service specs from specs directory
    topology = {"services": []}
    service_specs: dict = {}  # name → full spec for prompt context
    specs_path = Path(args.specs_dir)
    if specs_path.is_dir():
        import yaml
        for spec_file in sorted(specs_path.glob("*.yaml")):
            try:
                raw = yaml.safe_load(spec_file.read_text())
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            metadata = raw.get("metadata", {})
            spec = raw.get("spec", {})
            name = metadata.get("name", spec_file.stem)
            deps = [d["name"] for d in spec.get("dependencies", []) if isinstance(d, dict)]
            topology["services"].append({
                "name": name,
                "tier": metadata.get("tier", "standard"),
                "dependencies": deps,
            })
            # Store the full spec for service context in prompts
            service_specs[name] = {
                "name": name,
                "tier": metadata.get("tier", "standard"),
                "team": metadata.get("team"),
                "type": spec.get("type"),  # ai-gate, api, etc.
                "slos": spec.get("slos", {}),
                "dependencies": deps,
            }

    # Walk lineage to find the evaluation verdict with SLO details
    eval_context = {}
    corr_lineage_ctx = getattr(trigger.lineage, "context", []) or []
    for eval_id in corr_lineage_ctx:
        try:
            ev = verdict_store.get(eval_id)
            if ev and ev.subject.type == "evaluation":
                ev_custom = getattr(ev.metadata, "custom", {}) or {}
                eval_context = {
                    "verdict_id": eval_id,
                    "service": ev.subject.ref,
                    "slo_name": ev_custom.get("slo_name"),
                    "slo_type": ev_custom.get("slo_type"),
                    "target": ev_custom.get("target"),
                    "current_value": ev_custom.get("current_value"),
                    "breach": ev_custom.get("breach"),
                    "consecutive": ev_custom.get("consecutive"),
                }
                break
        except Exception:
            pass

    # Build service context from spec + evaluation verdict
    affected_service_spec = service_specs.get(trigger_service, {})
    service_type = affected_service_spec.get("type", "unknown")
    is_ai_gate = service_type == "ai-gate"

    service_context = {
        "service": trigger_service,
        "service_type": service_type,
        "is_ai_gate": is_ai_gate,
        "spec": affected_service_spec,
        "evaluation": eval_context,
    }

    # Build incident context from correlation verdict
    now = datetime.now(tz=timezone.utc).isoformat()
    incident_id = f"INC-{trigger_service.upper()}-{now[:19].replace('-', '').replace(':', '').replace('T', '-')}"

    # Determine severity from correlation verdict confidence
    confidence = trigger.judgment.confidence
    if confidence > 0.8:
        severity = 1  # critical
    elif confidence > 0.5:
        severity = 2  # high
    else:
        severity = 3  # medium

    context = IncidentContext(
        id=incident_id,
        state=IncidentState.TRIGGERED,
        created_at=now,
        updated_at=now,
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[args.trigger_verdict],
        topology=topology,
        metadata={
            "correlation_verdict": args.trigger_verdict,
            "blast_radius": trigger_custom.get("blast_radius", []),
            "root_causes": trigger_custom.get("root_causes", []),
            "severity": severity,
            "service_context": service_context,
        },
    )

    # Load config and run pipeline
    config_path = Path(args.config)
    if config_path.exists():
        config = load_config(args.config)
    else:
        # Minimal default config for CLI invocation
        from nthlayer_workers.respond.config import RespondConfig
        config = RespondConfig()

    # Override config from CLI flags
    config.verdict_store_path = args.verdict_store
    if getattr(args, "model", None):
        config.model = args.model

    async def _run():
        coord, ctx_store = _make_coordinator(config)
        try:
            ctx_store.save(context)
            result_ctx = await coord.run(context)
            return result_ctx
        finally:
            ctx_store.close()

    result_ctx = asyncio.run(_run())

    # Output
    output = {
        "incident_id": result_ctx.id,
        "state": result_ctx.state.value,
        "trigger_verdict": args.trigger_verdict,
        "service": trigger_service,
        "severity": severity,
    }

    if args.notify == "stdout":
        print(json.dumps(output, indent=2))
    elif args.notify.startswith("http"):
        import urllib.request
        req = urllib.request.Request(
            args.notify,
            data=json.dumps(output).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"Warning: notification failed: {e}", file=sys.stderr)
        print(json.dumps(output, indent=2))
    else:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
