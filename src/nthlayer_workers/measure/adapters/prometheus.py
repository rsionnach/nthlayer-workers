"""Prometheus polling adapter — queries Prometheus HTTP API for SLO breaches."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)


@dataclass
class SLODefinition:
    """An SLO parsed from an OpenSRM spec."""

    service: str
    slo_name: str
    slo_type: str  # "traditional" | "judgment"
    target: float
    window: str
    query: str  # PromQL query that returns the current value


@dataclass
class EvaluationResult:
    """Result of evaluating a single SLO."""

    service: str
    slo_name: str
    slo_type: str
    target: float
    current_value: float
    breach: bool
    consecutive: int


def load_specs(specs_dir: Path) -> list[SLODefinition]:
    """Load all OpenSRM specs from a directory and extract SLO definitions."""
    slos: list[SLODefinition] = []
    if not specs_dir.is_dir():
        return slos

    for spec_file in sorted(specs_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(spec_file.read_text())
        except Exception:
            logger.warning("Failed to parse spec: %s", spec_file)
            continue
        if not isinstance(raw, dict):
            continue

        metadata = raw.get("metadata", {})
        service = metadata.get("name", spec_file.stem)
        spec = raw.get("spec", {})
        slo_defs = spec.get("slos", {})

        for slo_name, slo_data in slo_defs.items():
            if not isinstance(slo_data, dict):
                continue
            target = slo_data.get("target")
            if target is None:
                continue

            # Normalize target (e.g., 99.9 → 0.999 for availability)
            if slo_name == "availability" and target > 1:
                target = target / 100.0

            window = slo_data.get("window", "7d")

            # Determine SLO type and PromQL query
            if slo_name in ("reversal_rate", "high_confidence_failure", "calibration", "feedback_latency"):
                slo_type = "judgment"
                query = _judgment_slo_query(service, slo_name, window)
            elif slo_name == "availability":
                slo_type = "traditional"
                query = f'slo:error_budget:ratio{{service="{service}"}}'
            elif slo_name == "latency":
                slo_type = "traditional"
                percentile = slo_data.get("percentile", "p99")
                query = f'slo:http_request_duration_seconds:{percentile}{{service="{service}"}}'
            else:
                slo_type = "traditional"
                query = f'slo:{slo_name}:ratio{{service="{service}"}}'

            slos.append(SLODefinition(
                service=service,
                slo_name=slo_name,
                slo_type=slo_type,
                target=target,
                window=window,
                query=query,
            ))

    return slos


def _judgment_slo_query(service: str, slo_name: str, window: str) -> str:
    """Build PromQL query for judgment SLOs using interim raw metrics."""
    if slo_name == "reversal_rate":
        return (
            f'sum(increase(gen_ai_overrides_total{{service="{service}"}}[{window}]))'
            f' / '
            f'sum(increase(gen_ai_decisions_total{{service="{service}"}}[{window}]))'
        )
    elif slo_name == "high_confidence_failure":
        return (
            f'sum(increase(gen_ai_overrides_hcf_total{{service="{service}"}}[{window}]))'
            f' / '
            f'sum(increase(gen_ai_decisions_total{{service="{service}",confidence_bucket="high"}}[{window}]))'
        )
    elif slo_name == "calibration":
        return f'gen_ai_calibration_error{{service="{service}"}}'
    elif slo_name == "feedback_latency":
        return f'gen_ai_feedback_latency_seconds{{service="{service}"}}'
    logger.warning("Unknown judgment SLO name, no PromQL query available", slo_name=slo_name, service=service)
    return ""


async def query_prometheus(
    client: httpx.AsyncClient,
    prometheus_url: str,
    promql: str,
) -> float | None:
    """Execute a PromQL instant query and return the scalar value, or None on failure."""
    try:
        resp = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": promql},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return None
        # Take the first result's value
        value_pair = results[0].get("value", [])
        if len(value_pair) < 2:
            return None
        val = float(value_pair[1])
        # NaN check (Prometheus returns "NaN" for division by zero)
        if val != val:  # NaN check
            return None
        return val
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        logger.debug("Prometheus query failed: %s — %s", promql, exc)
        return None


async def query_firing_alerts(
    client: httpx.AsyncClient,
    prometheus_url: str,
    service: str | None = None,
) -> list[dict[str, Any]]:
    """Query Prometheus for currently firing alerts, optionally filtered by service."""
    try:
        resp = await client.get(
            f"{prometheus_url}/api/v1/alerts",
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        alerts = data.get("data", {}).get("alerts", [])
        firing = [a for a in alerts if a.get("state") == "firing"]
        if service:
            firing = [a for a in firing if a.get("labels", {}).get("service") == service]
        return firing
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("Alert query failed: %s", exc)
        return []


def count_consecutive_breaches(
    verdicts: list,
    service: str,
    slo_name: str,
) -> int:
    """Count consecutive recent evaluation verdicts with breach=True for a service/SLO.

    Verdicts should be sorted newest-first. Counts from the most recent
    backward until a non-breach is found.
    """
    count = 0
    for v in verdicts:
        custom = getattr(v.metadata, "custom", {}) or {}
        if (
            v.subject.type == "evaluation"
            and v.subject.ref == service
            and custom.get("slo_name") == slo_name
        ):
            # Count consecutive windows where value exceeded target,
            # not where the final breach flag was set (which requires
            # the threshold to already be met — a catch-22).
            current = custom.get("current_value")
            target = custom.get("target")
            if current is not None and target is not None and current > target:
                count += 1
            else:
                break
    return count


async def evaluate_slos(
    prometheus_url: str,
    slos: list[SLODefinition],
    verdict_store,
    hysteresis_threshold: int = 3,
) -> list[EvaluationResult]:
    """Evaluate all SLOs against Prometheus and return results.

    Uses the verdict store to determine consecutive breach count for hysteresis.
    """
    from nthlayer_common.verdicts import VerdictFilter

    results: list[EvaluationResult] = []

    async with httpx.AsyncClient() as client:
        for slo in slos:
            current_value = await query_prometheus(client, prometheus_url, slo.query)
            if current_value is None:
                logger.debug("No data for %s/%s, skipping", slo.service, slo.slo_name)
                continue

            # Determine if current value breaches the target
            if slo.slo_name in ("reversal_rate", "high_confidence_failure", "calibration"):
                # These are "lower is better" — breach if current > target
                raw_breach = current_value > slo.target
            elif slo.slo_name == "feedback_latency":
                # Breach if latency exceeds target (in seconds)
                raw_breach = current_value > slo.target
            elif slo.slo_name == "availability":
                # Error budget ratio: breach if remaining budget < 0
                # target is 0.999, error_budget = 1 - ((1 - current) / (1 - target))
                raw_breach = current_value < 0.0
            elif slo.slo_name == "latency":
                # Breach if p99 exceeds target (target in ms, value in seconds)
                raw_breach = current_value > slo.target / 1000.0
            else:
                raw_breach = current_value < slo.target

            # Get consecutive breach count from verdict store
            recent = verdict_store.query(VerdictFilter(
                producer_system="nthlayer-measure",
                subject_type="evaluation",
                limit=20,
            ))
            # Sort newest first
            recent.sort(key=lambda v: v.timestamp, reverse=True)
            consecutive = count_consecutive_breaches(recent, slo.service, slo.slo_name)

            if raw_breach:
                consecutive += 1
            else:
                consecutive = 0

            # Hysteresis: judgment SLOs only breach after N consecutive windows
            if slo.slo_type == "judgment":
                breach = consecutive >= hysteresis_threshold
            else:
                # Traditional SLOs: Prometheus `for` duration handles hysteresis
                breach = raw_breach

            results.append(EvaluationResult(
                service=slo.service,
                slo_name=slo.slo_name,
                slo_type=slo.slo_type,
                target=slo.target,
                current_value=current_value,
                breach=breach,
                consecutive=consecutive,
            ))

    return results
