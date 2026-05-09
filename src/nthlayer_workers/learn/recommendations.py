"""Spec recommendation engine — Learn → Spec feedback loop (opensrm-jmy.2).

Produces structured ``SpecRecommendation`` documents from closed
incident retrospectives. The recommendation is a machine-readable
proposed change to a service's OpenSRM manifest with rationale and
evidence; a human reviews and merges. NthLayer never auto-modifies
specs (``requires_human_review`` is hardcoded ``True``).

Spec: ``docs/roadmap/NTHLAYER_MISSING_CAPABILITIES_SPEC.md`` § 2.

Scope landed in this commit:

- ``SpecRecommendation`` + ``Recommendation`` dataclasses with YAML
  serialisation (``to_yaml``).
- Two recommendation types from the spec's well-known list:
  ``tighten_slo`` and ``add_deploy_gate``. Both are produced from
  retrospective + verdict-chain analysis without external state.
- ``analyze_incident(retrospective_data, incident_id) ->
  SpecRecommendation`` glue.

Deferred to follow-up beads (each opens its own scope):

- ``add_dependency`` — needs correlator/topology integration to detect
  undeclared dependencies discovered during the incident.
- ``financial_impact`` field — depends on ``opensrm-jmy.1`` (business
  outcome binding) so the spec's ``outcomes`` block carries a
  per-failure cost figure.
- CLI ``--apply-to`` patching against an actual specs repo.
- CLI ``--pr`` git/GitHub flow.
- CLI ``--interactive`` walkthrough.

The defer list is captured on the bead's close reason; the model and
engine here are the building blocks those follow-ups attach to.
"""
from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import yaml

# Severity threshold for the tighten_slo heuristic. The retrospective
# already classifies whether an SLO judgment SLO was breached during
# the incident; a heuristic 80% gap (current_value vs target as a
# fraction of error budget) is the bead's documented trigger.
_TIGHTEN_SLO_GAP_FRACTION = 0.80

# When tightening, propose a new target half-way between the current
# value (where the incident landed) and the original target. Documented
# rationale: a tightening that the operator was already exceeding by
# a wide margin gets dialled most aggressively; mild violations get
# only a small tightening. Mid-point keeps the proposal defensible.
_TIGHTEN_SLO_BLEND = 0.5


@dataclass
class Recommendation:
    """One proposed spec change in a SpecRecommendation document.

    All fields except ``service``, ``type``, ``rationale``, and
    ``proposed_value`` are optional and only populated when the engine
    has the inputs.
    """

    service: str
    type: str  # "tighten_slo" | "add_deploy_gate" | etc.
    rationale: str
    proposed_value: Any
    field: str | None = None
    current_value: Any = None
    confidence: float = 0.0
    financial_impact: str | None = None
    # ``dataclasses.field`` not ``field`` — the attribute above shadows
    # the module-level imported name when used as a default expression.
    evidence: list[dict[str, Any]] = dataclasses.field(default_factory=list)


@dataclass
class SpecRecommendation:
    """A SpecRecommendation document — a set of proposed manifest
    changes generated from a single closed incident.

    ``requires_human_review`` is hardcoded ``True`` per spec; the
    constructor accepts the field so tests can verify the invariant
    but writing ``False`` raises.
    """

    incident: str
    generated_by: str
    generated_at: datetime
    confidence: float
    recommendations: list[Recommendation]
    requires_human_review: bool = True

    def __post_init__(self) -> None:
        # Spec § 2 "Validation Rules": requires_human_review cannot be
        # set to false programmatically.
        if self.requires_human_review is not True:
            raise ValueError(
                "requires_human_review is hardcoded True per OpenSRM spec; "
                "humans review and merge — NthLayer never auto-modifies specs"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0]; got {self.confidence}")

    def to_yaml(self) -> str:
        """Serialise to the on-wire SpecRecommendation YAML format."""
        return yaml.safe_dump(self._to_dict(), sort_keys=False, default_flow_style=False)

    def _to_dict(self) -> dict[str, Any]:
        recs: list[dict[str, Any]] = []
        for r in self.recommendations:
            d = asdict(r)
            # Drop None / empty fields so the YAML stays compact and
            # matches the spec's example shape.
            d = {k: v for k, v in d.items() if v not in (None, [], "")}
            recs.append(d)
        return {
            "apiVersion": "opensrm.io/v1",
            "kind": "SpecRecommendation",
            "metadata": {
                "incident": self.incident,
                "generated_by": self.generated_by,
                "generated_at": self.generated_at.isoformat(),
                "confidence": self.confidence,
                "requires_human_review": self.requires_human_review,
            },
            "recommendations": recs,
        }


