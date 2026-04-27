"""Tests for nthlayer_observe.slo.spec_loader module."""

import pytest
import yaml

from nthlayer_workers.observe.slo.collector import ServiceSLO
from nthlayer_workers.observe.slo.spec_loader import load_specs


def _valid_spec(name: str, slos: dict) -> dict:
    """Build a valid v1 manifest with required fields."""
    return {
        "apiVersion": "srm/v1",
        "kind": "ServiceReliabilityManifest",
        "metadata": {"name": name, "team": "test-team", "tier": "standard"},
        "spec": {
            "type": "api",
            "slos": slos,
        },
    }


class TestLoadSpecs:
    def test_loads_slos_from_valid_spec(self, tmp_path):
        spec = _valid_spec("payment-api", {
            "availability": {
                "target": 99.95,
                "window": "30d",
                "indicator": {"query": 'up{job="payment"}'},
            },
            "latency": {"target": 200, "window": "30d", "unit": "ms"},
        })
        (tmp_path / "payment.yaml").write_text(yaml.dump(spec))

        results = load_specs(tmp_path)
        assert len(results) == 2
        assert all(isinstance(r, ServiceSLO) for r in results)
        assert results[0].service == "payment-api"
        assert results[0].slo.name == "availability"
        assert results[0].slo.target == 99.95

    def test_loads_srm_v1_api_version(self, tmp_path):
        spec = {
            "apiVersion": "srm/v1",
            "kind": "ServiceReliabilityManifest",
            "metadata": {"name": "svc", "team": "test-team", "tier": "standard"},
            "spec": {
                "type": "api",
                "slos": {"avail": {"target": 99.9, "window": "7d"}},
            },
        }
        (tmp_path / "svc.yaml").write_text(yaml.dump(spec))

        results = load_specs(tmp_path)
        assert len(results) == 1

    def test_skips_non_srm_files(self, tmp_path):
        (tmp_path / "prometheus.yaml").write_text(
            yaml.dump({"global": {"scrape_interval": "15s"}})
        )
        spec = _valid_spec("svc", {"avail": {"target": 99.9, "window": "7d"}})
        (tmp_path / "valid.yaml").write_text(yaml.dump(spec))

        results = load_specs(tmp_path)
        assert len(results) == 1
        assert results[0].service == "svc"

    def test_skips_non_yaml_files(self, tmp_path):
        (tmp_path / "readme.md").write_text("# Specs")
        (tmp_path / "data.json").write_text("{}")
        results = load_specs(tmp_path)
        assert results == []

    def test_empty_directory(self, tmp_path):
        results = load_specs(tmp_path)
        assert results == []

    def test_nonexistent_directory_raises(self):
        with pytest.raises(ValueError, match="does not exist"):
            load_specs("/nonexistent/path")

    def test_skips_malformed_yaml(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("{{invalid yaml:")
        results = load_specs(tmp_path)
        assert results == []

    def test_skips_spec_without_metadata_name(self, tmp_path):
        spec = {
            "apiVersion": "srm/v1",
            "metadata": {"team": "test-team", "tier": "standard"},
            "spec": {
                "type": "api",
                "slos": {"avail": {"target": 99.9}},
            },
        }
        (tmp_path / "no-name.yaml").write_text(yaml.dump(spec))
        results = load_specs(tmp_path)
        assert results == []

    def test_skips_spec_without_slos(self, tmp_path):
        spec = {
            "apiVersion": "srm/v1",
            "metadata": {"name": "svc", "team": "test-team", "tier": "standard"},
            "spec": {"type": "api"},
        }
        (tmp_path / "no-slos.yaml").write_text(yaml.dump(spec))
        results = load_specs(tmp_path)
        assert results == []

    def test_multiple_specs_multiple_services(self, tmp_path):
        for name in ("svc-a", "svc-b"):
            spec = _valid_spec(name, {"avail": {"target": 99.9, "window": "30d"}})
            (tmp_path / f"{name}.yaml").write_text(yaml.dump(spec))

        results = load_specs(tmp_path)
        assert len(results) == 2
        services = {r.service for r in results}
        assert services == {"svc-a", "svc-b"}


class TestServiceSLO:
    def test_fields(self):
        from nthlayer_common.manifest.models import SLODefinition

        slo = SLODefinition(name="avail", target=99.9, slo_type="availability")
        item = ServiceSLO(service="svc", slo=slo)
        assert item.service == "svc"
        assert item.slo.name == "avail"
        assert item.slo.target == 99.9
