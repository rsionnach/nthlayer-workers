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

        result = load_specs(tmp_path)
        assert len(result.service_slos) == 2
        assert all(isinstance(r, ServiceSLO) for r in result.service_slos)
        assert result.service_slos[0].service == "payment-api"
        assert result.service_slos[0].slo.name == "availability"
        assert result.service_slos[0].slo.target == 99.95

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

        result = load_specs(tmp_path)
        assert len(result.service_slos) == 1

    def test_skips_non_srm_files(self, tmp_path):
        (tmp_path / "prometheus.yaml").write_text(
            yaml.dump({"global": {"scrape_interval": "15s"}})
        )
        spec = _valid_spec("svc", {"avail": {"target": 99.9, "window": "7d"}})
        (tmp_path / "valid.yaml").write_text(yaml.dump(spec))

        result = load_specs(tmp_path)
        assert len(result.service_slos) == 1
        assert result.service_slos[0].service == "svc"

    def test_skips_non_yaml_files(self, tmp_path):
        (tmp_path / "readme.md").write_text("# Specs")
        (tmp_path / "data.json").write_text("{}")
        result = load_specs(tmp_path)
        assert result.service_slos == []

    def test_empty_directory(self, tmp_path):
        result = load_specs(tmp_path)
        assert result.service_slos == []

    def test_nonexistent_directory_raises(self):
        with pytest.raises(ValueError, match="does not exist"):
            load_specs("/nonexistent/path")

    def test_malformed_yaml_yields_no_slos_and_is_counted(self, tmp_path):
        """Renamed from test_skips_malformed_yaml: since opensrm-3470 it is
        not skipped, it is COUNTED. A syntax error inside a specs directory
        is a deployment error, and the old name would be read as a live
        contract that it no longer is."""
        (tmp_path / "bad.yaml").write_text("{{invalid yaml:")
        result = load_specs(tmp_path)
        assert result.parse_failures == 1
        assert result.service_slos == []

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
        result = load_specs(tmp_path)
        assert result.service_slos == []

    def test_skips_spec_without_slos(self, tmp_path):
        spec = {
            "apiVersion": "srm/v1",
            "metadata": {"name": "svc", "team": "test-team", "tier": "standard"},
            "spec": {"type": "api"},
        }
        (tmp_path / "no-slos.yaml").write_text(yaml.dump(spec))
        result = load_specs(tmp_path)
        assert result.service_slos == []

    def test_multiple_specs_multiple_services(self, tmp_path):
        for name in ("svc-a", "svc-b"):
            spec = _valid_spec(name, {"avail": {"target": 99.9, "window": "30d"}})
            (tmp_path / f"{name}.yaml").write_text(yaml.dump(spec))

        result = load_specs(tmp_path)
        assert len(result.service_slos) == 2
        services = {r.service for r in result.service_slos}
        assert services == {"svc-a", "svc-b"}


class TestServiceSLO:
    def test_fields(self):
        from nthlayer_common.manifest.models import SLODefinition

        slo = SLODefinition(name="avail", target=99.9, slo_type="availability")
        item = ServiceSLO(service="svc", slo=slo)
        assert item.service == "svc"
        assert item.slo.name == "avail"
        assert item.slo.target == 99.9


class TestParseFailuresAreVisible:
    """opensrm-3470 — the observe path was measuring a subset in silence.

    load_specs swallowed ManifestLoadError, FileNotFoundError, ValueError and
    OSError with a bare `continue`. A service whose manifest failed to parse
    contributed zero SLOs and was indistinguishable from one declaring none,
    so the SLOs that would have breached were simply never evaluated.

    Same failure shape as opensrm-oh27's financial figure, one subsystem over.
    """

    def _broken(self, tmp_path):
        # Aims at being a manifest (v1 header) and fails: tier is invalid, so
        # ReliabilityManifest.__post_init__ raises ValueError, which the old
        # handler swallowed along with everything else.
        (tmp_path / "broken.yaml").write_text(yaml.dump({
            "apiVersion": "srm/v1",
            "kind": "ServiceReliabilityManifest",
            "metadata": {"name": "svc", "team": "t", "tier": "nonexistent-tier"},
            "spec": {"type": "api", "slos": {"a": {"target": 99.9, "window": "7d"}}},
        }))

    def test_a_broken_manifest_is_counted(self, tmp_path):
        self._broken(tmp_path)
        spec = _valid_spec("good", {"avail": {"target": 99.9, "window": "7d"}})
        (tmp_path / "good.yaml").write_text(yaml.dump(spec))

        result = load_specs(tmp_path)

        assert len(result.service_slos) == 1, "the good manifest still loads"
        assert result.parse_failures == 1, (
            "a manifest that failed to parse must reach the caller as a count; "
            "without it a partial view is indistinguishable from a complete one"
        )

    def test_a_broken_manifest_is_logged_with_its_path(self, tmp_path):
        """Asserted on the structured event, not on captured text — the log is
        a data contract for whoever investigates a number that looks wrong."""
        import structlog

        self._broken(tmp_path)

        with structlog.testing.capture_logs() as logs:
            load_specs(tmp_path)

        failures = [e for e in logs if e["event"] == "spec_parse_failed"]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert failures[0]["spec_file"].endswith("broken.yaml")
        assert failures[0]["error"]

    def test_foreign_yaml_is_not_counted_as_a_failure(self, tmp_path):
        """A prometheus rules file sharing the directory is not a broken
        manifest. Counting it would fire the caller's coverage caveat on every
        mixed-directory run, and a caveat that always fires stops being read."""
        (tmp_path / "prometheus.yaml").write_text(
            yaml.dump({"groups": [{"name": "g", "rules": []}]})
        )
        spec = _valid_spec("good", {"avail": {"target": 99.9, "window": "7d"}})
        (tmp_path / "good.yaml").write_text(yaml.dump(spec))

        result = load_specs(tmp_path)

        assert len(result.service_slos) == 1
        assert result.parse_failures == 0

    def test_yml_manifests_are_loaded(self, tmp_path):
        """`.yml` was already handled here, unlike opensrm-oh27's path — this
        pins it so the shared lister cannot regress it."""
        spec = _valid_spec("svc", {"avail": {"target": 99.9, "window": "7d"}})
        (tmp_path / "svc.yml").write_text(yaml.dump(spec))

        result = load_specs(tmp_path)

        assert len(result.service_slos) == 1
        assert result.service_slos[0].service == "svc"

    def test_clean_directory_reports_zero_failures(self, tmp_path):
        spec = _valid_spec("svc", {"avail": {"target": 99.9, "window": "7d"}})
        (tmp_path / "svc.yaml").write_text(yaml.dump(spec))

        result = load_specs(tmp_path)

        assert result.parse_failures == 0
