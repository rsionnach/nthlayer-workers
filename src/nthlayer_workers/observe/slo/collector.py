"""SLO metric collector — queries Prometheus and produces Assessments."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

import structlog
from nthlayer_common.manifest import ERROR_BUDGET_REMAINING
from nthlayer_common.manifest.models import SLODefinition
from nthlayer_common.providers import PrometheusProvider
from nthlayer_common.providers.prometheus import PrometheusProviderError

from nthlayer_workers.measure.adapters.prometheus import BUDGETED_KINDS, _classical_kind
from nthlayer_workers.observe.assessment import Assessment, create

logger = structlog.get_logger()

STATUS_THRESHOLDS = {
    100: "EXHAUSTED",
    80: "CRITICAL",
    50: "WARNING",
}


@dataclass
class ServiceSLO:
    """SLO with its owning service name.

    Bridges manifest-level context to the collector. The common
    SLODefinition doesn't carry a service field — it's a property
    of ReliabilityManifest. This carrier adds the one field the
    collector needs.
    """

    service: str
    slo: SLODefinition


@dataclass
class SLOResult:
    """Result of collecting metrics for a single SLO."""

    name: str
    objective: float
    window: str
    total_budget_minutes: float
    current_sli: float | None = None
    burned_minutes: float | None = None
    percent_consumed: float | None = None
    status: str = "UNKNOWN"
    error: str | None = None


@dataclass
class BudgetSummary:
    """Aggregate budget summary across all SLOs."""

    total_budget_minutes: float
    burned_budget_minutes: float
    remaining_percent: float
    consumed_percent: float
    valid_slo_count: int


# SLOResult.error for an SLO that is never queried, because generate writes
# no error budget for it. The reason follows the prefix.
NOT_EVALUABLE_PREFIX = "not_evaluable: "
NO_SLO_TYPE = "no slo_type"
JUDGMENT_SLO = "judgment SLOs are evaluated by measure"
# SLOResult.error when Prometheus holds no budget for an SLO that should
# have one: generate's rules are not loaded, or generate chose not to write
# this budget (an objective or latency bound it cannot trust).
NO_BUDGET_SERIES = "no error budget series"
# SLOResult.error when the budget rule has nothing to divide by: no requests
# in the window.
BUDGET_NOT_FINITE = "error budget is not a finite number"
# The two reasons that are an absence of data, not a fault.
_NO_DATA_REASONS = frozenset({NO_BUDGET_SERIES, BUDGET_NOT_FINITE})


def not_evaluable_reason(slo: SLODefinition) -> str | None:
    """Why observe has no error budget to read for this SLO, or None if it
    has one.

    Observe reads the budget generate writes
    (``slo:error_budget_remaining:ratio``), as measure does, and generate
    writes one only for the kinds in measure's ``BUDGETED_KINDS``. Nothing
    else is evaluated, whatever queries the SLO carries [opensrm-9qa6].

    The kind is measure's ``_classical_kind``, so the two modules agree on
    what an SLO is. Two checks come before it, because it would answer
    wrongly: a missing type (it falls back to the SLO's name, and a name is
    not a declaration) and a judgment SLO (it loads as availability, but its
    budget is measure's to evaluate, from the A1 counters).
    """
    slo_type = str(slo.slo_type).strip() if slo.slo_type is not None else ""
    if not slo_type:
        return NO_SLO_TYPE
    if slo.judgment_type:
        return JUDGMENT_SLO
    if _classical_kind(slo) not in BUDGETED_KINDS:
        return f"no error budget is written for slo_type {slo_type!r}"
    return None


@dataclass(frozen=True)
class BudgetReading:
    """What an error budget sample says about its SLO.

    ``remaining`` is the series value: 1.0 a full budget, 0.0 spent,
    negative overspent. ``objective`` is the series' own ``objective``
    label, the 0-100 target the budget was computed against.
    """

    remaining: float
    objective: float

    @property
    def consumed(self) -> float:
        """The share of the budget used: 0.0 none, 1.0 all, above 1 overspent."""
        return 1.0 - self.remaining

    @property
    def sli_percent(self) -> float:
        """The good ratio over the SLO's window, as a percentage.

        generate writes ``remaining = 1 - (1 - good) / (1 - objective/100)``;
        this is that formula solved for ``good``.
        """
        return (1.0 - self.consumed * (1.0 - self.objective / 100.0)) * 100.0


def read_budget_series(series: list[dict]) -> BudgetReading | str:
    """The reading in an instant-query result for one SLO's budget, or the
    reason there is none.

    Exactly one series is a reading. None is ``NO_BUDGET_SERIES``, and a
    value that is not a finite number is ``BUDGET_NOT_FINITE`` (the rule
    divides by a request count of zero): both are an absence of data.
    Anything else is a reason naming a fault: more than one series (two
    rule sets writing the same SLO), a malformed sample, or an
    ``objective`` label that is missing or leaves no budget.
    """
    if not series:
        return NO_BUDGET_SERIES
    if len(series) > 1:
        return f"{len(series)} error budget series for one SLO"
    sample = series[0]
    try:
        remaining = float(sample["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return "error budget sample has no value"
    if not math.isfinite(remaining):
        return BUDGET_NOT_FINITE
    try:
        objective = float(sample["metric"]["objective"])
    except (KeyError, TypeError, ValueError):
        return "error budget series has no objective label"
    if not (math.isfinite(objective) and 0.0 < objective < 100.0):
        return f"error budget series has objective {objective}, which leaves no budget"
    return BudgetReading(remaining=remaining, objective=objective)


class SLOMetricCollector:
    """SLO metric collector.

    Holds state for logging only: which SLOs it has already warned about
    (no type; a budget computed against another objective than the
    manifest's), so a worker that keeps its collector across cycles warns
    once per service and SLO, not once per cycle.
    """

    def __init__(self, prometheus_url: str | None = None) -> None:
        self.prometheus_url = prometheus_url
        self._untyped_warned: set[tuple[str, str]] = set()
        self._objective_warned: set[tuple[str, str]] = set()
        self._username = os.environ.get("PROMETHEUS_USERNAME") or os.environ.get(
            "NTHLAYER_METRICS_USER"
        )
        self._password = os.environ.get("PROMETHEUS_PASSWORD") or os.environ.get(
            "NTHLAYER_METRICS_PASSWORD"
        )

    async def collect(self, slo_definitions: list[ServiceSLO]) -> list[SLOResult]:
        """Collect SLO metrics from Prometheus."""
        if not self.prometheus_url:
            raise ValueError("Prometheus URL is required for metric collection")

        provider = PrometheusProvider(
            self.prometheus_url, username=self._username, password=self._password
        )
        results = []
        try:
            for item in slo_definitions:
                result = await self._collect_single_slo(item, provider)
                results.append(result)
        finally:
            await provider.aclose()

        return results

    async def _collect_single_slo(
        self, item: ServiceSLO, provider: PrometheusProvider
    ) -> SLOResult:
        """Collect metrics for a single SLO."""
        slo = item.slo
        objective = slo.target
        window = slo.window

        window_minutes = _parse_window_minutes(window)
        error_budget_percent = (100 - objective) / 100
        total_budget_minutes = window_minutes * error_budget_percent

        result = SLOResult(
            name=slo.name,
            objective=objective,
            window=window,
            total_budget_minutes=total_budget_minutes,
        )

        reason = not_evaluable_reason(slo)
        if reason is not None:
            # A reading, not silence: the gate and the portfolio take the
            # newest slo_status per SLO whatever its age, so saying nothing
            # would leave an earlier false reading in force.
            if reason == NO_SLO_TYPE and (item.service, slo.name) not in self._untyped_warned:
                self._untyped_warned.add((item.service, slo.name))
                logger.warning("slo_without_type_not_evaluated", service=item.service, slo=slo.name)
            result.error = NOT_EVALUABLE_PREFIX + reason
            result.status = "NO_DATA"
            return result

        # {service, slo}, as measure reads it: the objective is not a
        # matcher but something to read back.
        query = ERROR_BUDGET_REMAINING.selector(service=item.service, slo=slo.name)

        try:
            response = await provider.query(query)
            series = (response.get("data") or {}).get("result") or []
            reading = read_budget_series(series)

            if isinstance(reading, str) and reading in _NO_DATA_REASONS:
                result.error = reading
                result.status = "NO_DATA"
                return result
            if isinstance(reading, str):
                logger.warning(
                    "error_budget_unreadable", service=item.service, slo=slo.name, reason=reading
                )
                result.error = reading
                result.status = "ERROR"
                return result

            self._warn_if_objective_differs(item, reading.objective)
            # The series' objective, not the manifest's target: it is what
            # the budget was computed against, and the two differ while a
            # changed manifest waits for its rules to be regenerated.
            result.objective = reading.objective
            result.total_budget_minutes = window_minutes * (100 - reading.objective) / 100
            result.current_sli = reading.sli_percent
            result.burned_minutes = result.total_budget_minutes * reading.consumed
            result.percent_consumed = reading.consumed * 100
            result.status = _determine_status(result.percent_consumed)

        except PrometheusProviderError as e:
            logger.warning("prometheus_query_failed", slo=slo.name, error=str(e))
            result.error = str(e)
            result.status = "ERROR"
        except Exception as e:
            logger.warning("unexpected_query_error", slo=slo.name, error=str(e), exc_info=True)
            result.error = str(e)
            result.status = "ERROR"

        return result

    def _warn_if_objective_differs(self, item: ServiceSLO, objective: float) -> None:
        """Warn, once per service and SLO, when the budget in Prometheus was
        computed against another objective than the manifest declares."""
        try:
            declared = float(item.slo.target)
        except (TypeError, ValueError):
            declared = math.nan
        if declared == objective:
            return
        key = (item.service, item.slo.name)
        if key in self._objective_warned:
            return
        self._objective_warned.add(key)
        logger.warning(
            "slo_objective_differs_from_manifest",
            service=item.service,
            slo=item.slo.name,
            manifest_target=item.slo.target,
            budget_objective=objective,
        )

    def calculate_aggregate_budget(self, results: list[SLOResult]) -> BudgetSummary:
        """Calculate aggregate budget across all SLOs."""
        valid_results = [r for r in results if r.burned_minutes is not None]

        if not valid_results:
            return BudgetSummary(
                total_budget_minutes=0,
                burned_budget_minutes=0,
                remaining_percent=100,
                consumed_percent=0,
                valid_slo_count=0,
            )

        total_budget = sum(r.total_budget_minutes for r in valid_results)
        burned_budget = sum(r.burned_minutes or 0 for r in valid_results)

        remaining_pct = (
            (total_budget - burned_budget) / total_budget * 100 if total_budget > 0 else 100
        )

        return BudgetSummary(
            total_budget_minutes=total_budget,
            burned_budget_minutes=burned_budget,
            remaining_percent=remaining_pct,
            consumed_percent=100 - remaining_pct,
            valid_slo_count=len(valid_results),
        )


def results_to_assessments(results: list[SLOResult], service: str) -> list[Assessment]:
    """Convert SLOResults to slo_status Assessments."""
    assessments = []
    for r in results:
        data: dict[str, Any] = {
            "slo_name": r.name,
            "objective": r.objective,
            "window": r.window,
            "total_budget_minutes": r.total_budget_minutes,
            "status": r.status,
        }
        if r.current_sli is not None:
            data["current_sli"] = r.current_sli
        if r.burned_minutes is not None:
            data["burned_minutes"] = r.burned_minutes
        if r.percent_consumed is not None:
            data["percent_consumed"] = r.percent_consumed
        if r.error is not None:
            data["error"] = r.error

        assessments.append(create("slo_status", service, data))
    return assessments


def _determine_status(percent_consumed: float) -> str:
    """Determine SLO status based on budget consumption."""
    for threshold, status in sorted(STATUS_THRESHOLDS.items(), reverse=True):
        if percent_consumed >= threshold:
            return status
    return "HEALTHY"


def _parse_window_minutes(window: str) -> float:
    """Parse window string like '30d' into minutes."""
    SUFFIXES = {"d": 24 * 60, "h": 60, "w": 7 * 24 * 60, "m": 1}
    if window and window[-1] in SUFFIXES:
        try:
            return int(window[:-1]) * SUFFIXES[window[-1]]
        except ValueError:
            pass
    logger.warning("unknown_window_format", window=window, default="30d")
    return 30 * 24 * 60  # Default 30 days
