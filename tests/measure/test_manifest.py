"""The calibrate report's judgment SLO, read through nthlayer-common's loader
[opensrm-n4v1].

Fixture provenance: the v2 manifest is the spec's ai-gate service
(``opensrm/spec/v2/examples/services/ai-gate.yaml``: metadata, owner, service)
carrying the spec's standalone JudgmentSLO documents
(``opensrm/spec/v2/examples/judgment-slos/01-reversal-rate.yaml`` and
``02-high-confidence-failure.yaml``) as its ``judgment_slo`` entries, which is
the entry shape nthlayer-common's v2 parser requires (``parser/v2.py``
``_parse_judgment_slo``: ``metadata.name``, ``spec.*``). Copied from opensrm
0f5b1ed; they live here, not read from the sibling checkout, so the suite
does not depend on the workspace layout. The srm/v1 manifest has the shape of
the shipped ``nthlayer/demo/specs/fraud-detect.yaml``.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from nthlayer_common.manifest import ManifestLoadError

from nthlayer_workers.measure.manifest import JudgmentSLO, load_manifest

# opensrm/spec/v2/examples/services/ai-gate.yaml, without its classical SLOs
# and with no judgment_slo block.
_AI_GATE_SERVICE: dict = {
    "apiVersion": "opensrm.nthlayer.io/v2",
    "kind": "ServiceManifest",
    "metadata": {
        "name": "refund-approver",
        "namespace": "default",
        "annotations": {"backstage.io/component-ref": "component:default/refund-approver"},
        "labels": {"domain": "commerce", "tier": "critical"},
    },
    "spec": {
        "owner": {
            "group": "group:default/trust-and-safety",
            "escalation": "group:default/sre-on-call",
            "technical_contact": "user:default/sam.okoro",
        },
        "service": {
            "name": "refund-approver",
            "type": "ai-gate",
            "description": "Decides whether a customer refund request is auto-approved, "
                           "declined, or routed to a human reviewer",
        },
    },
}

# opensrm/spec/v2/examples/judgment-slos/01-reversal-rate.yaml
_REVERSAL_RATE_SLO: dict = {
    "apiVersion": "opensrm.nthlayer.io/v2",
    "kind": "JudgmentSLO",
    "metadata": {"name": "triage-agent-reversal-rate", "namespace": "default"},
    "spec": {
        "service": "triage-agent",
        "judgment_type": "reversal_rate",
        "measurement": {"window": "7d", "source": "lineage"},
        "target": {"maximum_reversal_rate": 0.05},
        "breach_actions": [
            {"notify": "group:default/sre-payments"},
            {"create_case": {"priority": "P2"}},
        ],
    },
}

# opensrm/spec/v2/examples/judgment-slos/02-high-confidence-failure.yaml
_HIGH_CONFIDENCE_FAILURE_SLO: dict = {
    "apiVersion": "opensrm.nthlayer.io/v2",
    "kind": "JudgmentSLO",
    "metadata": {"name": "investigation-agent-hcf", "namespace": "default"},
    "spec": {
        "service": "investigation-agent",
        "judgment_type": "high_confidence_failure",
        "measurement": {"confidence_threshold": 0.9, "window": "30d"},
        "target": {"maximum_failure_rate": 0.02},
    },
}


def _v2_manifest(tmp_path: Path, judgment_slos: list[dict]) -> Path:
    """The spec's ai-gate service carrying the given judgment_slo entries."""
    doc = copy.deepcopy(_AI_GATE_SERVICE)
    if judgment_slos:
        doc["spec"]["judgment_slo"] = copy.deepcopy(judgment_slos)
    path = tmp_path / "ai-gate.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


_SRM_V1 = """\
apiVersion: srm/v1
kind: ServiceReliabilityManifest
metadata:
  name: fraud-detect
  team: payments-ml
  tier: critical
spec:
  type: ai-gate
  slos:
    reversal_rate:
      target: 98.5
      window: 30d
"""

# The shape this loader used to read: its own, under the published
# opensrm/v1 apiVersion, which nthlayer-common rejects as migrate-only.
_BESPOKE_OPENSRM_V1 = """\
apiVersion: opensrm/v1
kind: ServiceReliabilityManifest
metadata:
  name: code-reviewer-agent
  tier: critical
spec:
  type: ai-gate
  slos:
    judgment:
      reversal:
        rate:
          target: 0.05
          window: 30d
      high_confidence_failure:
        target: 0.02
        confidence_threshold: 0.9
      quality_threshold: 0.6
"""


class TestV2:
    def test_reversal_rate_target_is_the_declared_ceiling(self, tmp_path):
        path = _v2_manifest(tmp_path, [_REVERSAL_RATE_SLO, _HIGH_CONFIDENCE_FAILURE_SLO])

        slo = load_manifest(path)

        assert slo == JudgmentSLO(agent_name="refund-approver", reversal_rate_target=0.05)

    def test_target_carries_no_float_noise(self, tmp_path):
        """The report tests ``reversal_rate <= target``: a rate exactly at the
        declared ceiling must be compliant, so the ceiling must come back
        exactly, not as 0.050000000000000044."""
        slo = load_manifest(_v2_manifest(tmp_path, [_REVERSAL_RATE_SLO]))
        assert slo.reversal_rate_target == 0.05
        assert slo.reversal_rate_target >= 1 / 20

    def test_manifest_without_a_reversal_rate_slo_is_none(self, tmp_path):
        assert load_manifest(_v2_manifest(tmp_path, [_HIGH_CONFIDENCE_FAILURE_SLO])) is None

    def test_manifest_without_judgment_slos_is_none(self, tmp_path):
        assert load_manifest(_v2_manifest(tmp_path, [])) is None

    def test_two_reversal_rate_slos_are_refused(self, tmp_path):
        second = copy.deepcopy(_REVERSAL_RATE_SLO)
        second["metadata"]["name"] = "second-reversal-rate"

        with pytest.raises(ManifestLoadError):
            load_manifest(_v2_manifest(tmp_path, [_REVERSAL_RATE_SLO, second]))

    def test_quality_threshold_has_no_manifest_source(self, tmp_path):
        slo = load_manifest(_v2_manifest(tmp_path, [_REVERSAL_RATE_SLO]))
        assert slo.quality_threshold is None


class TestOtherFormats:
    def test_srm_v1_floor_becomes_the_ceiling(self, tmp_path):
        """srm/v1 writes the 0-100 floor (98.5 = at most 1.5% reversed)."""
        path = tmp_path / "fraud-detect.yaml"
        path.write_text(_SRM_V1)

        slo = load_manifest(path)

        assert slo == JudgmentSLO(agent_name="fraud-detect", reversal_rate_target=0.015)

    def test_the_old_bespoke_shape_is_rejected(self, tmp_path):
        path = tmp_path / "old.yaml"
        path.write_text(_BESPOKE_OPENSRM_V1)

        with pytest.raises(ManifestLoadError):
            load_manifest(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_manifest(tmp_path / "nonexistent.yaml")

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")

        with pytest.raises(ManifestLoadError):
            load_manifest(path)


def test_judgment_slo_is_frozen(tmp_path):
    slo = load_manifest(_v2_manifest(tmp_path, [_REVERSAL_RATE_SLO]))
    with pytest.raises(AttributeError):
        slo.reversal_rate_target = 0.10  # type: ignore[misc]
