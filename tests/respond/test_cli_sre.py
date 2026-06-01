"""Tests for SRE CLI commands (oncall, brief, shift-report, suppress, post-incident, delegate)."""

from nthlayer_workers.respond.cli import build_parser


class TestCLIParserSRECommands:
    """Test that the new SRE subcommands are registered and parse correctly."""

    def test_oncall_command_exists(self):
        parser = build_parser()
        args = parser.parse_args(["oncall", "--specs-dir", "./specs"])
        assert args.command == "oncall"
        assert args.specs_dir == "./specs"

    def test_brief_command_exists(self):
        parser = build_parser()
        args = parser.parse_args(["brief", "INC-001", "--verdict-store", "verdicts.db"])
        assert args.command == "brief"
        assert args.incident_id == "INC-001"
        assert args.verdict_store == "verdicts.db"

    def test_shift_report_command_exists(self):
        parser = build_parser()
        args = parser.parse_args([
            "shift-report",
            "--from", "2026-04-13T09:00:00Z",
            "--to", "2026-04-14T09:00:00Z",
            "--verdict-store", "verdicts.db",
        ])
        assert args.command == "shift-report"
        assert args.from_time == "2026-04-13T09:00:00Z"
        assert args.to == "2026-04-14T09:00:00Z"

    def test_suppress_command_exists(self):
        parser = build_parser()
        args = parser.parse_args([
            "suppress",
            "payment-api",
            "latency_p99",
            "--window", "02:00-04:00",
            "--reason", "nightly backup",
            "--baseline", "350.0",
        ])
        assert args.command == "suppress"
        assert args.service == "payment-api"
        assert args.metric == "latency_p99"
        assert args.reason == "nightly backup"
        assert args.baseline == 350.0

    def test_post_incident_command_exists(self):
        parser = build_parser()
        args = parser.parse_args(["post-incident", "INC-001", "--verdict-store", "verdicts.db"])
        assert args.command == "post-incident"
        assert args.incident_id == "INC-001"

    def test_delegate_command_exists(self):
        parser = build_parser()
        args = parser.parse_args(["delegate", "INC-001", "--safe-actions-only"])
        assert args.command == "delegate"
        assert args.incident_id == "INC-001"
        assert args.safe_actions_only is True

    def test_delegate_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["delegate", "INC-001"])
        assert args.safe_actions_only is True  # default
        assert args.max_duration == "2h"  # default

    def test_delegate_no_safe_actions(self):
        """Can disable safe-actions-only via --no-safe-actions-only."""
        parser = build_parser()
        args = parser.parse_args(["delegate", "INC-001", "--no-safe-actions-only"])
        assert args.safe_actions_only is False

    def test_existing_commands_still_work(self):
        """Verify adding new commands doesn't break existing ones."""
        parser = build_parser()

        serve = parser.parse_args(["serve"])
        assert serve.command == "serve"

        replay = parser.parse_args(["replay", "--scenario", "test.yaml"])
        assert replay.command == "replay"

        respond = parser.parse_args(["respond", "--trigger-verdict", "v1"])
        assert respond.command == "respond"
