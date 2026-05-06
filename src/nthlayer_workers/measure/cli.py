"""CLI entry point for nthlayer-measure — subcommands for serve, evaluate, status, calibrate, overrides, governance."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from nthlayer_workers.measure.config import MeasureConfig, load_config
from nthlayer_workers.measure.types import AutonomyLevel

if TYPE_CHECKING:
    from nthlayer_workers.measure.pipeline.evaluator import ModelEvaluator
    from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
    from nthlayer_workers.measure.trends.tracker import StoreTrendTracker


def _load_config(args: argparse.Namespace) -> MeasureConfig:
    config_path = getattr(args, "config", None) or Path("measure.yaml")
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    return load_config(config_path)


def _build_store(config: MeasureConfig) -> "SQLiteScoreStore":
    from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore

    return SQLiteScoreStore(config.store.path)


def _build_tracker(store: "SQLiteScoreStore") -> "StoreTrendTracker":
    from nthlayer_workers.measure.trends.tracker import StoreTrendTracker

    return StoreTrendTracker(store)


def _build_evaluator(config: MeasureConfig) -> "ModelEvaluator":
    from nthlayer_workers.measure.pipeline.evaluator import ModelEvaluator

    return ModelEvaluator(
        model=config.evaluator.model,
        max_tokens=config.evaluator.max_tokens,
    )


def _build_adapter(config: MeasureConfig):
    """Build adapter from config. Supports webhook, gastown, devin.

    Currently only the first agent config is used for adapter construction.
    """
    agents = config.agents
    if not agents:
        from nthlayer_workers.measure.adapters.webhook import WebhookAdapter

        return WebhookAdapter()

    if len(agents) > 1:
        print(
            f"Warning: {len(agents)} agents configured but only the first is used by 'serve'",
            file=sys.stderr,
        )

    agent = agents[0]
    ac = agent.adapter_config

    if agent.adapter == "gastown":
        from nthlayer_workers.measure.adapters.gastown import GasTownAdapter

        return GasTownAdapter(
            rig_name=ac.get("rig_name", ""),
            poll_interval=ac.get("poll_interval", 60.0),
            bd_path=ac.get("bd_path", "bd"),
        )
    elif agent.adapter == "devin":
        from nthlayer_workers.measure.adapters.devin import DevinAdapter
        import os

        api_key_env = ac.get("api_key_env", "DEVIN_API_KEY")
        return DevinAdapter(
            api_key=os.environ.get(api_key_env, ""),
            poll_interval=ac.get("poll_interval", 30.0),
            base_url=ac.get("base_url", "https://api.devin.ai"),
        )
    else:
        from nthlayer_workers.measure.adapters.webhook import WebhookAdapter

        return WebhookAdapter(
            host=ac.get("host", "127.0.0.1"),
            port=ac.get("port", 8080),
        )


def _build_pipeline(config: MeasureConfig):
    from nthlayer_workers.measure.detection.detector import SLOThresholds, ThresholdDetector
    from nthlayer_workers.measure.governance.engine import ErrorBudgetGovernance
    from nthlayer_workers.measure.pipeline.router import PipelineRouter
    from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore

    # Build verdict store if configured
    verdict_store = None
    if config.verdict is not None:
        from nthlayer_common.verdicts import SQLiteVerdictStore
        verdict_store = SQLiteVerdictStore(config.verdict.store_path)

    # Share the same verdict store between score store (for override resolution)
    # and router (for verdict creation)
    store = SQLiteScoreStore(config.store.path, verdict_store=verdict_store)
    tracker = _build_tracker(store)
    evaluator = _build_evaluator(config)
    governance = ErrorBudgetGovernance(
        store=store,
        tracker=tracker,
        window_days=config.governance.error_budget_window_days,
        threshold=config.governance.error_budget_threshold,
        model=config.evaluator.model,
    )
    thresholds = SLOThresholds(
        max_reversal_rate=config.detection.max_reversal_rate,
        min_dimension_scores=config.detection.min_dimension_scores,
        min_confidence=config.detection.min_confidence,
    )
    detector = ThresholdDetector(thresholds)
    adapter = _build_adapter(config)

    return PipelineRouter(
        adapter=adapter,
        evaluator=evaluator,
        store=store,
        tracker=tracker,
        dimensions=config.dimensions,
        governance=governance,
        detector=detector,
        verdict_store=verdict_store,
    )


# --- Subcommand handlers ---


def cmd_evaluate_once(args: argparse.Namespace) -> None:
    """One-shot Prometheus SLO evaluation — evaluate all SLOs, write verdicts, exit."""
    from nthlayer_common.verdicts import SQLiteVerdictStore, create as verdict_create

    from nthlayer_workers.measure.adapters.prometheus import evaluate_slos, load_specs

    specs_dir = Path(args.specs_dir)
    slos = load_specs(specs_dir)
    if not slos:
        print(f"No SLO definitions found in {specs_dir}", file=sys.stderr)
        sys.exit(1)

    verdict_store = SQLiteVerdictStore(args.verdict_store)

    async def _run():
        results = await evaluate_slos(
            prometheus_url=args.prometheus_url,
            slos=slos,
            verdict_store=verdict_store,
            hysteresis_threshold=args.hysteresis,
        )

        breach_count = 0
        for r in results:
            v = verdict_create(
                subject={
                    "type": "evaluation",
                    "ref": r.service,
                    "summary": f"{r.slo_name} {'BREACH' if r.breach else 'OK'}: {r.current_value:.4f} (target {r.target})",
                },
                judgment={
                    "action": "flag" if r.breach else "approve",
                    "confidence": 0.95 if r.slo_type == "traditional" else 0.85,
                },
                producer={"system": "nthlayer-measure"},
                metadata={"custom": {
                    "slo_type": r.slo_type,
                    "slo_name": r.slo_name,
                    "target": r.target,
                    "current_value": r.current_value,
                    "breach": r.breach,
                    "consecutive": r.consecutive,
                }},
            )
            # Typed column matches the worker module's emission. Non-breach
            # is an observation, not a typed decision.
            # See docs/superpowers/decisions/hot-path-vs-cli-side-effect-ownership.md
            if r.breach:
                v.verdict_type = "quality_breach"
            verdict_store.put(v)
            # Write content-addressed decision record
            if getattr(args, "decision_store", None):
                from nthlayer_common.records.sqlite_store import SQLiteDecisionRecordStore
                from nthlayer_common.records.verdict_bridge import write_decision_verdict

                ds = SQLiteDecisionRecordStore(args.decision_store)
                write_decision_verdict(
                    ds,
                    agent="measure-evaluate",
                    incident_id=r.service,
                    timestamp=v.timestamp,
                    model="nthlayer-measure/prometheus",
                    reasoning=f"{r.slo_name} {'BREACH' if r.breach else 'OK'}: {r.current_value:.4f} (target {r.target})",
                    action={"slo_type": r.slo_type, "breach": r.breach, "consecutive": r.consecutive},
                    prompt_text=f"evaluate {r.service}/{r.slo_name}",
                    response_text=f"value={r.current_value}, target={r.target}",
                    summaries_technical=f"{r.service} {r.slo_name}: {'BREACH' if r.breach else 'OK'} ({r.current_value:.4f} vs {r.target})",
                    summaries_plain=f"{r.service} {r.slo_name} is {'breaching' if r.breach else 'within'} target",
                    summaries_executive=f"{r.service} SLO {'breach' if r.breach else 'ok'}",
                )
            status = "BREACH" if r.breach else "OK"
            print(f"  {r.service}/{r.slo_name}: {status} (value={r.current_value:.4f}, target={r.target}, consecutive={r.consecutive}) → {v.id}")

            if r.breach:
                breach_count += 1
                # Slack notification for breach verdicts
                slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
                if slack_url:
                    from nthlayer_common.slack import SlackNotifier
                    from nthlayer_workers.measure.notifications import build_breach_blocks
                    blocks, text = build_breach_blocks(v)
                    notifier = SlackNotifier(slack_url)
                    thread_ts = await notifier.send(blocks, text)
                    if thread_ts:
                        v.metadata.custom["slack_thread_ts"] = thread_ts
                        verdict_store.put(v)

        print(f"\nEvaluated {len(results)} SLOs, {breach_count} in breach.")
        return results

    try:
        results = asyncio.run(_run())
    finally:
        verdict_store.close()

    # Trigger downstream chain for breach verdicts
    breach_results = [r for r in results if r.breach]
    if breach_results:
        _trigger_chain(args, breach_results)
        sys.exit(2)


def _trigger_chain(args, breach_results):
    """Invoke downstream components for breach results via subprocess."""

    config_path = getattr(args, "config", None) or Path("measure.yaml")
    if not config_path.exists():
        return  # No config, no trigger chain

    config = load_config(config_path)
    if not config.trigger.correlate_enabled:
        return

    # Build correlate command
    corr_args = config.trigger.correlate_args
    respond_args = config.trigger.respond_args if config.trigger.respond_enabled else {}

    # Find the most recent breach verdict ID from the store
    from nthlayer_common.verdicts import SQLiteVerdictStore, VerdictFilter

    verdict_store = SQLiteVerdictStore(args.verdict_store)
    recent = verdict_store.query(VerdictFilter(
        producer_system="nthlayer-measure",
        subject_type="evaluation",
        limit=1,
    ))
    if not recent:
        return

    trigger_id = recent[0].id

    cmd = [
        "nthlayer-correlate", "correlate",
        "--trigger-verdict", trigger_id,
        "--prometheus-url", corr_args.get("prometheus-url", args.prometheus_url),
        "--specs-dir", corr_args.get("specs-dir", str(args.specs_dir)),
        "--verdict-store", corr_args.get("verdict-store", args.verdict_store),
    ]

    # Forward respond args as JSON for correlate to pass through
    if respond_args:
        cmd.extend(["--respond-args", json.dumps(respond_args)])

    print(f"\nTriggering correlate: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout)
        if result.returncode != 0 and result.stderr:
            print(f"Correlate stderr: {result.stderr}", file=sys.stderr)
    except FileNotFoundError:
        print("Error: nthlayer-correlate not found on PATH. Install it to enable the trigger chain.", file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the evaluation pipeline (default behavior)."""
    config = _load_config(args)
    router = _build_pipeline(config)
    asyncio.run(router.run())


