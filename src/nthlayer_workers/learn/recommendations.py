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
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import yaml

from nthlayer_common.outcomes import FinancialImpact


def compute_rec_id(incident_id: str, rec_type: str, field: str) -> str:
    """Deterministic rec id from (incident, type, field).

    Format: rec-<12-char-lowercase-sha256-hex>. Stable across tool versions.
    Algorithm pinned in jmy.6 design § 6.1 so future contributors don't
    accidentally change the hash basis.
    """
    payload = f"{incident_id}|{rec_type}|{field}".encode("utf-8")
    return "rec-" + hashlib.sha256(payload).hexdigest()[:12]


class OutcomeKind(StrEnum):
    """Per-recommendation outcome from --apply-to evaluation (jmy.6 § 5).

    Lives in recommendations.py (not _yaml.py) because outcomes are part
    of the recommendation lifecycle — operator-visible in summaries,
    PR body, error messages. _yaml.py's classify_outcome implements the
    state machine that produces these.
    """

    APPLY_CLEAN = "apply_clean"
    ALREADY_APPLIED = "already_applied"
    DRIFT_DETECTED = "drift_detected"
    TARGET_PATH_MISSING = "target_path_missing"
    MANIFEST_NOT_FOUND = "manifest_not_found"


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

# add_dependency confidence (opensrm-jmy.21). Mid-band: the observed
# blast radius is strong evidence that a runtime relationship exists,
# but whether it should be a declared dependency (vs incidental
# co-failure) is an operator judgement — so a flat 0.5 rather than
# the higher tighten_slo / add_deploy_gate confidences.
_ADD_DEPENDENCY_CONFIDENCE = 0.5


