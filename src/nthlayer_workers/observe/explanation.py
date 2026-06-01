"""ExplanationEngine — build human-readable budget explanations from assessments.

Deterministic. No LLM. Pure arithmetic on assessment data.

Enriches budget causes with drift-pattern context (opensrm-pku):
gradual decline / step change / projected exhaustion are surfaced
alongside the SLI gap and budget consumption percentages.
"""
from __future__ import annotations

from nthlayer_common.explanation import BudgetExplanation

from nthlayer_workers.observe.assessment import Assessment
from nthlayer_workers.observe.store import AssessmentFilter, AssessmentStore

# Maps SLO assessment status to explanation severity.
# ERROR/NO_DATA are data-quality issues (e.g. Prometheus unreachable),
# not budget concerns — "warning" severity since they need attention
# but don't indicate budget consumption.
_STATUS_SEVERITY = {
    "EXHAUSTED": "critical",
    "CRITICAL": "critical",
    "WARNING": "warning",
    "ERROR": "warning",
    "HEALTHY": "info",
    "NO_DATA": "info",
    "UNKNOWN": "info",
}

# Drift pattern enum values (lowercase string per DriftPattern.value) →
# cause-string templates. ``{slope_pct}`` is filled in with the
# slope_per_week value (already a fraction, multiplied by 100 here).
# STABLE produces no cause — no signal worth surfacing.
_DRIFT_PATTERN_CAUSE = {
    "gradual_decline":
        "Budget gradually declining at {slope_pct:.2f}%/week (drift detected)",
    "gradual_improvement":
        "Budget gradually improving at {slope_pct:.2f}%/week",
    "step_change_down":
        "Step-change budget drop detected — likely recent deploy or config change",
    "step_change_up":
        "Step-change budget increase detected",
    "seasonal":
        "Seasonal budget pattern detected — review window/time-of-day correlation",
    "volatile":
        "High budget variance detected — unstable trend",
}


class ExplanationEngine:
    """Build budget explanations from the assessment store."""

    def explain_service(
        self,
        service: str,
        store: AssessmentStore,
        slo_filter: str | None = None,
    ) -> list[BudgetExplanation]:
        """Build explanations for a service from latest slo_status assessments.

        Enriches each explanation's ``causes`` with drift context where
        a recent ``drift_signal`` assessment exists for the same SLO
        (opensrm-pku).
        """
        assessments = store.query(
            AssessmentFilter(service=service, kind="slo_status", limit=0)
        )

        # Deduplicate: keep latest per SLO name (query returns desc by timestamp)
        seen: set[str] = set()
        latest: list[Assessment] = []
        for a in assessments:
            slo_name = a.data.get("slo_name", "unknown")
            if slo_name not in seen:
                seen.add(slo_name)
                latest.append(a)

        if slo_filter:
            latest = [a for a in latest if a.data.get("slo_name") == slo_filter]

        drift_by_slo = self._latest_drift_by_slo(service, store)

        return [
            self._explain_slo(service, a, drift_by_slo.get(a.data.get("slo_name", "unknown")))
            for a in latest
        ]

    def _latest_drift_by_slo(
        self, service: str, store: AssessmentStore
    ) -> dict[str, dict]:
        """Return ``{slo_name: drift_data}`` for the latest drift_signal per SLO.

        Bounded to ``service`` and ``kind="drift_signal"`` so the query is
        cheap. ``limit=0`` returns the full set; we dedupe to the latest
        per-SLO ourselves (the assessment store sorts desc by timestamp).
        """
        results: dict[str, dict] = {}
        drift_assessments = store.query(
            AssessmentFilter(service=service, kind="drift_signal", limit=0)
        )
        for a in drift_assessments:
            slo_name = a.data.get("slo_name")
            if slo_name and slo_name not in results:
                results[slo_name] = a.data
        return results

    def _explain_slo(
        self,
        service: str,
        assessment: Assessment,
        drift: dict | None = None,
    ) -> BudgetExplanation:
        data = assessment.data
        slo_name = data.get("slo_name", "unknown")
        status = data.get("status", "UNKNOWN")
        pct = data.get("percent_consumed", 0.0) or 0.0
        burned = data.get("burned_minutes", 0.0) or 0.0
        total = data.get("total_budget_minutes", 0.0) or 0.0
        sli = data.get("current_sli", 0.0) or 0.0
        obj = data.get("objective", 0.0) or 0.0
        window = data.get("window", "30d")
        remaining = max(0, total - burned)
        severity = _STATUS_SEVERITY.get(status, "info")

        # Headline
        status_desc = {
            "EXHAUSTED": "budget exhausted",
            "CRITICAL": "near exhaustion",
            "WARNING": "approaching threshold",
        }
        desc = status_desc.get(status, "within budget")
        headline = f"{slo_name}: {pct:.0f}% consumed — {desc} ({status})"

        # Body. SLI and objective are in collector.py's 0-100 percentage range
        # (slo/collector.py multiplies SLI by 100; YAML target uses the same
        # convention, e.g. availability target=99.9, reversal_rate target=98.5).
        body = (
            f"Window: {window}. "
            f"Budget: {total:.0f} min total, {burned:.0f} min consumed, "
            f"{remaining:.0f} min remaining. "
            f"Current SLI: {sli:.2f}% (target: {obj:.2f}%)."
        )

        # Causes
        causes: list[str] = []
        if pct > 80:
            causes.append(
                "Budget consumption exceeds 80% — sustained error rate above target"
            )
        if sli < obj and obj > 0:
            gap = obj - sli  # both in 0-100 range, so subtraction yields percentage points
            causes.append(
                f"Current SLI ({sli:.2f}%) is {gap:.2f}pp below target ({obj:.2f}%)"
            )

        # Drift-derived causes (opensrm-pku). Skipped silently when no drift
        # signal has been recorded for this SLO yet (e.g. cold start).
        if drift is not None:
            pattern = drift.get("pattern")
            template = _DRIFT_PATTERN_CAUSE.get(pattern)
            if template is not None:
                slope_per_week = drift.get("slope_per_week", 0.0) or 0.0
                causes.append(template.format(slope_pct=slope_per_week * 100))
            days_until = drift.get("days_until_exhaustion")
            if isinstance(days_until, int) and days_until >= 0:
                causes.append(
                    f"Projected exhaustion in {days_until} days at current trend"
                )

        # Actions
        actions: list[str] = []
        if status == "EXHAUSTED":
            actions.append(
                "Deployment gate will block — resolve underlying issue before deploying"
            )
        if status in ("CRITICAL", "EXHAUSTED"):
            actions.append("Investigate root cause of elevated error rate")
            actions.append("Consider freezing deployments until budget recovers")
        elif status == "WARNING":
            actions.append(
                "Monitor trend — investigate if consumption continues to rise"
            )

        return BudgetExplanation(
            service=service,
            slo_name=slo_name,
            headline=headline,
            body=body,
            causes=causes,
            recommended_actions=actions,
            severity=severity,
        )