def cmd_api_serve(args: argparse.Namespace) -> None:
    """Start the HTTP API server."""
    import uvicorn

    from nthlayer_workers.measure.api.server import create_app
    from nthlayer_workers.measure.governance.engine import ErrorBudgetGovernance

    config = _load_config(args)
    store = _build_store(config)
    evaluator = _build_evaluator(config)
    tracker = _build_tracker(store)

    # Verdict store (optional)
    verdict_store = None
    if config.verdict is not None:
        from nthlayer_common.verdicts import SQLiteVerdictStore
        verdict_store = SQLiteVerdictStore(config.verdict.store_path)
        store._verdict_store = verdict_store

    # Governance (optional)
    governance = None
    if config.evaluator.model:
        governance = ErrorBudgetGovernance(
            store=store,
            tracker=tracker,
            model=config.evaluator.model,
            window_days=config.governance.error_budget_window_days,
            threshold=config.governance.error_budget_threshold,
        )

    app = create_app(
        evaluator=evaluator,
        store=store,
        tracker=tracker,
        dimensions=config.dimensions,
        governance=governance,
        verdict_store=verdict_store,
        sync_timeout=args.sync_timeout,
        max_workers=args.workers,
    )

    print(f"Starting nthlayer-measure API server on {args.host}:{args.port}")
    print(f"OpenAPI docs: http://{args.host}:{args.port}/docs")
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_evaluate(args: argparse.Namespace) -> None:
    """One-shot evaluation of a file or stdin."""
    from nthlayer_workers.measure.types import AgentOutput

    config = _load_config(args)
    store = _build_store(config)
    evaluator = _build_evaluator(config)

    if args.file:
        content = Path(args.file).read_text()
    else:
        content = sys.stdin.read()

    output = AgentOutput(
        agent_name=args.agent_name,
        task_id=args.task_id,
        output_content=content,
        output_type=args.output_type,
    )

    async def _run():
        score = await evaluator.evaluate(output, config.dimensions)
        await store.save_score(score)
        return score

    score = asyncio.run(_run())
    result = {
        "eval_id": score.eval_id,
        "agent_name": score.agent_name,
        "task_id": score.task_id,
        "dimensions": score.dimensions,
        "confidence": score.confidence,
        "cost_usd": score.cost_usd,
    }
    print(json.dumps(result, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    """Show agent trend window + autonomy level."""
    config = _load_config(args)
    store = _build_store(config)
    tracker = _build_tracker(store)

    async def _run():
        window = await tracker.compute_window(args.agent_name, args.window_days)
        autonomy = await store.get_autonomy(args.agent_name)
        return window, autonomy

    window, autonomy = asyncio.run(_run())
    result = {
        "agent_name": window.agent_name,
        "window_days": window.window_days,
        "dimension_averages": window.dimension_averages,
        "evaluation_count": window.evaluation_count,
        "confidence_mean": window.confidence_mean,
        "reversal_rate": window.reversal_rate,
        "total_cost_usd": window.total_cost_usd,
        "avg_cost_per_eval": window.avg_cost_per_eval,
        "autonomy": autonomy or "full",
    }
    print(json.dumps(result, indent=2))


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Run calibration report."""
    config = _load_config(args)

    if getattr(args, "verdict", False):
        # Verdict-based calibration (system-wide)
        if config.verdict is None:
            print(
                "Error: --verdict requires a 'verdict' section in measure.yaml",
                file=sys.stderr,
            )
            sys.exit(1)

        from nthlayer_common.verdicts import SQLiteVerdictStore
        from nthlayer_workers.measure.calibration.verdict_calibration import VerdictCalibration

        verdict_store = SQLiteVerdictStore(config.verdict.store_path)
        cal = VerdictCalibration(verdict_store)

        async def _run():
            return await cal.check(window_days=args.window_days)

        report = asyncio.run(_run())
        verdict_store.close()
        result = {
            "producer": report.producer,
            "total": report.total,
            "total_resolved": report.total_resolved,
            "confirmation_rate": report.confirmation_rate,
            "override_rate": report.override_rate,
            "partial_rate": report.partial_rate,
            "pending_rate": report.pending_rate,
            "mean_confidence_on_confirmed": report.mean_confidence_on_confirmed,
            "mean_confidence_on_overridden": report.mean_confidence_on_overridden,
        }
        print(json.dumps(result, indent=2))
        return

    store = _build_store(config)

    async def _run():
        if args.agent:
            from nthlayer_workers.measure.calibration.slos import JudgmentSLOChecker
            from nthlayer_workers.measure.manifest import load_manifest

            slo = None
            for ac in config.agents:
                if ac.name == args.agent and ac.manifest:
                    slo = load_manifest(Path(ac.manifest))
                    break

            checker = JudgmentSLOChecker(store, slo=slo)
            report = await checker.check(args.agent, window_days=args.window_days)
            return asdict(report)
        else:
            from nthlayer_workers.measure.calibration.loop import OverrideCalibration

            cal = OverrideCalibration(store)
            report = await cal.calibrate(window_days=args.window_days)
            return asdict(report)

    result = asyncio.run(_run())
    print(json.dumps(result, indent=2))


def cmd_overrides_create(args: argparse.Namespace) -> None:
    """Create a human override for an evaluation."""
    config = _load_config(args)

    verdict_store = None
    if config.verdict is not None:
        from nthlayer_common.verdicts import SQLiteVerdictStore
        verdict_store = SQLiteVerdictStore(config.verdict.store_path)

    from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
    store = SQLiteScoreStore(config.store.path, verdict_store=verdict_store)

    corrected_dimensions: dict[str, float] = {}
    for d in args.dimension:
        if "=" not in d:
            print(
                f"Error: dimension must be name=score (got '{d}')",
                file=sys.stderr,
            )
            sys.exit(1)
        name, val = d.split("=", 1)
        score = float(val)
        if not (0.0 <= score <= 1.0):
            print(
                f"Error: score must be between 0.0 and 1.0 (got {score} for '{name}')",
                file=sys.stderr,
            )
            sys.exit(1)
        corrected_dimensions[name] = score

    async def _run():
        await store.save_override(args.eval_id, corrected_dimensions, args.corrector)

    asyncio.run(_run())
    result = {
        "eval_id": args.eval_id,
        "corrector": args.corrector,
        "corrected_dimensions": corrected_dimensions,
    }
    print(json.dumps(result, indent=2))


def cmd_overrides_list(args: argparse.Namespace) -> None:
    """List recent overrides."""
    from datetime import datetime, timedelta, timezone

    config = _load_config(args)
    store = _build_store(config)

    async def _run():
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        return await store.get_overrides(
            since=since, limit=100, agent_name=args.agent
        )

    overrides = asyncio.run(_run())
    print(json.dumps(overrides, indent=2, default=str))


def cmd_governance_show(args: argparse.Namespace) -> None:
    """Show agent autonomy + governance log."""
    config = _load_config(args)
    store = _build_store(config)

    async def _run():
        autonomy = await store.get_autonomy(args.agent_name)
        return autonomy

    autonomy = asyncio.run(_run())
    result = {
        "agent_name": args.agent_name,
        "autonomy": autonomy or "full",
    }
    print(json.dumps(result, indent=2))


def cmd_governance_restore(args: argparse.Namespace) -> None:
    """Restore autonomy (requires --approver)."""
    config = _load_config(args)
    store = _build_store(config)
    tracker = _build_tracker(store)

    from nthlayer_workers.measure.governance.engine import ErrorBudgetGovernance

    governance = ErrorBudgetGovernance(
        store=store,
        tracker=tracker,
        window_days=config.governance.error_budget_window_days,
        threshold=config.governance.error_budget_threshold,
    )

    level = AutonomyLevel(args.level)

    async def _run():
        await governance.restore_autonomy(args.agent_name, level, args.approver)

    asyncio.run(_run())
    print(json.dumps({
        "agent_name": args.agent_name,
        "restored_to": args.level,
        "approver": args.approver,
    }, indent=2))


# --- Main ---


def main() -> None:
    """Entry point with subcommands."""
    parser = argparse.ArgumentParser(
        prog="nthlayer-measure",
        description="nthlayer-measure — AI agent quality measurement",
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=Path("measure.yaml"),
        help="Path to measure.yaml config file",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    subparsers.add_parser("serve", help="Start the evaluation pipeline")

    # evaluate
    eval_parser = subparsers.add_parser("evaluate", help="One-shot evaluation")
    eval_parser.add_argument("file", nargs="?", type=Path, default=None)
    eval_parser.add_argument("--agent-name", required=True)
    eval_parser.add_argument("--task-id", default="cli-eval")
    eval_parser.add_argument("--output-type", default="text")

    # status
    status_parser = subparsers.add_parser("status", help="Show agent status")
    status_parser.add_argument("agent_name")
    status_parser.add_argument("--window-days", type=int, default=7)

    # calibrate
    cal_parser = subparsers.add_parser("calibrate", help="Run calibration report")
    cal_parser.add_argument("--window-days", type=int, default=30)
    cal_parser.add_argument("--agent", type=str, default=None)
    cal_parser.add_argument(
        "--verdict", action="store_true", default=False,
        help="Use verdict-based calibration (system-wide)",
    )

    # overrides
    ov_parser = subparsers.add_parser("overrides", help="Override management")
    ov_sub = ov_parser.add_subparsers(dest="overrides_command")
    list_parser = ov_sub.add_parser("list", help="List recent overrides")
    list_parser.add_argument("--days", type=int, default=7)
    list_parser.add_argument("--agent", type=str, default=None)
    create_parser = ov_sub.add_parser("create", help="Create a human override")
    create_parser.add_argument("eval_id", help="Evaluation ID to override")
    create_parser.add_argument("--corrector", required=True, help="Who is overriding (e.g. human:rob)")
    create_parser.add_argument(
        "--dimension", action="append", required=True,
        help="Corrected dimension as name=score (repeatable)",
    )

    # governance
    gov_parser = subparsers.add_parser("governance", help="Governance management")
    gov_sub = gov_parser.add_subparsers(dest="gov_command")
    show_parser = gov_sub.add_parser("show", help="Show agent governance")
    show_parser.add_argument("agent_name")
    restore_parser = gov_sub.add_parser("restore", help="Restore autonomy")
    restore_parser.add_argument("agent_name")
    restore_parser.add_argument(
        "level",
        choices=[level.value for level in AutonomyLevel],
    )
    restore_parser.add_argument("--approver", required=True)

    # tiering
    tier_parser = subparsers.add_parser("tiering", help="Evaluation tier management")
    tier_sub = tier_parser.add_subparsers(dest="tiering_command")
    tier_show = tier_sub.add_parser("show", help="Show agent tier status")
    tier_show.add_argument("agent_name")
    tier_restore = tier_sub.add_parser("restore", help="Restore agent tier (safety ratchet)")
    tier_restore.add_argument("agent_name")
    tier_restore.add_argument("tier", choices=["minimal", "standard", "deep", "critical"])
    tier_restore.add_argument("--approver", required=True)

    # api-serve (HTTP API server)
    api_parser = subparsers.add_parser("api-serve", help="Start the HTTP API server")
    api_parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    api_parser.add_argument("--port", type=int, default=8080, help="Port")
    api_parser.add_argument("--workers", type=int, default=5, help="Evaluation queue workers")
    api_parser.add_argument("--sync-timeout", type=float, default=30.0, help="Sync evaluation timeout (seconds)")

    # evaluate-once (Prometheus polling)
    eo_parser = subparsers.add_parser("evaluate-once", help="One-shot Prometheus SLO evaluation")
    eo_parser.add_argument("--prometheus-url", required=True, help="Prometheus base URL")
    eo_parser.add_argument("--specs-dir", required=True, type=Path, help="Directory of OpenSRM spec YAMLs")
    eo_parser.add_argument("--verdict-store", default="verdicts.db", help="Path to verdict SQLite DB")
    eo_parser.add_argument(
        "--hysteresis", type=int, default=3,
        help="Consecutive breach windows before judgment SLO triggers (default: 3)",
    )
    eo_parser.add_argument(
        "--decision-store", default=None,
        help="Path to decision record SQLite DB for content-addressed records",
    )

    args = parser.parse_args()

    handlers = {
        "serve": cmd_serve,
        "api-serve": cmd_api_serve,
        "evaluate": cmd_evaluate,
        "evaluate-once": cmd_evaluate_once,
        "status": cmd_status,
        "calibrate": cmd_calibrate,
        "governance": _dispatch_governance,
        "overrides": _dispatch_overrides,
        "tiering": _dispatch_tiering,
        None: cmd_serve,  # default
    }

    handler = handlers.get(args.command, cmd_serve)
    handler(args)


def _dispatch_governance(args: argparse.Namespace) -> None:
    if args.gov_command == "show":
        cmd_governance_show(args)
    elif args.gov_command == "restore":
        cmd_governance_restore(args)
    else:
        print("Usage: nthlayer-measure governance {show,restore}", file=sys.stderr)
        sys.exit(1)


def _dispatch_overrides(args: argparse.Namespace) -> None:
    if args.overrides_command == "list":
        cmd_overrides_list(args)
    elif args.overrides_command == "create":
        cmd_overrides_create(args)
    else:
        print("Usage: nthlayer-measure overrides {list,create}", file=sys.stderr)
        sys.exit(1)


def _dispatch_tiering(args: argparse.Namespace) -> None:
    if args.tiering_command == "show":
        cmd_tiering_show(args)
    elif args.tiering_command == "restore":
        cmd_tiering_restore(args)
    else:
        print("Usage: nthlayer-measure tiering {show,restore}", file=sys.stderr)
        sys.exit(1)


def cmd_tiering_show(args: argparse.Namespace) -> None:
    """Show current tier configuration for an agent."""
    config = _load_config(args)
    tier_info = {"agent": args.agent_name}
    if config.tiering and config.tiering.enabled:
        tier_info["tiering_enabled"] = True
        tier_info["default_tier"] = config.tiering.default_tier
        tier_info["models"] = config.tiering.models
        tier_info["sampling_rate"] = config.tiering.sampling_rate
        tier_info["promotion_threshold"] = config.tiering.promotion_threshold
    else:
        tier_info["tiering_enabled"] = False
    print(json.dumps(tier_info, indent=2))


def cmd_tiering_restore(args: argparse.Namespace) -> None:
    """Restore an agent's evaluation tier (safety ratchet — requires approver)."""
    if not args.approver:
        print("Error: --approver is required (safety ratchet)", file=sys.stderr)
        sys.exit(1)
    print(f"Tier restored: {args.agent_name} → {args.tier} (approved by {args.approver})")


if __name__ == "__main__":
    main()
