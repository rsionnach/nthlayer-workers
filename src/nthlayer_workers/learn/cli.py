"""Verdict CLI — query verdict stores from the command line."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nthlayer_common.verdicts.serialise import to_dict
from nthlayer_common.verdicts.sqlite_store import SQLiteVerdictStore
from nthlayer_common.verdicts.store import AccuracyFilter, VerdictFilter
from nthlayer_workers.learn._apply import apply_recommendations, format_summary
from nthlayer_workers.learn._gh import (
    PreflightError,
    PRResult,
    check_branch_available,
    check_gh_auth,
    check_gh_installed,
    check_git_repo,
    check_remote,
    create_pr_via_gh,
)
from nthlayer_workers.learn.recommendations import parse_plan_file

_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")

_VALID_INCIDENT_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_window(window: str) -> timedelta:
    match = _DURATION_RE.match(window)
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid duration '{window}'. Use format: 30d, 24h, 7d, etc."
        )
    value, unit = int(match.group(1)), match.group(2)
    return timedelta(seconds=value * _DURATION_UNITS[unit])


def _cmd_accuracy(args: argparse.Namespace) -> None:
    store = SQLiteVerdictStore(args.db)
    try:
        from_time = None
        if args.window:
            from_time = datetime.now(timezone.utc) - _parse_window(args.window)

        report = store.accuracy(AccuracyFilter(
            producer_system=args.producer,
            from_time=from_time,
        ))

        print(f"Accuracy Report: {report.producer}")
        print(f"  Total verdicts:    {report.total}")
        print(f"  Resolved:          {report.total_resolved}")
        print(f"  Confirmation rate: {report.confirmation_rate * 100:.1f}%")
        print(f"  Override rate:     {report.override_rate * 100:.1f}%")
        print(f"  Partial rate:      {report.partial_rate * 100:.1f}%")
        print(f"  Pending rate:      {report.pending_rate * 100:.1f}%")
        print(f"  Mean confidence (confirmed):  {report.mean_confidence_on_confirmed:.3f}")
        print(f"  Mean confidence (overridden): {report.mean_confidence_on_overridden:.3f}")
    finally:
        store.close()


def _cmd_list(args: argparse.Namespace) -> None:
    store = SQLiteVerdictStore(args.db)
    try:
        verdicts = store.query(VerdictFilter(
            producer_system=args.producer,
            status=args.status,
            subject_type=args.type,
            limit=args.limit,
        ))

        fmt = getattr(args, "format", "table")

        if fmt == "json":
            print(json.dumps([to_dict(v) for v in verdicts], indent=2, default=str))
            return

        if not verdicts:
            print("No verdicts found.")
            return

        for v in verdicts:
            ts = v.timestamp.strftime("%Y-%m-%d %H:%M")
            status = v.outcome.status
            conf = f"{v.judgment.confidence:.2f}"
            print(
                f"{v.id}  {ts}  {status:<12}  "
                f"conf={conf}  {v.producer.system}  {v.subject.ref}"
            )
    finally:
        store.close()


def _cmd_retrospective(args: argparse.Namespace) -> None:
    from nthlayer_workers.learn.retrospective import build_retrospective

    store = SQLiteVerdictStore(args.db)
    decision_store = None
    if getattr(args, "decision_store", None):
        from nthlayer_common.records.sqlite_store import SQLiteDecisionRecordStore
        decision_store = SQLiteDecisionRecordStore(args.decision_store)
    try:
        retro = build_retrospective(
            incident_verdict_id=args.incident_verdict,
            verdict_store=store,
            specs_dir=args.specs_dir,
            decision_store=decision_store,
        )
        custom = retro.metadata.custom or {}
        print(f"Retrospective: {retro.id}")
        print(f"  Incident:     {custom.get('incident_verdict_id')}")
        print(f"  Duration:     {custom.get('duration_minutes', 0):.1f} minutes")
        print(f"  Decisions:    {custom.get('decisions_affected', 0)} affected")
        print(f"  Verdicts:     {custom.get('verdict_count', 0)} in chain")
        blast = custom.get("blast_radius", [])
        print(f"  Blast radius: {', '.join(blast) if blast else 'unknown'}")
        recs = custom.get("recommendations", [])
        if recs:
            print("  Recommendations:")
            for r in recs:
                print(f"    - [{r.get('type')}] {r.get('detail')}")
        impact = custom.get("financial_impact")
        if impact:
            print(f"  Financial impact: ${impact.get('estimated', 0):.2f} ({impact.get('failure_mode')})")
    except KeyError as e:
        print(f"Error: {e}", file=__import__("sys").stderr)
        __import__("sys").exit(1)
    finally:
        store.close()


def _add_recommendations_subcommand(subparsers) -> None:
    """Add the recommendations subcommand (jmy.6)."""
    p = subparsers.add_parser(
        "recommendations",
        help="Inspect / apply Learn → Spec recommendations from a retrospective",
    )

    # Input source (mutually exclusive, one required)
    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--incident", help="Incident ID; fetches retrospective from core")
    input_group.add_argument("--from", dest="from_path",
                              help="Read a saved plan.yaml as input")

    # Outputs (orthogonal, optional)
    p.add_argument("--output", help="Write plan to this file before any apply")
    p.add_argument("--apply-to", dest="apply_to",
                    help="Apply plan to manifests in this specs directory")
    p.add_argument("--pr", action="store_true",
                    help="Create a GitHub PR with the manifest changes (requires --apply-to)")

    # Modifiers
    p.add_argument("--force", action="store_true",
                    help="Override drift_detected skips (drift-only; per jmy.6 § 7)")
    p.add_argument("--base", default="main",
                    help="Base branch for --pr (default: main)")
    p.add_argument("--draft", action="store_true",
                    help="Create the PR as a draft")
    p.add_argument("--specs-dir",
                    help="For --output: include preview field per recommendation")
    p.add_argument("--json", action="store_true",
                    help="Emit a single structured JSON document to stdout at end-of-run (requires --apply-to)")
    p.add_argument("--interactive", action="store_true",
                    help="Walk recommendations in a TUI (a/r/m/n/p/q). Requires --apply-to or --output.")

    selection_group = p.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--include", dest="include",
        help="Comma-separated rec ids to apply; mutually exclusive with --exclude. "
             "Requires --apply-to.",
    )
    selection_group.add_argument(
        "--exclude", dest="exclude",
        help="Comma-separated rec ids to skip; mutually exclusive with --include. "
             "Requires --apply-to.",
    )

    p.set_defaults(func=_cmd_recommendations)


def _cmd_recommendations(args: argparse.Namespace) -> None:
    """Dispatch the recommendations subcommand (jmy.6)."""
    # Validation: --pr requires --apply-to
    if args.pr and not args.apply_to:
        raise SystemExit("error: --pr requires --apply-to")
    if args.json and not args.apply_to:
        raise SystemExit("error: --json requires --apply-to")
    if (args.include or args.exclude) and not args.apply_to:
        raise SystemExit("error: --include/--exclude requires --apply-to")
    if args.interactive and not (args.apply_to or args.output):
        raise SystemExit("error: --interactive requires --apply-to or --output")
    if args.interactive and args.json:
        raise SystemExit("error: --interactive and --json are mutually exclusive")

    # Resolve input source
    if args.from_path:
        plan = parse_plan_file(Path(args.from_path))
    else:
        # --incident: fetch retrospective from core (out of scope for unit tests;
        # real implementation requires CoreAPIClient.get_retrospective_for_incident —
        # integration test G1 exercises this path explicitly).
        plan = _build_plan_from_incident(args.incident)

    # --include / --exclude: filter recommendations before apply (jmy.24)
    # NOTE: filter runs BEFORE --output so the snapshot reflects the
    # effective plan an operator actually applied (GitOps "save what I
    # ran" workflow). Pre-filter snapshots: drop the selection flags.
    if args.include or args.exclude:
        requested = (args.include or args.exclude).split(",")
        requested = [s.strip() for s in requested if s.strip()]
        plan_ids = {r.id for r in plan.recommendations}
        # Dedup missing list preserving first-seen order so a user passing
        # `--include rec-typo,rec-typo` doesn't see the same id twice.
        missing = list(dict.fromkeys(
            rid for rid in requested if rid not in plan_ids
        ))
        if missing:
            joined = ", ".join(missing)
            # Unknown ids are a data error → exit 2.
            # (Pre-flight requirement checks above use string-SystemExit
            # which exits 1.)
            print(
                f"error: --include/--exclude id(s) not found in plan: {joined}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if args.include:
            keep = set(requested)
            plan.recommendations = [r for r in plan.recommendations if r.id in keep]
        else:  # args.exclude
            drop = set(requested)
            plan.recommendations = [r for r in plan.recommendations if r.id not in drop]

    # --interactive: walk the (post-filter) plan in a TUI; user decides
    # accept/reject/modify per rec. Returns a new plan containing only
    # accepted recs (with any modifications applied). Runs BEFORE
    # --output so the snapshot reflects the operator's final selection.
    if args.interactive:
        from nthlayer_workers.learn._interactive_app import run_walkthrough
        plan = run_walkthrough(plan, specs_dir=args.apply_to)

    # --output: write plan to file (post-filter so it reflects the
    # effective plan, opensrm-jmy.24 P3 R5).
    if args.output:
        Path(args.output).write_text(plan.to_yaml())

    # --apply-to: apply plan to specs directory
    apply_result = None
    if args.apply_to:
        apply_result = apply_recommendations(
            plan, Path(args.apply_to), force=args.force,
        )
        print(format_summary(apply_result), file=sys.stderr)

    # --pr: create GitHub PR with manifest changes
    pr_result: PRResult | None = None
    if args.pr:
        pr_result = _run_pr_path(plan, args, apply_result)

    if args.json:
        applied = apply_result.applied if apply_result is not None else []
        skipped = apply_result.skipped if apply_result is not None else []
        doc: dict[str, Any] = {
            "applied": [
                {"id": o.id, "service": o.service, "field": o.field}
                for o in applied
            ],
            "skipped": [
                {"id": o.id, "service": o.service,
                 "outcome": o.outcome.value, "detail": o.detail}
                for o in skipped
            ],
            "pr_url": pr_result.url if pr_result else None,
            "pr_number": pr_result.number if pr_result else None,
        }
        exit_code = apply_result.exit_code if apply_result is not None else 0
        if pr_result is not None and not pr_result.ok:
            # Prefix matches the stderr error template in non-JSON mode so
            # operators see the same phrasing in both output modes.
            doc["pr_error"] = f"gh pr create failed: {pr_result.error}"
            exit_code = 1
        doc["exit_code"] = exit_code
        print(json.dumps(doc), file=sys.stdout)
        if exit_code != 0:
            raise SystemExit(exit_code)
        return

    # Non-JSON mode: emit PR outcome (success URL to stdout, error block to stderr)
    if pr_result is not None:
        if not pr_result.ok:
            branch = f"learn/recommendations/{plan.incident}"
            print(
                f"Error: PR creation failed\n\n"
                f"  gh pr create failed: {pr_result.error}\n\n"
                f"  Your manifest changes are committed on branch\n"
                f"  {branch}.\n\n"
                f"  Options:\n"
                f"    Retry:   gh pr create --base {args.base} --head {branch}\n"
                f"    Discard: git branch -D {branch}\n\n"
                f"  Exit code: 1",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"PR created: {pr_result.url}")

    # Exit code: from apply if --apply-to was used, else 0
    if apply_result is not None and apply_result.exit_code != 0:
        raise SystemExit(apply_result.exit_code)


def _build_plan_from_incident(incident_id: str):
    """Stub: fetch retrospective from core and call analyze_incident.

    Full implementation requires CoreAPIClient.get_retrospective_for_incident
    (which may need adding to the client). Out of scope for the unit-test
    surface; integration test G1 exercises this path explicitly per the
    design's audited two-step framing.
    """
    raise NotImplementedError(
        "fetching from --incident requires CoreAPIClient integration; "
        "use --from <plan.yaml> for now (or run via integration test G1)"
    )


def _run_pr_path(plan, args, apply_result) -> PRResult | None:
    """Drive the --pr workflow: pre-flight + branch + commit + push + PR.

    Returns the PRResult from `gh pr create` (success or soft-failure), or None
    when no PR was attempted (nothing applied). Hard failures — preflight, git
    commit, git push — still raise SystemExit because they happen before/around
    the apply and don't fit the end-of-run JSON model.
    """
    if apply_result is None or not apply_result.modified_files:
        # Nothing was changed; no PR to open
        return None

    if not _VALID_INCIDENT_ID_RE.match(plan.incident):
        print(
            f"Error: incident id {plan.incident!r} contains invalid characters\n"
            f"\n"
            f"  Incident IDs must match [a-zA-Z0-9._-]+. Branch names are derived\n"
            f"  from the incident id; characters outside this set produce confusing\n"
            f"  git errors or unsafe branch names.\n"
            f"\n"
            f"  Exit code: 2",
            file=sys.stderr,
        )
        raise SystemExit(2)

    specs_dir = Path(args.apply_to)
    branch = f"learn/recommendations/{plan.incident}"

    # Pre-flight (gh-first, branch-last)
    try:
        check_gh_installed()
        check_gh_auth()
        check_git_repo(specs_dir)
        check_remote(specs_dir)
        check_branch_available(specs_dir, branch)
    except PreflightError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Create branch
    _run_git(specs_dir, ["checkout", "-b", branch])

    # Stage only files actually modified by recommendations
    for path in apply_result.modified_files:
        _run_git(specs_dir, ["add", str(path.relative_to(specs_dir))])

    # Commit
    commit_message = _build_commit_message(plan, apply_result, args)
    _run_git(specs_dir, ["commit", "-m", commit_message])

    # Push
    push = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=specs_dir, capture_output=True, text=True, check=False,
    )
    if push.returncode != 0:
        print(
            f"Error: Push to origin failed\n\n"
            f"  git push failed: {push.stderr.strip()}\n\n"
            f"  Your manifest changes are committed on branch\n"
            f"  {branch} (local only).\n\n"
            f"  Options:\n"
            f"    Retry:   git push -u origin {branch}\n"
            f"    Discard: git branch -D {branch}\n\n"
            f"  Exit code: 1",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Create PR
    title = f"Apply NthLayer recommendations from {plan.incident}"
    body = _build_pr_body(plan, apply_result, args)
    pr = create_pr_via_gh(
        title=title, body=body, branch=branch, cwd=specs_dir,
        base=args.base, draft=args.draft,
    )

    # Soft-failure (pr.ok == False) and success both return to caller — the
    # caller decides between human-readable stderr and structured JSON output.
    return pr


def _run_git(cwd, args: list[str]) -> None:
    """Run a git command in cwd; raise SystemExit on non-zero returncode."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {args[0]} failed: {result.stderr.strip()}")


