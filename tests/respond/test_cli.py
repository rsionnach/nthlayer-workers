# tests/test_cli.py
"""Tests for CLI argument parsing."""
from __future__ import annotations

from nthlayer_workers.respond.cli import build_parser


def test_parser_serve():
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.command == "serve"


def test_parser_serve_with_config():
    parser = build_parser()
    args = parser.parse_args(["serve", "--config", "custom.yaml"])
    assert args.command == "serve"
    assert args.config == "custom.yaml"


def test_parser_replay():
    parser = build_parser()
    args = parser.parse_args(["replay", "--scenario", "test.yaml", "--no-model"])
    assert args.command == "replay"
    assert args.scenario == "test.yaml"
    assert args.no_model is True


def test_parser_replay_no_model_default_false():
    parser = build_parser()
    args = parser.parse_args(["replay", "--scenario", "test.yaml"])
    assert args.no_model is False


def test_parser_approve():
    parser = build_parser()
    args = parser.parse_args(["approve", "INC-2026-0001"])
    assert args.command == "approve"
    assert args.incident_id == "INC-2026-0001"


def test_parser_reject():
    parser = build_parser()
    args = parser.parse_args(["reject", "INC-2026-0001", "--reason", "Wrong action"])
    assert args.command == "reject"
    assert args.incident_id == "INC-2026-0001"
    assert args.reason == "Wrong action"


def test_parser_resume():
    parser = build_parser()
    args = parser.parse_args(["resume", "INC-2026-0001"])
    assert args.command == "resume"
    assert args.incident_id == "INC-2026-0001"


def test_parser_status():
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"


def test_parser_status_with_config():
    parser = build_parser()
    args = parser.parse_args(["status", "--config", "custom.yaml"])
    assert args.command == "status"
    assert args.config == "custom.yaml"
