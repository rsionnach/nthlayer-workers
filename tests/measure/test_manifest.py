"""The calibrate report's judgment SLO, read through nthlayer-common's loader
[opensrm-n4v1].

Fixture provenance: the v2 manifests are built from the spec's own files
(``opensrm/spec/v2/examples``): the ``services/ai-gate.yaml`` service with the
standalone JudgmentSLO documents of ``judgment-slos/`` as its ``judgment_slo``
entries, which is the entry shape nthlayer-common's v2 parser requires
(``parser/v2.py`` ``_parse_judgment_slo``: ``metadata.name``, ``spec.*``). The
srm/v1 manifest has the shape of the shipped ``nthlayer/demo/specs/fraud-detect.yaml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from nthlayer_common.manifest import ManifestLoadError

from nthlayer_workers.measure.manifest import JudgmentSLO, load_manifest

_SPEC_EXAMPLES = Path(__file__).resolve().parents[3] / "opensrm" / "spec" / "v2" / "examples"


def _spec_doc(relative: str) -> dict:
    src = _SPEC_EXAMPLES / relative
    assert src.is_file(), src
    return yaml.safe_load(src.read_text())


def _v2_manifest(tmp_path: Path, judgment_docs: list[str]) -> Path:
    """The spec's ai-gate service carrying the named spec JudgmentSLO documents."""
    doc = _spec_doc("services/ai-gate.yaml")
    doc["spec"]["judgment_slo"] = [_spec_doc(f"judgment-slos/{name}") for name in judgment_docs]
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
        declared = _spec_doc("judgment-slos/01-reversal-rate.yaml")["spec"]["target"]
        path = _v2_manifest(tmp_path, ["01-reversal-rate.yaml", "02-high-confidence-failure.yaml"])

        slo = load_manifest(path)

        assert slo == JudgmentSLO(
            agent_name=_spec_doc("services/ai-gate.yaml")["metadata"]["name"],
            reversal_rate_target=declared["maximum_reversal_rate"],
        )

    def test_target_carries_no_float_noise(self, tmp_path):
        """The report tests ``reversal_rate <= target``: a rate exactly at the
        declared ceiling must be compliant, so the ceiling must come back
        exactly, not as 0.050000000000000044."""
        slo = load_manifest(_v2_manifest(tmp_path, ["01-reversal-rate.yaml"]))
        assert slo.reversal_rate_target == 0.05
        assert slo.reversal_rate_target >= 1 / 20

    def test_manifest_without_a_reversal_rate_slo_is_none(self, tmp_path):
        path = _v2_manifest(tmp_path, ["02-high-confidence-failure.yaml"])
        assert load_manifest(path) is None

    def test_manifest_without_judgment_slos_is_none(self):
        src = _SPEC_EXAMPLES / "judgment-thresholds" / "all-five-types.yaml"
        assert src.is_file(), src
        assert load_manifest(src) is None

    def test_two_reversal_rate_slos_are_refused(self, tmp_path):
        doc = _spec_doc("services/ai-gate.yaml")
        first = _spec_doc("judgment-slos/01-reversal-rate.yaml")
        second = _spec_doc("judgment-slos/01-reversal-rate.yaml")
        second["metadata"]["name"] = "second-reversal-rate"
        doc["spec"]["judgment_slo"] = [first, second]
        path = tmp_path / "two.yaml"
        path.write_text(yaml.safe_dump(doc))

        with pytest.raises(ManifestLoadError):
            load_manifest(path)

    def test_quality_threshold_has_no_manifest_source(self, tmp_path):
        slo = load_manifest(_v2_manifest(tmp_path, ["01-reversal-rate.yaml"]))
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
    slo = load_manifest(_v2_manifest(tmp_path, ["01-reversal-rate.yaml"]))
    with pytest.raises(AttributeError):
        slo.reversal_rate_target = 0.10  # type: ignore[misc]
