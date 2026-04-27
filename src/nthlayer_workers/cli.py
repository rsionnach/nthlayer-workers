"""NthLayer workers CLI — run all worker modules."""

import argparse
import asyncio
import sys

from nthlayer_workers.runner import ModuleRunner


def main():
    parser = argparse.ArgumentParser(description="NthLayer workers")
    parser.add_argument("-V", "--version", action="version", version="%(prog)s 1.5.0a1")
    sub = parser.add_subparsers(dest="command")

    # -- serve: start all worker modules --
    serve_parser = sub.add_parser("serve", help="Start the workers process (all modules)")
    serve_parser.add_argument("--core-url", default="http://localhost:8000",
                              help="Core API URL (default: http://localhost:8000)")
    serve_parser.add_argument("--instance-id", default="workers-1",
                              help="Instance ID for heartbeats (default: workers-1)")
    serve_parser.add_argument("--prometheus-url", default="http://localhost:9090",
                              help="Prometheus URL (default: http://localhost:9090)")
    serve_parser.add_argument("--collect-interval", type=int, default=60,
                              help="SLO collection + portfolio cycle interval in seconds (default: 60)")
    serve_parser.add_argument("--drift-interval", type=int, default=1800,
                              help="Drift analysis cycle interval in seconds (default: 1800)")
    serve_parser.add_argument("--topology-interval", type=int, default=86400,
                              help="Topology/blast-radius cycle interval in seconds (default: 86400)")
    serve_parser.add_argument("--correlate-interval", type=int, default=10,
                              help="Correlate session window cycle interval in seconds (default: 10)")
    serve_parser.add_argument("--topology-drift-interval", type=int, default=3600,
                              help="Correlate topology drift cycle interval in seconds (default: 3600)")
    serve_parser.add_argument("--contract-interval", type=int, default=3600,
                              help="Correlate contract divergence cycle interval in seconds (default: 3600)")
    serve_parser.add_argument("--tempo-endpoint", default=None,
                              help="Tempo endpoint URL (optional — topology module is no-op if not set)")
    serve_parser.add_argument("--measure-interval", type=int, default=60,
                              help="Measure evaluation cycle interval in seconds (default: 60)")
    serve_parser.add_argument("--outcome-interval", type=int, default=60,
                              help="Learn outcome resolution cycle interval in seconds (default: 60)")
    serve_parser.add_argument("--retrospective-interval", type=int, default=30,
                              help="Learn retrospective cycle interval in seconds (default: 30)")
    serve_parser.add_argument("--expiry-threshold-days", type=int, default=7,
                              help="Verdict expiry threshold in days (default: 7)")
    serve_parser.add_argument("--min-resolution-age-hours", type=int, default=1,
                              help="Minimum verdict age before resolution attempts in hours (default: 1)")
    serve_parser.add_argument("--respond-interval", type=int, default=30,
                              help="Respond worker cycle interval in seconds (default: 30)")

    # -- gate: deploy gate evaluation (CLI-only, not a worker module) --
    gate_parser = sub.add_parser("gate", help="Evaluate deployment gate for a service")
    gate_parser.add_argument("--service", required=True, help="Service name")
    gate_parser.add_argument("--tier", default=None,
                             help="Service tier (default: resolved from manifest)")
    gate_parser.add_argument("--commit-sha", default=None, help="Commit SHA (informational)")
    gate_parser.add_argument("--core-url", default="http://localhost:8000",
                             help="Core API URL (default: http://localhost:8000)")

    args = parser.parse_args()

    if args.command == "serve":
        from nthlayer_common.api_client import CoreAPIClient
        from nthlayer_workers.observe.worker import (
            ObserveCollectModule,
            ObserveDriftModule,
            ObserveTopologyModule,
        )

        client = CoreAPIClient(base_url=args.core_url)
        runner = ModuleRunner(
            core_url=args.core_url,
            instance_id=args.instance_id,
            client=client,
        )

        collect = ObserveCollectModule(client=client, prometheus_url=args.prometheus_url)
        drift = ObserveDriftModule(client=client, prometheus_url=args.prometheus_url)
        topology = ObserveTopologyModule(client=client, prometheus_url=args.prometheus_url)

        runner.register(collect, interval_seconds=args.collect_interval)
        runner.register(drift, interval_seconds=args.drift_interval)
        runner.register(topology, interval_seconds=args.topology_interval)

        # P3-D: Correlate modules — session windows, topology drift, contract divergence
        from nthlayer_workers.correlate.worker import (
            CorrelateContractModule,
            CorrelateSessionModule,
            CorrelateTopologyModule,
        )

        trace_backend = None
        if args.tempo_endpoint:
            from nthlayer_workers.correlate.traces.tempo import TempoTraceBackend
            trace_backend = TempoTraceBackend(endpoint=args.tempo_endpoint)

        correlate_session = CorrelateSessionModule(client=client)
        correlate_topology = CorrelateTopologyModule(client=client, trace_backend=trace_backend)
        correlate_contract = CorrelateContractModule(client=client, prometheus_url=args.prometheus_url)

        runner.register(correlate_session, interval_seconds=args.correlate_interval)
        runner.register(correlate_topology, interval_seconds=args.topology_drift_interval)
        runner.register(correlate_contract, interval_seconds=args.contract_interval)

        # P3-F: Learn modules — outcome resolution + retrospective
        from nthlayer_workers.learn.worker import LearnOutcomeModule, LearnRetrospectiveModule

        learn_outcome = LearnOutcomeModule(
            client=client,
            expiry_threshold_days=args.expiry_threshold_days,
            minimum_resolution_age_hours=args.min_resolution_age_hours,
        )
        learn_retro = LearnRetrospectiveModule(client=client)

        runner.register(learn_outcome, interval_seconds=args.outcome_interval)
        runner.register(learn_retro, interval_seconds=args.retrospective_interval)

        # P3-C: Measure module — judgment SLO evaluation
        from nthlayer_workers.measure.worker import MeasureModule

        measure = MeasureModule(client=client, prometheus_url=args.prometheus_url)
        runner.register(measure, interval_seconds=args.measure_interval)

        # P3-E: Respond module — incident response coordinator
        from nthlayer_workers.respond.config import RespondConfig
        from nthlayer_workers.respond.worker import RespondModule

        respond_config = RespondConfig(cycle_interval_seconds=float(args.respond_interval))
        respond = RespondModule(client=client, config=respond_config)
        runner.register(respond, interval_seconds=args.respond_interval)

        asyncio.run(runner.run())

    elif args.command == "gate":
        exit_code = _run_gate(args)
        sys.exit(exit_code)

    else:
        parser.print_help()
        sys.exit(1)