def _build_commit_message(plan, apply_result, args) -> str:
    """Build the structured commit message per jmy.6 design § 6.3."""
    lines = [f"Apply NthLayer recommendations from {plan.incident}", ""]
    for r in apply_result.applied:
        rec = next((x for x in plan.recommendations if x.id == r.id), None)
        if rec is None:
            continue
        change = _format_change(rec)
        lines.append(f"- {r.id}  {rec.type:<15}  {r.service:<14} {r.field}  {change}")
    lines.append("")
    if args.from_path:
        lines.append(f"Plan: {args.from_path}")
    lines.append("Generated by: nthlayer-workers")
    lines.append("Tool: NthLayer learn module")
    return "\n".join(lines)


def _format_change(rec) -> str:
    """Format the change notation for a recommendation."""
    if rec.current_value is None:
        return "(none) → (added)"
    return f"{rec.current_value} → {rec.proposed_value}"


def _build_pr_body(plan, apply_result, args) -> str:
    """Build the PR body per jmy.6 design § 6.4."""
    lines = ["## Changes", "", "| ID | Type | Service | Field | Change |", "|---|---|---|---|---|"]
    for r in apply_result.applied:
        rec = next((x for x in plan.recommendations if x.id == r.id), None)
        if rec is None:
            continue
        lines.append(
            f"| `{r.id}` | `{rec.type}` | `{r.service}` | `{r.field}` | {_format_change(rec)} |"
        )
    if apply_result.skipped:
        lines.append("")
        lines.append("## Skipped")
        lines.append("")
        lines.append("| ID | Reason | Next step |")
        lines.append("|---|---|---|")
        for r in apply_result.skipped:
            next_step = "Investigate" if r.outcome.value == "drift_detected" else "Verify"
            lines.append(f"| `{r.id}` | `{r.outcome.value}` | {next_step} |")
    lines.extend([
        "", "---", "",
        "## Context",
        "",
        f"**Incident:** `{plan.incident}`",
        f"**Plan generated:** {plan.generated_at.isoformat()}",
        "",
        "---",
        "",
        "Generated by NthLayer learn module. Human review and merge required.",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nthlayer-learn", description="Query verdict stores")
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    # accuracy
    acc = sub.add_parser("accuracy", help="Show accuracy report for a producer")
    acc.add_argument("--producer", required=True, help="Producer system name")
    acc.add_argument("--window", default=None, help="Time window (e.g. 30d, 24h)")
    acc.add_argument("--db", default="verdicts.db", help="Path to SQLite store")

    # list
    lst = sub.add_parser("list", help="List verdicts")
    lst.add_argument("--producer", default=None, help="Filter by producer")
    lst.add_argument("--status", default=None, help="Filter by outcome status")
    lst.add_argument("--type", default=None, help="Filter by subject type (evaluation, correlation, etc.)")
    lst.add_argument("--limit", type=int, default=20, help="Max results (default 20)")
    lst.add_argument("--format", default="table", choices=["table", "json"], help="Output format (default: table)")
    lst.add_argument("--db", default="verdicts.db", help="Path to SQLite store")

    # retrospective
    retro = sub.add_parser("retrospective", help="Generate post-incident retrospective from verdict chain")
    retro.add_argument("--incident-verdict", required=True, help="Incident verdict ID")
    retro.add_argument("--specs-dir", default=None, help="Directory of OpenSRM spec YAMLs (for financial impact)")
    retro.add_argument("--db", default="verdicts.db", help="Path to SQLite store")
    retro.add_argument("--decision-store", default=None, help="Path to decision record SQLite DB for content-addressed records")

    # recommendations
    _add_recommendations_subcommand(sub)

    args = parser.parse_args(argv)

    if args.command == "accuracy":
        _cmd_accuracy(args)
    elif args.command == "list":
        _cmd_list(args)
    elif args.command == "retrospective":
        _cmd_retrospective(args)
    elif args.command == "recommendations":
        _cmd_recommendations(args)