@dataclass
class Recommendation:
    """One proposed spec change in a SpecRecommendation document.

    All fields except ``id``, ``service``, ``type``, ``rationale``, and
    ``proposed_value`` are optional and only populated when the engine
    has the inputs. ``id`` is a deterministic rec-<12-char-sha256-hex>
    computed via compute_rec_id(incident_id, type, field).
    """

    id: str
    service: str
    type: str  # "tighten_slo" | "add_deploy_gate" | etc.
    rationale: str
    proposed_value: Any
    field: str | None = None
    current_value: Any = None
    confidence: float = 0.0
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
    # Top-level financial impact for the incident (opensrm-jmy.23).
    # Populated by propagation from retrospective_data — never recomputed
    # here. None when the retrospective lacked an outcomes block.
    financial_impact: FinancialImpact | None = None

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
        metadata: dict[str, Any] = {
            "incident": self.incident,
            "generated_by": self.generated_by,
            "generated_at": self.generated_at.isoformat(),
            "confidence": self.confidence,
            "requires_human_review": self.requires_human_review,
        }
        if self.financial_impact is not None:
            # Native field names from FinancialImpact (estimated, currency,
            # decisions_affected, failure_mode, volume_source). asdict()
            # recursion produces the nested dict; no renaming.
            metadata["financial_impact"] = asdict(self.financial_impact)
        return {
            "apiVersion": "nthlayer.io/learn/v1",
            "kind": "RecommendationPlan",
            "metadata": metadata,
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

    ``financial_impact`` is propagated from
    ``retrospective_data["financial_impact"]`` (the dict produced by
    ``nthlayer_common.outcomes.compute_financial_impact`` upstream). Wrong
    type raises ``TypeError`` naming the incident; absent / ``None`` /
    ``{}`` all coerce to ``None`` (in-flight partial state treated as
    missing rather than fabricating a half-built ``FinancialImpact``).
    Persisted ``{}`` in a plan file is rejected by ``parse_plan_file``
    instead — the two policies are intentional, not symmetric.
    """
    recs: list[Recommendation] = []

    incident_custom = retrospective_data.get("incident_custom") or {}
    evaluations = retrospective_data.get("evaluations") or []
    root_causes = incident_custom.get("root_causes") or []

    for rec in _tighten_slo_recommendations(evaluations, incident_id):
        recs.append(rec)
    for rec in _add_deploy_gate_recommendations(evaluations, root_causes, incident_id):
        recs.append(rec)
    recs.extend(_add_dependency_recommendations(incident_custom, incident_id))

    overall_confidence = (
        sum(r.confidence for r in recs) / len(recs) if recs else 0.0
    )

    # Propagate financial_impact from the retrospective (opensrm-jmy.23).
    # Source: retrospective writes a dict produced by
    # nthlayer_common.outcomes.compute_financial_impact into
    # metadata.custom["financial_impact"] (see learn/retrospective.py:147).
    # Reconstruct the dataclass so downstream consumers see a typed
    # FinancialImpact, not a raw dict. No recompute.
    # Contract: wrong-type input names the incident in the error;
    # missing/extra keys raise TypeError from the dataclass — fail loud.
    # An empty-but-present dict coerces to None (treats upstream partial
    # state as missing rather than fabricating a half-built FinancialImpact).
    fi_dict = retrospective_data.get("financial_impact")
    if fi_dict is not None and not isinstance(fi_dict, dict):
        raise TypeError(
            f"retrospective_data['financial_impact'] for incident "
            f"{incident_id} must be a mapping or None, got "
            f"{type(fi_dict).__name__}"
        )
    financial_impact = FinancialImpact(**fi_dict) if fi_dict else None

    return SpecRecommendation(
        incident=incident_id,
        generated_by=generated_by,
        generated_at=datetime.now(timezone.utc),
        confidence=round(overall_confidence, 2),
        recommendations=recs,
        financial_impact=financial_impact,
    )


def _tighten_slo_recommendations(
    evaluations: list[dict[str, Any]],
    incident_id: str,
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
        field_path = f"spec.slos.{slo_name}.target"
        out.append(Recommendation(
            id=compute_rec_id(incident_id, "tighten_slo", field_path),
            service=service,
            type="tighten_slo",
            field=field_path,
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
    incident_id: str,
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

        gate_field = "spec.deployment.gates.judgment"
        out.append(Recommendation(
            id=compute_rec_id(incident_id, "add_deploy_gate", gate_field),
            service=service,
            type="add_deploy_gate",
            field=gate_field,
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


def _normalize_blast_radius(raw: list[Any]) -> list[str]:
    """Normalise a blast_radius payload to ``list[str]``.

    Retrospective metadata.custom["blast_radius"] is written as
    ``list[str]`` by ``build_retrospective``, but the input shape from
    upstream correlation custom payloads can be either ``list[str]`` or
    ``list[{"service": str, ...}]``. Tolerate both so this heuristic
    doesn't depend on the retrospective normalisation step having run.
    Non-mapping / missing-service entries are silently dropped.

    Duplicates are collapsed in first-seen order via ``dict.fromkeys`` —
    an upstream blast_radius that lists the same service twice would
    otherwise yield two recs with the same id (opensrm-jmy.21 P1 R5).
    """
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            svc = item.get("service")
            if isinstance(svc, str):
                out.append(svc)
    return list(dict.fromkeys(out))


def _add_dependency_recommendations(
    incident_custom: dict[str, Any],
    incident_id: str,
) -> list[Recommendation]:
    """Recommend adding undeclared services from the incident chain to the
    trigger service's manifest dependencies (opensrm-jmy.21).

    Heuristic: for the trigger service S, services that appeared in the
    incident's blast_radius but are NOT in S's declared dependencies (and
    are not S itself) get an add_dependency recommendation.

    Trigger service is read from ``incident_custom["trigger_service"]``.
    Absent → no recommendations (we don't invent a key). Declared deps
    come from ``incident_custom["declared_dependencies_by_service"]``
    populated by ``learn.retrospective.build_retrospective``.
    """
    trigger = incident_custom.get("trigger_service")
    blast = _normalize_blast_radius(incident_custom.get("blast_radius") or [])
    if not trigger or not blast:
        return []
    declared_map = incident_custom.get("declared_dependencies_by_service") or {}
    declared_for_trigger = set(declared_map.get(trigger, []))
    undeclared = [
        svc for svc in blast
        if svc != trigger and svc not in declared_for_trigger
    ]
    recs: list[Recommendation] = []
    for svc in undeclared:
        # field_path includes svc only for hashing — distinguishes
        # per-svc recs in the same incident. rec.field is the actual
        # YAML target ("spec.dependencies[+]") and is identical across
        # the per-incident recs; the apply layer uses rec.proposed_value
        # to disambiguate which item to append.
        field_path = f"spec.dependencies[+].{svc}"
        recs.append(Recommendation(
            id=compute_rec_id(incident_id, "add_dependency", field_path),
            service=trigger,
            type="add_dependency",
            rationale=(
                f"{svc} participated in the incident chain for {trigger} "
                f"({incident_id}) but is not declared in {trigger}'s "
                f"manifest dependencies."
            ),
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value={"name": svc, "type": "unknown"},
            confidence=_ADD_DEPENDENCY_CONFIDENCE,
        ))
    return recs


SUPPORTED_API_VERSIONS = frozenset({"nthlayer.io/learn/v1"})


class PlanFileUnknownVersionError(ValueError):
    """Plan file's apiVersion is not one we recognise."""


class PlanFileInvalidError(ValueError):
    """Plan file is malformed or missing required keys."""


def parse_plan_file(path) -> SpecRecommendation:
    """Read a plan.yaml file and deserialise to SpecRecommendation.

    Validates apiVersion is in SUPPORTED_API_VERSIONS and required
    structural keys are present. Raises PlanFileUnknownVersionError
    or PlanFileInvalidError on bad input. The underlying yaml.YAMLError
    propagates on malformed YAML (handled at a higher layer as
    manifest_parse_error).

    ``metadata.financial_impact`` round-trips through ``to_yaml`` →
    ``parse_plan_file``. Pre-jmy.23 plan files (no key) load with
    ``financial_impact=None``. Any non-mapping value, or a dict that
    fails ``FinancialImpact(**...)`` construction (missing keys, extra
    keys, including ``{}``), raises ``PlanFileInvalidError`` — persisted
    malformed state is rejected rather than coerced. This is intentionally
    stricter than ``analyze_incident``'s in-memory propagation.
    """
    from pathlib import Path

    text = Path(path).read_text()
    data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise PlanFileInvalidError(
            f"plan file root must be a mapping, got {type(data).__name__}"
        )

    api_version = data.get("apiVersion")
    if api_version not in SUPPORTED_API_VERSIONS:
        raise PlanFileUnknownVersionError(
            f"plan file apiVersion {api_version!r} is not supported; "
            f"expected one of {sorted(SUPPORTED_API_VERSIONS)} "
            f"(e.g. nthlayer.io/learn/v1)"
        )

    if "recommendations" not in data:
        raise PlanFileInvalidError("plan file missing required 'recommendations' key")

    if not isinstance(data["recommendations"], list):
        raise PlanFileInvalidError(
            f"plan file recommendations must be a list, "
            f"got {type(data['recommendations']).__name__}"
        )

    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise PlanFileInvalidError("plan file metadata must be a mapping")

    generated_at_str = metadata.get("generated_at")
    if not isinstance(generated_at_str, str):
        raise PlanFileInvalidError("plan file metadata.generated_at must be an ISO 8601 string")

    # opensrm-jmy.23: round-trip financial_impact through metadata so
    # to_yaml() → parse_plan_file preserves the figure. Pre-jmy.23 plan
    # files (no key) load with financial_impact=None.
    fi_metadata = metadata.get("financial_impact")
    if fi_metadata is None:
        financial_impact: FinancialImpact | None = None
    elif isinstance(fi_metadata, dict):
        try:
            financial_impact = FinancialImpact(**fi_metadata)
        except TypeError as exc:
            raise PlanFileInvalidError(
                f"plan metadata.financial_impact invalid: {exc}"
            ) from exc
    else:
        raise PlanFileInvalidError(
            f"plan metadata.financial_impact must be a mapping or null, "
            f"got {type(fi_metadata).__name__}"
        )

    recs: list[Recommendation] = []
    for r in data["recommendations"]:
        if not isinstance(r, dict):
            raise PlanFileInvalidError("each recommendation must be a mapping")
        try:
            recs.append(Recommendation(**r))
        except TypeError as exc:
            raise PlanFileInvalidError(f"recommendation invalid: {exc}") from exc

    try:
        generated_at_parsed = datetime.fromisoformat(generated_at_str)
        if generated_at_parsed.tzinfo is None:
            generated_at_parsed = generated_at_parsed.replace(tzinfo=timezone.utc)
        return SpecRecommendation(
            incident=metadata.get("incident", ""),
            generated_by=metadata.get("generated_by", "nthlayer-learn"),
            generated_at=generated_at_parsed,
            confidence=metadata.get("confidence", 0.0),
            recommendations=recs,
            requires_human_review=metadata.get("requires_human_review", True),
            financial_impact=financial_impact,
        )
    except (TypeError, ValueError) as exc:
        raise PlanFileInvalidError(f"plan metadata invalid: {exc}") from exc