def _run_gate(args: argparse.Namespace) -> int:
    """Run deploy gate evaluation.

    Exit codes:
        0 = APPROVED or WARNING (deploy allowed; WARNING text on stderr)
        1 = evaluation error (couldn't evaluate; deploy scripts decide)
        2 = BLOCKED (do not deploy)
    """
    return asyncio.run(_gate_async(args))


async def _gate_async(args: argparse.Namespace) -> int:
    """Async gate evaluation — single event loop for all core API calls."""
    import json

    from nthlayer_common.api_client import CoreAPIClient

    from nthlayer_workers.observe.assessment import Assessment, create, from_dict, to_dict
    from nthlayer_workers.observe.gate.evaluator import check_deploy
    from nthlayer_workers.observe.store import AssessmentFilter, MemoryAssessmentStore

    client = CoreAPIClient(base_url=args.core_url)

    try:
        # 1. Resolve tier from manifest if not specified
        tier = args.tier
        if tier is None:
            manifest_result = await client.get_manifest(args.service)
            if not manifest_result.ok:
                print(f"Error: could not fetch manifest for {args.service}: {manifest_result.error}", file=sys.stderr)
                return 1
            if not isinstance(manifest_result.data, dict):
                print(f"Error: malformed manifest response for {args.service}", file=sys.stderr)
                return 1
            tier = manifest_result.data.get("tier", "standard")

        # 2. Fetch SLO assessments from core into a MemoryAssessmentStore
        #    Single fetch — used by both check_deploy and parent_ids extraction.
        api_result = await client.get_assessments(service=args.service, kind="slo_status")
        if not api_result.ok:
            print(f"Error: could not fetch assessments: {api_result.error}", file=sys.stderr)
            return 1

        store = MemoryAssessmentStore()
        slo_ids: list[str] = []
        for d in (api_result.data or []):
            a = from_dict(d)
            store.put(a)
            slo_ids.append(a.id)

        # 3. Evaluate gate
        result = check_deploy(args.service, tier, store)

        # 4. Submit deploy_gate assessment to core
        assessment = create("deploy_gate", args.service, {
            "action": "deploy",
            "decision": result.result.name.lower(),
            "budget_remaining_pct": result.budget_remaining_pct,
            "warning_threshold": result.warning_threshold,
            "blocking_threshold": result.blocking_threshold,
            "slo_count": result.slo_count,
            "reasons": [result.message] + result.recommendations,
            "commit_sha": args.commit_sha,
            "parent_ids": slo_ids,
        })
        await client.submit_assessment(to_dict(assessment))

        # 5. Output result
        output = {
            "service": result.service,
            "tier": result.tier,
            "decision": result.result.name,
            "budget_remaining_pct": round(result.budget_remaining_pct, 1),
            "message": result.message,
            "recommendations": result.recommendations,
        }
        print(json.dumps(output, indent=2))

        if result.result.name == "BLOCKED":
            return 2
        if result.result.name == "WARNING":
            print(f"WARNING: {result.message}", file=sys.stderr)
        return 0

    except Exception as e:
        print(f"Error: gate evaluation failed: {e}", file=sys.stderr)
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    main()
