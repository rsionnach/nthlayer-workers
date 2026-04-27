"""SitRep configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class SitRepConfig:
    store_path: str = "sitrep-events.db"
    ingestion_host: str = "127.0.0.1"
    ingestion_port: int = 8081
    correlation_window_minutes: int = 5
    dedup_key_fields: list[str] = field(
        default_factory=lambda: ["source", "service", "type", "environment"]
    )
    token_budget: int = 4000
    cache_ttl_minutes: int = 15
    model_name: str = "claude-sonnet-4-20250514"
    model_max_tokens: int = 4096
    verdict_store_path: str = "verdicts.db"
    manifests_dir: str | None = None
    watching_interval: int = 300
    alert_interval: int = 60
    incident_interval: int = 30
    degraded_interval: int = 120
    # Trace backend configuration
    trace_backend: str | None = None
    trace_detail: str = "full"
    trace_baseline_window: str = "1h"
    tempo_endpoint: str = "http://localhost:3200"
    tempo_org_id: str = ""
    tempo_timeout: int = 30
    tempo_use_service_graphs: bool = True


def load_config(path: str | None = None) -> SitRepConfig:
    """Load config from YAML file. Missing fields use defaults."""
    if path is None:
        return SitRepConfig()

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    kwargs: dict[str, Any] = {}

    # Flatten nested YAML structure to flat config
    store = data.get("store", {})
    if "path" in store:
        kwargs["store_path"] = store["path"]

    ingestion = data.get("ingestion", {})
    if "host" in ingestion:
        kwargs["ingestion_host"] = ingestion["host"]
    if "port" in ingestion:
        kwargs["ingestion_port"] = ingestion["port"]

    correlation = data.get("correlation", {})
    if "window_minutes" in correlation:
        kwargs["correlation_window_minutes"] = correlation["window_minutes"]
    if "dedup_key_fields" in correlation:
        kwargs["dedup_key_fields"] = correlation["dedup_key_fields"]

    snapshot = data.get("snapshot", {})
    if "token_budget" in snapshot:
        kwargs["token_budget"] = snapshot["token_budget"]
    if "cache_ttl_minutes" in snapshot:
        kwargs["cache_ttl_minutes"] = snapshot["cache_ttl_minutes"]

    model = data.get("model", {})
    if "model" in model:
        kwargs["model_name"] = model["model"]
    if "max_tokens" in model:
        kwargs["model_max_tokens"] = model["max_tokens"]

    verdict = data.get("verdict", {}).get("store", {})
    if "path" in verdict:
        kwargs["verdict_store_path"] = verdict["path"]

    topology = data.get("topology", {})
    if "manifests_dir" in topology:
        kwargs["manifests_dir"] = topology["manifests_dir"]

    state = data.get("state", {})
    for key in ["watching_interval_seconds", "alert_interval_seconds",
                "incident_interval_seconds", "degraded_interval_seconds"]:
        short_key = key.replace("_seconds", "")
        if key in state:
            kwargs[short_key] = state[key]

    traces = data.get("traces", {})
    if "backend" in traces:
        kwargs["trace_backend"] = traces["backend"]
    if "detail" in traces:
        kwargs["trace_detail"] = traces["detail"]
    if "baseline_window" in traces:
        kwargs["trace_baseline_window"] = traces["baseline_window"]

    tempo = traces.get("tempo", {})
    if "endpoint" in tempo:
        kwargs["tempo_endpoint"] = tempo["endpoint"]
    if "org_id" in tempo:
        kwargs["tempo_org_id"] = tempo["org_id"]
    if "timeout_seconds" in tempo:
        kwargs["tempo_timeout"] = tempo["timeout_seconds"]
    if "use_service_graphs" in tempo:
        kwargs["tempo_use_service_graphs"] = tempo["use_service_graphs"]

    return SitRepConfig(**kwargs)
