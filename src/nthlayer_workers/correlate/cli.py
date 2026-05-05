"""SitRep CLI — serve, status, replay."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
import yaml

from nthlayer_workers.correlate.config import SitRepConfig, load_config
from nthlayer_workers.correlate.correlation.changes import find_change_candidates
from nthlayer_workers.correlate.correlation.dedup import deduplicate
from nthlayer_workers.correlate.correlation.engine import CorrelationEngine
from nthlayer_workers.correlate.correlation.temporal import group_temporal
from nthlayer_workers.correlate.correlation.topology import group_topology
from nthlayer_workers.correlate.ingestion.severity import pre_score
from nthlayer_workers.correlate.snapshot.generator import SnapshotBudget, SnapshotGenerator
from nthlayer_workers.correlate.snapshot.model import ModelInterface
from nthlayer_workers.correlate.state import StateMachine
from nthlayer_workers.correlate.store.sqlite import SQLiteEventStore
from nthlayer_workers.correlate.types import AgentState, EventType, SitRepEvent

logger = structlog.get_logger()

REFERENCE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Confidence decay window for temporal proximity heuristic (30 minutes)
_PROXIMITY_WINDOW_SECONDS = 1800.0

_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_duration(value: str) -> timedelta:
    """Parse a duration string (e.g. '1h', '30m', '2h') to timedelta."""
    for suffix, multiplier in sorted(_DURATION_UNITS.items(), key=lambda x: -len(x[0])):
        if value.endswith(suffix):
            return timedelta(seconds=float(value[:-len(suffix)]) * multiplier)
    return timedelta(hours=1)  # default fallback


def _proximity_confidence(seconds: float | None) -> float:
    """Heuristic confidence from temporal proximity of a change to a signal.

    Decays linearly from 1.0 (simultaneous) to 0.0 (30 minutes apart).
    Returns 0.5 for unknown proximity.
    """
    if seconds is None:
        return 0.5
    from nthlayer_common.parsing import clamp
    return clamp(1.0 - seconds / _PROXIMITY_WINDOW_SECONDS)


def parse_relative_time(at_str: str) -> str:
    """Parse 'T+Nm' to ISO 8601."""
    match = re.match(r"T\+(\d+)m", at_str)
    if not match:
        raise ValueError(f"Invalid time format: {at_str}")
    minutes = int(match.group(1))
    ts = REFERENCE_TIME + timedelta(minutes=minutes)
    return ts.isoformat()


def scenario_event_to_sitrep(evt_data: dict, index: int) -> SitRepEvent:
    """Convert a scenario event dict to a SitRepEvent."""
    ts = parse_relative_time(evt_data["at"])
    payload = evt_data["payload"]
    service = payload.get("service", "unknown")
    return SitRepEvent(
        id=f"scenario-evt-{index:04d}",
        timestamp=ts,
        source="scenario",
        type=EventType(evt_data["type"]),
        service=service,
        environment="production",
        severity=0.5,
        payload=payload,
    )


def _build_topology_dict(scenario: dict) -> dict[str, Any] | None:
    """Build the topology dict expected by the correlation engine from scenario YAML."""
    topo_section = scenario.get("topology")
    if not topo_section:
        return None
    services = topo_section.get("services", [])
    if not services:
        return None
    result: dict[str, Any] = {}
    for svc in services:
        name = svc["name"]
        result[name] = {
            "tier": svc.get("tier", "standard"),
            "dependencies": svc.get("dependencies", []),
            "dependents": svc.get("dependents", []),
        }
    return result


def replay_command(
    scenario_path: str,
    config_path: str | None,
    no_model: bool,
    store_dir: str,
) -> int:
    """Replay a scenario fixture. Returns exit code."""
    # Load scenario
    try:
        with open(scenario_path) as f:
            raw = yaml.safe_load(f)
    except (FileNotFoundError, OSError) as exc:
        logger.error("scenario_load_failed", path=scenario_path, error=str(exc))
        return 2

    scenario = raw.get("scenario", raw)

    # Parse events
    events_data = scenario.get("events", [])
    events: list[SitRepEvent] = []
    for i, evt_data in enumerate(events_data):
        try:
            event = scenario_event_to_sitrep(evt_data, i)
            events.append(event)
        except (ValueError, KeyError) as exc:
            logger.warning("event_parse_failed", index=i, error=str(exc))

    # Build topology
    topology = _build_topology_dict(scenario)

    # Open temp store
    db_path = os.path.join(store_dir, "replay.db")
    store = SQLiteEventStore(db_path)

    try:
        # Insert events
        if events:
            store.insert_batch(events)

        # Compute the actual time window from events for correlation
        if events:
            timestamps = [e.timestamp for e in events]
            start_ts = min(timestamps)
            end_ts = max(timestamps)
            # Add a buffer to ensure the window captures all events
            end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
            end_buffered = (end_dt + timedelta(minutes=1)).isoformat()

            # Run correlation sub-steps manually (since engine uses datetime.now())
            all_events = store.get_by_time_window(start_ts, end_buffered)

            if all_events:
                # Deduplicate
                deduped = deduplicate(all_events)

                # Severity enrichment
                enriched = []
                for event in deduped:
                    new_severity = pre_score(event, None)
                    if new_severity != event.severity:
                        event = replace(event, severity=new_severity)
                    enriched.append(event)

                # Compute window minutes from the event spread
                start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                window_minutes = max(
                    int((end_dt - start_dt).total_seconds() / 60) + 1,
                    5,
                )

                # Temporal grouping
                temporal_groups = group_temporal(enriched, window_minutes=window_minutes)

                # Topology grouping
                topology_correlations = group_topology(temporal_groups, topology)

                # Change candidate indexing
                # Since get_recent_changes uses datetime('now'), we query the store
                # directly for change events in our scenario window
                change_candidates_map = find_change_candidates(
                    store, temporal_groups, topology=topology,
                    window_minutes=window_minutes + 30,
                )

                # Assemble correlation groups using the engine helper
                engine = CorrelationEngine()
                groups = engine.assemble_groups(
                    temporal_groups, topology_correlations,
                    change_candidates_map, topology,
                )
            else:
                groups = []
        else:
            groups = []

        # Report
        scenario_id = scenario.get("id", "unknown")
        print(f"\n=== Replay: {scenario_id} ===")
        print(f"Events inserted: {len(events)}")
        print(f"Correlation groups found: {len(groups)}")

        services_affected: set[str] = set()
        total_changes = 0
        for g in groups:
            services_affected.update(g.services)
            total_changes += len(g.change_candidates)
            print(f"  [{g.id}] P{g.priority}: {g.summary}")

        print(f"Services affected: {sorted(services_affected)}")
        print(f"Change candidates: {total_changes}")

        if not no_model:
            # Model-enabled path
            config = load_config(config_path) if config_path else SitRepConfig()
            generator = SnapshotGenerator(SnapshotBudget(config.token_budget))
            model = ModelInterface(config.model_name, config.model_max_tokens)

            if groups:
                prompt, cache_hit = generator.generate(groups, AgentState.WATCHING)
                try:
                    assessments = asyncio.run(model.interpret(prompt, groups))
                    print(f"Correlation assessments created: {len(assessments)}")
                except Exception as exc:
                    logger.warning("model_call_failed", error=str(exc))
                    print("Model call failed, skipping assessments")
        else:
            print("Model: skipped (--no-model)")

        print()
        return 0

    finally:
        store.close()



def status_command(config_path: str | None, store_dir: str | None = None) -> int:
    """Show current SitRep status. Returns exit code."""
    config = load_config(config_path) if config_path else SitRepConfig()

    # Use store_dir if provided (for testing), otherwise use config path
    if store_dir:
        db_path = os.path.join(store_dir, "sitrep-events.db")
    else:
        db_path = config.store_path

    store = SQLiteEventStore(db_path)
    try:
        stats = store.get_stats()

        print("\n=== SitRep Status ===")
        print(f"Agent state: {AgentState.WATCHING.value}")
        print(f"Event count: {stats['event_count']}")
        print(f"Oldest event: {stats['min_timestamp'] or 'none'}")
        print(f"Newest event: {stats['max_timestamp'] or 'none'}")

        # DB file size
        if os.path.exists(db_path):
            size_bytes = os.path.getsize(db_path)
            if size_bytes < 1024:
                size_str = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
            print(f"DB size: {size_str}")
        else:
            print("DB size: 0 B")

        print()
        return 0

    finally:
        store.close()


async def _serve_loop(config: SitRepConfig) -> None:
    """Run the full serve pipeline."""
    from nthlayer_workers.correlate.ingestion.webhook import WebhookIngester

    store = SQLiteEventStore(config.store_path)
    ingester = WebhookIngester(config.ingestion_host, config.ingestion_port)
    engine = CorrelationEngine()
    generator = SnapshotGenerator(SnapshotBudget(config.token_budget))
    model = ModelInterface(config.model_name, config.model_max_tokens)
    state_machine = StateMachine()

    # Buffer events via queue to avoid concurrent SQLite writes
    event_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
    ingester.on_event(lambda event: event_queue.put_nowait(event))

    # Start ingester
    await ingester.start()
    logger.info(
        "sitrep_started",
        host=config.ingestion_host,
        port=config.ingestion_port,
    )

    # Shutdown event
    shutdown = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("shutdown_requested")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    # Optionally open assessment store. opensrm-saun.1.2.1: model.interpret()
    # now emits correlation_snapshot assessments (was: correlation pseudo-
    # verdicts). The legacy nthlayer_learn.store.VerdictStore import path is
    # removed; serve mode persists via SQLiteAssessmentStore sharing the
    # configured verdict-store db file.
    assessment_store = None
    try:
        from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore
        assessment_store = SQLiteAssessmentStore(config.verdict_store_path)
    except Exception:
        logger.info("assessment_store_not_available")

    try:
        while not shutdown.is_set():
            interval = state_machine.get_interval()
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal cycle

            # Drain buffered events into store (single-threaded, safe)
            while not event_queue.empty():
                try:
                    event = event_queue.get_nowait()
                    store.insert(event)
                except asyncio.QueueEmpty:
                    break

            # Run correlation
            try:
                groups = engine.correlate(
                    store,
                    config.correlation_window_minutes,
                    topology=None,  # No manifests in Tier 1
                )

                model_healthy = True
                state_machine.update(groups, model_healthy)

                prompt, cache_hit = generator.generate(groups, state_machine.state)

                if not cache_hit and groups:
                    try:
                        await model.interpret(prompt, groups, assessment_store)
                    except Exception as exc:
                        logger.warning("model_call_failed", error=str(exc))
                        state_machine.update(groups, model_healthy=False)

                logger.info(
                    "correlation_cycle",
                    state=state_machine.state.value,
                    groups=len(groups),
                    cache_hit=cache_hit,
                )

            except Exception as exc:
                logger.error("correlation_error", error=str(exc))

    finally:
        await ingester.stop()
        store.close()
        logger.info("sitrep_stopped")


def serve_command(config_path: str | None) -> int:
    """Start the full SitRep pipeline. Returns exit code."""
    config = load_config(config_path) if config_path else SitRepConfig()
    try:
        asyncio.run(_serve_loop(config))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("serve_failed", error=str(exc))
        return 2


def correlate_command(
    trigger_verdict_id: str,
    prometheus_url: str,
    specs_dir: str,
    verdict_store_path: str,
    respond_args: str | None = None,
    reasoning: bool = True,
    reasoning_model: str | None = None,
    decision_store_path: str | None = None,
    trace_backend: object | None = None,  # TraceBackend Protocol (not runtime_checkable)
    trace_baseline_window: str = "1h",
) -> int:
    """Correlate signals from a trigger evaluation verdict.

    Reads the trigger verdict from the store, queries Prometheus for correlated
    signals across the blast radius, runs the correlation engine, and writes
    a correlation verdict.
    """
    from nthlayer_common.verdicts import (
        SQLiteVerdictStore,
        VerdictFilter,
    )

    from nthlayer_workers.correlate.prometheus import (
        blast_radius_services,
        fetch_alerts,
        fetch_metric_breaches,
        load_dependency_graph,
        verdict_to_event,
    )

    log = structlog.get_logger("correlate_command")

    # Open verdict store
    verdict_store = SQLiteVerdictStore(verdict_store_path)

    # Read trigger verdict
    trigger = verdict_store.get(trigger_verdict_id)
    if trigger is None:
        log.error("Trigger verdict not found", verdict_id=trigger_verdict_id)
        return 1

    trigger_service = trigger.subject.ref or "unknown"
    trigger_custom = getattr(trigger.metadata, "custom", {}) or {}
    log.info(
        "Trigger verdict loaded",
        service=trigger_service,
        slo_name=trigger_custom.get("slo_name"),
        breach=trigger_custom.get("breach"),
    )

    # Load dependency graph from specs
    dep_graph = load_dependency_graph(specs_dir)

    # Compute blast radius
    affected = blast_radius_services(trigger_service, dep_graph)
    log.info("Blast radius computed", affected_services=sorted(affected))

    # Gather events from Prometheus and verdict store (+ optional trace evidence)
    async def _gather():
        import httpx

        events: list[SitRepEvent] = []
        trace_evidence = None

        async with httpx.AsyncClient() as client:
            # 1. Prometheus alerts on affected services
            alerts = await fetch_alerts(client, prometheus_url, affected)
            events.extend(alerts)

            # 2. Prometheus metric breaches on affected services
            breaches = await fetch_metric_breaches(client, prometheus_url, affected)
            events.extend(breaches)

        # 3. Recent evaluation verdicts from store as events
        recent = verdict_store.query(VerdictFilter(
            producer_system="nthlayer-measure",
            subject_type="evaluation",
            limit=50,
        ))
        for v in recent:
            svc = v.subject.ref or v.subject.service or ""
            if svc in affected:
                events.append(verdict_to_event(v))

        # 4. Trace evidence (optional, graceful degradation)
        if trace_backend is not None:
            try:
                baseline_td = _parse_duration(trace_baseline_window)
                end = datetime.now(tz=timezone.utc)
                start = end - timedelta(minutes=30)
                trace_evidence = await trace_backend.get_trace_evidence(
                    services=sorted(affected),
                    start=start,
                    end=end,
                    baseline_window=baseline_td,
                )
            except Exception as exc:
                log.warning("trace_evidence_unavailable", error=str(exc))

        return events, trace_evidence

    events, trace_evidence = asyncio.run(_gather())

    # Close trace backend client to release connections
    if trace_backend is not None and hasattr(trace_backend, "aclose"):
        asyncio.run(trace_backend.aclose())

    log.info("Gathered events", count=len(events))

    # Compute topology divergence (declared vs observed) for reasoning enrichment
    if trace_evidence and dep_graph:
        from nthlayer_workers.correlate.traces.topology import detect_topology_divergence
        trace_evidence.topology_divergence = detect_topology_divergence(trace_evidence, dep_graph)

    if not events:
        log.info("No correlated events found, no correlation verdict needed")
        return 0

    # Insert events into temp store and run correlation engine
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_store = SQLiteEventStore(os.path.join(tmp_dir, "correlation.db"))
        tmp_store.insert_batch(events)

        engine = CorrelationEngine()
        topology = dep_graph if dep_graph else None
        groups = engine.correlate(tmp_store, window_minutes=30, topology=topology)
        tmp_store.close()

    if not groups:
        log.info("No correlation groups formed")
        return 0

    log.info("Correlation groups", count=len(groups))
    for g in groups:
        log.info(
            "Group",
            priority=g.priority,
            services=g.services,
            event_count=g.event_count,
            summary=g.summary,
        )

    # Reasoning layer: model-based causal analysis if enabled, else heuristic
    reasoning_result = None
    reasoning_mode = "heuristic"

    if reasoning:
        from nthlayer_workers.correlate.reasoning import reason_about_correlations, reasoning_available

        if reasoning_available():
            kwargs = {}
            if reasoning_model:
                kwargs["model"] = reasoning_model
            if trace_evidence:
                kwargs["trace_evidence"] = trace_evidence
            reasoning_result = asyncio.run(
                reason_about_correlations(groups, dep_graph, **kwargs)
            )
            if not reasoning_result.get("degraded", False):
                reasoning_mode = "model"
                log.info("Reasoning complete", mode=reasoning_mode,
                         overall_confidence=reasoning_result.get("overall_confidence"))
            else:
                log.info("Reasoning degraded, falling back to heuristic",
                         reason=reasoning_result.get("overall_assessment"))
                reasoning_result = None
        else:
            log.info("Reasoning skipped: no LLM API key set (ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    else:
        log.info("Reasoning disabled via --no-reasoning")

    # Build root causes and confidence from reasoning or heuristic
    reasoning_by_group = {}
    if reasoning_result and reasoning_mode == "model":
        for ga in reasoning_result.get("groups", []):
            reasoning_by_group[ga["group_id"]] = ga

    root_causes = []
    for g in groups:
        ga = reasoning_by_group.get(g.id)
        if ga and ga.get("root_cause"):
            # Model-provided root cause
            root_causes.append({
                "service": g.services[0] if g.services else trigger_service,
                "type": ga["root_cause"],
                "confidence": ga["confidence"],
                "evidence": ga.get("reasoning", ""),
                "recommended_actions": ga.get("recommended_actions", []),
            })
        else:
            # Heuristic fallback: temporal proximity
            for cc in g.change_candidates:
                root_causes.append({
                    "service": cc.change.service,
                    "type": cc.change.payload.get("change_type", "unknown"),
                    "confidence": _proximity_confidence(cc.temporal_proximity_seconds),
                    "evidence": cc.change.payload.get("detail", ""),
                })

    blast_list = [
        {
            "service": svc,
            "impact": "direct" if svc == trigger_service else "downstream",
            "slo_breached": any(
                getattr(e, "service", "") == svc
                for e in events
                if getattr(e, "type", None) == EventType.METRIC_BREACH
            ),
        }
        for svc in sorted(affected)
    ]

    # Overall confidence: model reasoning or peak severity heuristic
    if reasoning_result and reasoning_mode == "model":
        overall_confidence = reasoning_result["overall_confidence"]
    else:
        overall_confidence = min(1.0, max(
            max(s.peak_severity for s in g.signals) for g in groups
        )) if groups else 0.5

    # Build verdict summary: prefer model assessment over template
    if reasoning_result and reasoning_mode == "model" and reasoning_result.get("overall_assessment"):
        verdict_summary = reasoning_result["overall_assessment"]
    elif root_causes:
        verdict_summary = (
            f"{root_causes[0].get('service', trigger_service)} "
            f"{root_causes[0].get('type', 'incident')} — "
            f"{len(blast_list)} services in blast radius"
        )
    else:
        verdict_summary = f"{trigger_service} incident — {len(blast_list)} services affected"

    # opensrm-saun.1.2.1: emit a correlation_snapshot ASSESSMENT (the v1.5
    # canonical primitive). correlation_snapshot is in ASSESSMENT_KINDS;
    # producing a "correlation"-typed verdict here was a category error.
    import uuid as _uuid
    from nthlayer_workers.observe.assessment import Assessment as _Assessment
    from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore

    corr_assessment_id = f"csn-{trigger_service}-{_uuid.uuid4().hex[:8]}"
    corr_assessment_data = {
        "summary": verdict_summary,
        "action": "flag" if any(g.priority <= 1 for g in groups) else "escalate",
        "confidence": overall_confidence,
        # parent_ids: lineage anchor to the trigger verdict (replaces the
        # legacy verdict_link(context=[...]) pattern).
        "parent_ids": [trigger_verdict_id],
        "trigger_verdict": trigger_verdict_id,
        "root_causes": root_causes,
        "blast_radius": blast_list,
        "groups": len(groups),
        "events_gathered": len(events),
        "reasoning_mode": reasoning_mode,
        "reasoning": reasoning_result if reasoning_mode == "model" else None,
        "evidence_sources": {
            "prometheus": True,
            "verdict_store": True,
            "trace_backend": trace_evidence.backend if trace_evidence else None,
        },
        "trace_query_time_ms": trace_evidence.query_time_ms if trace_evidence else None,
    }
    corr_assessment = _Assessment(
        id=corr_assessment_id,
        created_at=datetime.now(timezone.utc),
        kind="correlation_snapshot",
        service=trigger_service,
        producer="nthlayer-correlate",
        data=corr_assessment_data,
    )
    # Persist to a SQLite assessment store sharing the verdict store's
    # database file (assessments and verdicts can coexist per
    # SQLiteAssessmentStore's docstring).
    asm_store = SQLiteAssessmentStore(verdict_store_path)
    asm_store.put(corr_assessment)

    # Legacy CLI side-effect (opensrm-saun.1.2.1).
    # The correlate worker owns production side-effects (decision records,
    # notifications, downstream incident triggering). This CLI command's
    # integrations are no-ops on the assessment path pending assessment-shaped
    # overloads in nthlayer-common.records and the Slack block builder.
    # See docs/superpowers/decisions/legacy-cli-maintenance-mode.md
    if decision_store_path:
        log.info(
            "decision_record_write_skipped_for_assessment",
            assessment_id=corr_assessment_id,
            note="opensrm-saun.1.2.1: write_decision_assessment helper not yet available",
        )

    # Slack notification path used Verdict-attribute semantics
    # (corr_verdict.metadata.custom, .subject.ref, .judgment.confidence).
    # Skipping on the assessment path; the hot-path worker emits Slack via
    # a different channel. Tracked as legacy CLI cleanup.
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if slack_url:
        log.info(
            "slack_notification_skipped_for_assessment",
            assessment_id=corr_assessment_id,
            note="opensrm-saun.1.2.1: legacy Slack flow assumed Verdict semantics",
        )

    print(f"Correlation snapshot assessment: {corr_assessment_id}")
    print(f"  Groups: {len(groups)}, Events: {len(events)}, Blast radius: {len(affected)} services")

    # Forward to nthlayer-respond if respond_args is set
    if respond_args:
        import json
        import subprocess

        try:
            args_dict = json.loads(respond_args)
        except json.JSONDecodeError:
            log.error("Invalid --respond-args JSON", raw=respond_args)
            return 1

        # Only allow known respond flags to prevent injection
        allowed_keys = {"specs-dir", "config", "notify"}
        for key in args_dict:
            if key not in allowed_keys:
                log.warning("Ignoring unknown respond arg", key=key)

        cmd = [
            "nthlayer-respond", "respond",
            "--trigger-verdict", corr_assessment_id,
            "--verdict-store", verdict_store_path,
        ]
        for key, value in args_dict.items():
            if key in allowed_keys:
                cmd.extend([f"--{key}", str(value)])

        log.info("Invoking nthlayer-respond", cmd=cmd)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log.error("nthlayer-respond failed", returncode=result.returncode, stderr=result.stderr)
            else:
                print(result.stdout)
        except FileNotFoundError:
            log.error("nthlayer-respond not found on PATH")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="nthlayer-correlate",
        description="SitRep — Situational awareness through automated signal correlation",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the full SitRep pipeline")
    serve_parser.add_argument(
        "--config", default=None, help="Path to sitrep.yaml config file"
    )

    # status
    status_parser = subparsers.add_parser("status", help="Show current SitRep status")
    status_parser.add_argument(
        "--config", default=None, help="Path to sitrep.yaml config file"
    )

    # replay
    replay_parser = subparsers.add_parser("replay", help="Replay a scenario fixture")
    replay_parser.add_argument(
        "--scenario", required=True, help="Path to scenario YAML file"
    )
    replay_parser.add_argument(
        "--config", default=None, help="Path to sitrep.yaml config file"
    )
    replay_parser.add_argument(
        "--no-model", action="store_true", help="Skip model calls"
    )

    # correlate (live data — trigger from verdict)
    corr_parser = subparsers.add_parser("correlate", help="Correlate signals from a trigger verdict")
    corr_parser.add_argument("--trigger-verdict", required=True, help="Evaluation verdict ID that triggered correlation")
    corr_parser.add_argument("--prometheus-url", required=True, help="Prometheus base URL")
    corr_parser.add_argument("--specs-dir", required=True, help="Directory of OpenSRM spec YAMLs")
    corr_parser.add_argument("--verdict-store", default="verdicts.db", help="Path to verdict SQLite DB")
    # Reasoning layer flags
    reasoning_group = corr_parser.add_mutually_exclusive_group()
    reasoning_group.add_argument("--reasoning", action="store_true", default=True, help="Enable model-based causal reasoning (default if API key set)")
    reasoning_group.add_argument("--no-reasoning", action="store_true", help="Disable model reasoning, use heuristic only")
    corr_parser.add_argument("--model", default=None, help="Model for reasoning (e.g. 'openai/gpt-4o', 'anthropic/claude-sonnet-4-20250514')")
    # Forward flags for downstream components (passed through, not parsed by correlate)
    corr_parser.add_argument("--respond-args", default=None, help="JSON-encoded args to forward to nthlayer-respond")
    corr_parser.add_argument("--decision-store", default=None, help="Path to decision record SQLite DB for content-addressed records")
    # Trace backend flags
    corr_parser.add_argument("--trace-backend", default=None, choices=["tempo"], help="Trace backend to query for evidence (e.g. 'tempo')")
    corr_parser.add_argument("--tempo-endpoint", default=None, help="Tempo query endpoint (e.g. 'http://tempo:3200')")
    corr_parser.add_argument("--trace-detail", default="full", choices=["summary", "full"], help="Trace evidence detail level")

    return parser


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(2)

    if args.command == "serve":
        sys.exit(serve_command(args.config))
    elif args.command == "status":
        sys.exit(status_command(args.config))
    elif args.command == "replay":
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys.exit(
                replay_command(
                    scenario_path=args.scenario,
                    config_path=args.config,
                    no_model=args.no_model,
                    store_dir=tmp_dir,
                )
            )
    elif args.command == "correlate":
        # Construct trace backend if requested
        _trace_backend = None
        if args.trace_backend == "tempo":
            from nthlayer_workers.correlate.traces.tempo import TempoTraceBackend
            _trace_backend = TempoTraceBackend(
                endpoint=args.tempo_endpoint,
                use_service_graphs=True,
            )

        sys.exit(
            correlate_command(
                trigger_verdict_id=args.trigger_verdict,
                prometheus_url=args.prometheus_url,
                specs_dir=args.specs_dir,
                verdict_store_path=args.verdict_store,
                respond_args=args.respond_args,
                reasoning=not args.no_reasoning,
                reasoning_model=args.model,
                decision_store_path=getattr(args, "decision_store", None),
                trace_backend=_trace_backend,
            )
        )


if __name__ == "__main__":
    main()