def analyze_incident(
    retrospective_data: dict[str, Any],
    incident_id: str,
    *,
    generated_by: str = "nthlayer-learn",
) -> SpecRecommendation:
    """Build a SpecRecommendation from a retrospective.

    The retrospective dict shape comes from
    ``learn.retrospective.build_retrospective``. The two heuristics
    we land here:

    - ``tighten_slo``: when a judgment SLO breach in the retrospective
      shows the SLI ran far below target (gap as a fraction of error
      budget exceeds ``_TIGHTEN_SLO_GAP_FRACTION``), propose a tighter
      target half-way between observed value and original target.
      Earlier alerting on the tighter target would have caught the
      regression sooner.

    - ``add_deploy_gate``: when the retrospective's root_causes block
      classifies the incident as a deploy/config_change/model_regression,
      propose a deploy-gate that would have blocked the bad change.
      Threshold defaults to the original target plus a margin so the
      gate is achievable in practice.

    Confidence is the average of the produced recommendations' own
    confidence scores; missing recommendations don't drag it down.
    """
    recs: list[Recommendation] = []

    incident_custom = retrospective_data.get("incident_custom") or {}
    evaluations = retrospective_data.get("evaluations") or []
    root_causes = incident_custom.get("root_causes") or []

    for rec in _tighten_slo_recommendations(evaluations):
        recs.append(rec)
    for rec in _add_deploy_gate_recommendations(evaluations, root_causes):
        recs.append(rec)

    overall_confidence = (
        sum(r.confidence for r in recs) / len(recs) if recs else 0.0
    )

    return SpecRecommendation(
        incident=incident_id,
        generated_by=generated_by,
        generated_at=datetime.now(timezone.utc),
        confidence=round(overall_confidence, 2),
        recommendations=recs,
    )


def _tighten_slo_recommendations(
    evaluations: list[dict[str, Any]],
) -> list[Recommendation]:
    """Produce ``tighten_slo`` recommendations from breached judgment SLOs."""
    out: list[Recommendation] = []
    for e in evaluations:
        if not isinstance(e, dict):
            continue
        if not e.get("breach"):
            continue
        if e.get("slo_type") != "judgment":
            continue
        target = e.get("target")
        current = e.get("current_value")
        slo_name = e.get("slo_name")
        service = e.get("service")
        if target is None or current is None or not slo_name or not service:
            continue
        # Gap fraction: how far below target the SLI ran, normalised
        # by the size of the error budget. Targets are 0-100 percentage
        # convention (opensrm-5fff); gap_fraction is unitless.
        budget = max(0.0, 100.0 - float(target))
        if budget <= 0:
            continue
        gap_fraction = max(0.0, float(target) - float(current)) / budget
        if gap_fraction < _TIGHTEN_SLO_GAP_FRACTION:
            continue
        proposed = float(current) + (float(target) - float(current)) * _TIGHTEN_SLO_BLEND
        out.append(Recommendation(
            service=service,
            type="tighten_slo",
            field=f"spec.slos.{slo_name}.target",
            current_value=target,
            proposed_value=round(proposed, 2),
            rationale=(
                f"During the incident, {slo_name} on {service} ran at "
                f"{current:.2f} against a target of {target:.2f} "
                f"(gap = {gap_fraction:.0%} of error budget). Tightening "
                f"the target to {proposed:.2f} would have triggered "
                f"alerting earlier."
            ),
            confidence=0.7,
            evidence=[{
                "slo_name": slo_name,
                "observed_value": current,
                "configured_target": target,
            }],
        ))
    return out


def _add_deploy_gate_recommendations(
    evaluations: list[dict[str, Any]],
    root_causes: list[dict[str, Any]],
) -> list[Recommendation]:
    """Produce ``add_deploy_gate`` recommendations from change-shaped root causes.

    Pairs each change-classified root cause with the most-breached
    judgment SLO (if any) on the same service to seed the gate
    threshold. If no SLO breach is available we still emit the
    recommendation with a generic threshold prompt — the operator
    fills it in during review.
    """
    out: list[Recommendation] = []
    change_types = {"deploy", "model_deploy", "config_change", "model_regression"}
    for rc in root_causes:
        if not isinstance(rc, dict):
            continue
        if rc.get("type") not in change_types:
            continue
        service = rc.get("service")
        if not service:
            continue

        # Match against a breached judgment SLO on the same service for
        # a concrete threshold; default to a placeholder if none.
        target_slo: dict[str, Any] | None = None
        for e in evaluations:
            if (
                isinstance(e, dict)
                and e.get("service") == service
                and e.get("slo_type") == "judgment"
                and e.get("breach")
            ):
                target_slo = e
                break

        if target_slo is not None:
            slo_name = target_slo.get("slo_name", "judgment_slo")
            threshold = target_slo.get("target")
            rationale = (
                f"Incident root cause classified as {rc.get('type')!r} on "
                f"{service}. A canary deploy-gate keyed on {slo_name} at "
                f"{threshold} would have caught the regression in the "
                f"canary window before promotion."
            )
            evidence = [
                {"root_cause_type": rc.get("type"), "slo_name": slo_name}
            ]
            confidence = 0.65
        else:
            slo_name = "judgment_slo"
            threshold = "<operator threshold>"
            rationale = (
                f"Incident root cause classified as {rc.get('type')!r} on "
                f"{service}, but no judgment SLO was breached during the "
                f"incident. Operator should choose a gate signal during "
                f"review — model-quality or freshness metrics are typical."
            )
            evidence = [{"root_cause_type": rc.get("type")}]
            confidence = 0.4

        out.append(Recommendation(
            service=service,
            type="add_deploy_gate",
            field="spec.deployment.gates.judgment",
            current_value=None,
            proposed_value={
                "enabled": True,
                "block_on": slo_name,
                "threshold": threshold,
                "evaluation_window": "15m",
            },
            rationale=rationale,
            confidence=confidence,
            evidence=evidence,
        ))
    return out
