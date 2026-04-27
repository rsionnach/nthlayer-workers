"""Verdict CLI — query verdict stores from the command line."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

from nthlayer_learn.serialise import to_dict
from nthlayer_learn.sqlite_store import SQLiteVerdictStore
from nthlayer_learn.store import AccuracyFilter, VerdictFilter

_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")

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

    args = parser.parse_args(argv)

    if args.command == "accuracy":
        _cmd_accuracy(args)
    elif args.command == "list":
        _cmd_list(args)
    elif args.command == "retrospective":
        _cmd_retrospective(args)
