"""learn resolves verdicts whose ``outcome`` is absent [opensrm-eel4].

Core reads a verdict with no ``outcome``, a null ``outcome`` or no
``outcome.status`` as pending (nthlayer-core ``store.py``,
``update_verdict_outcome``: ``content_dict.get("outcome") or {}`` then
``.get("status", "pending")``, and the SQL
``IFNULL(json_extract(content, '$.outcome.status'), 'pending')``). Measure's
verdicts are stored as sent and carry no ``outcome`` key, so learn has to read
them the same way or it never resolves them.

Fixture provenance: the measure verdicts here are produced by running the real
``MeasureModule`` and capturing what it hands to ``submit_verdict``; nothing is
hand-shaped to match learn. The ``outcome_resolution`` verdict is the dict core
builds in ``server.py`` ``post_verdict_outcome``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.api_client import APIResult

from nthlayer_workers.learn.worker import (
    LearnOutcomeModule,
    LearnRetrospectiveModule,
    _build_chain_timeline,
    _outcome_status,
)
from nthlayer_workers.measure.worker import MeasureModule
from tests.measure.test_measure_worker import _manifest_with_judgment_slos


def _ok(data=None, status_code=200):
    return APIResult(ok=True, status_code=status_code, data=data if data is not None else {})


async def _measure_verdicts() -> dict[str, dict]:
    """Run one breaching MeasureModule cycle; return its verdicts by type.

    The dicts are the envelope ``data`` measure submits, which is what core
    stores and what ``GET /verdicts`` returns.
    """
    with patch("nthlayer_workers.measure.worker.PrometheusProvider") as prov_cls:
        prov = AsyncMock()
        prov.get_sli_value = AsyncMock(return_value=0.92)  # below the 98.5 target
        prov.aclose = AsyncMock()
        prov_cls.return_value = prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=_ok(status_code=201))
        sent: list[dict] = []

        async def _capture(envelope):
            sent.append(envelope["data"])
            return _ok(status_code=201)

        client.submit_verdict = AsyncMock(side_effect=_capture)
        await MeasureModule(client=client, prometheus_url="http://prom:9090").process_cycle()

    by_type = {v["type"]: v for v in sent}
    assert set(by_type) == {"quality_breach", "autonomy_change"}
    return by_type


def _aged(verdict: dict, **age) -> dict:
    """The same verdict, created ``age`` ago (measure stamps it with now)."""
    return {**verdict, "created_at": (datetime.now(UTC) - timedelta(**age)).isoformat()}


def _learn_client(verdicts: list[dict], descendants: list[dict] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get_verdicts = AsyncMock(return_value=_ok(verdicts))
    client.get_descendants = AsyncMock(return_value=_ok(descendants or []))
    client.resolve_outcome = AsyncMock(return_value=_ok(status_code=201))
    client.submit_assessment = AsyncMock(return_value=_ok(status_code=201))
    return client


class TestMeasureVerdictsAreResolved:
    async def test_measure_verdicts_carry_no_outcome_key(self):
        """The premise of the bead. If measure starts writing an outcome, the
        tests below stop exercising the absent-key path: move them to a
        hand-built absent-key verdict before deleting this one."""
        verdicts = await _measure_verdicts()
        assert [t for t, v in verdicts.items() if "outcome" in v] == []

    @pytest.mark.parametrize("verdict_type", ["quality_breach", "autonomy_change"])
    async def test_measure_verdict_past_threshold_is_expired(self, verdict_type):
        verdict = _aged((await _measure_verdicts())[verdict_type], days=10)
        client = _learn_client([verdict])

        await LearnOutcomeModule(client=client, expiry_threshold_days=7).process_cycle()

        client.resolve_outcome.assert_awaited_once()
        verdict_id, resolution = client.resolve_outcome.call_args[0]
        assert verdict_id == verdict["id"]
        assert resolution["outcome_status"] == "expired"

    async def test_measure_breach_is_confirmed_by_downstream_execution(self):
        verdict = _aged((await _measure_verdicts())["quality_breach"], hours=2)
        client = _learn_client(
            [verdict],
            descendants=[{"id": "vrd-exec", "type": "execution", "outcome": {"status": "confirmed"}}],
        )

        await LearnOutcomeModule(client=client).process_cycle()

        client.resolve_outcome.assert_awaited_once()
        verdict_id, resolution = client.resolve_outcome.call_args[0]
        assert verdict_id == verdict["id"]
        assert resolution["outcome_status"] == "confirmed"
        assert resolution["path"] == "lineage"
        # Measure verdicts express no confidence, so the signal has no delta.
        signal = client.submit_assessment.call_args[0][0]["data"]
        assert signal["kind"] == "calibration_signal"
        assert signal["data"]["verdict_type"] == "quality_breach"
        assert signal["data"]["calibration_delta"] is None

    async def test_young_measure_verdict_is_examined_and_left_pending(self):
        """Pending but neither resolvable nor expired: the lineage is looked
        up once, nothing is written, and the cursor moves past it (to the end
        of the window read, opensrm-lpbi)."""
        verdict = _aged((await _measure_verdicts())["quality_breach"], hours=2)
        client = _learn_client([verdict])

        module = LearnOutcomeModule(client=client, expiry_threshold_days=7)
        await module.process_cycle()

        client.get_descendants.assert_awaited_once_with(verdict["id"])
        client.resolve_outcome.assert_not_awaited()
        cursor = datetime.fromisoformat((await module.get_state())["cursor"])
        assert cursor > datetime.fromisoformat(verdict["created_at"])


# Each case is a way a stored verdict can lack a status; core reads all of
# them as pending (see the module docstring).
_NO_STATUS = [
    pytest.param({}, id="absent"),
    pytest.param({"outcome": None}, id="null_outcome"),
    pytest.param({"outcome": {}}, id="empty_outcome"),
    pytest.param({"outcome": {"status": None}}, id="null_status"),
]
_NO_STATUS_SHAPES = [case.values[0] for case in _NO_STATUS]


class TestOutcomeStatusReader:
    @pytest.mark.parametrize("extra", _NO_STATUS)
    def test_missing_status_reads_as_pending(self, extra):
        assert _outcome_status({"id": "v", **extra}) == "pending"

    @pytest.mark.parametrize("status", ["pending", "confirmed", "overridden", "partial", "expired"])
    def test_written_status_is_returned(self, status):
        assert _outcome_status({"id": "v", "outcome": {"status": status}}) == status

    @pytest.mark.parametrize("extra", _NO_STATUS)
    async def test_missing_status_enters_resolution(self, extra):
        very_old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        verdict = {"id": "vrd-x", "created_at": very_old, "type": "quality_breach",
                   "service": "svc", **extra}
        client = _learn_client([verdict])

        await LearnOutcomeModule(client=client, expiry_threshold_days=7).process_cycle()

        client.resolve_outcome.assert_awaited_once()
        assert client.resolve_outcome.call_args[0][1]["outcome_status"] == "expired"

    @pytest.mark.parametrize("status", ["confirmed", "overridden", "partial", "expired"])
    async def test_resolved_verdict_is_still_skipped(self, status):
        very_old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        verdict = {"id": "vrd-done", "created_at": very_old, "type": "quality_breach",
                   "service": "svc", "outcome": {"status": status}}
        client = _learn_client([verdict])

        await LearnOutcomeModule(client=client, expiry_threshold_days=7).process_cycle()

        client.get_descendants.assert_not_awaited()
        client.resolve_outcome.assert_not_awaited()


class TestOutcomeResolutionRecordsAreNotCandidates:
    def _resolution_record(self, original_id: str, created_at: str) -> dict:
        # nthlayer-core server.py post_verdict_outcome: the child verdict core
        # creates for POST /verdicts/{id}/outcome. It has no ``outcome`` key.
        return {
            "id": f"out-{original_id}",
            "type": "outcome_resolution",
            "service": "fraud-detect",
            "created_at": created_at,
            "parent_ids": [original_id],
            "chain_depth": 1,
            "pipeline_latency_ms": None,
            "outcome_status": "expired",
            "resolution": "No outcome signal within threshold",
            "reasoning": None,
        }

    async def test_old_resolution_record_is_not_expired_in_turn(self):
        very_old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        client = _learn_client([self._resolution_record("vrd-breach-1", very_old)])

        await LearnOutcomeModule(client=client, expiry_threshold_days=7).process_cycle()

        client.get_descendants.assert_not_awaited()
        client.resolve_outcome.assert_not_awaited()

    async def test_record_is_skipped_and_its_neighbour_resolved(self):
        very_old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        breach = _aged((await _measure_verdicts())["quality_breach"], days=10)
        client = _learn_client([self._resolution_record("vrd-other", very_old), breach])

        await LearnOutcomeModule(client=client, expiry_threshold_days=7).process_cycle()

        client.resolve_outcome.assert_awaited_once()
        assert client.resolve_outcome.call_args[0][0] == breach["id"]


class _FakeCore:
    """The two core behaviours the lineage path depends on.

    - ``descendants_of`` is transitive: nthlayer-core ``store.py``
      ``_populate_lineage`` copies every ancestor row of the parent, and
      ``descendants_of`` orders by hop distance.
    - ``POST /verdicts/{id}/outcome`` (``server.py`` ``post_verdict_outcome``)
      stores a child ``out-<id>`` of type ``outcome_resolution`` and never
      mutates the original.
    """

    def __init__(self, verdicts: list[dict]):
        self.verdicts = {v["id"]: v for v in verdicts}
        self.resolutions: dict[str, str] = {}

    def _hops(self, descendant: dict, ancestor_id: str, depth: int = 1) -> int | None:
        parents = [p for p in descendant.get("parent_ids") or [] if p in self.verdicts]
        if ancestor_id in parents:
            return depth
        found = [self._hops(self.verdicts[p], ancestor_id, depth + 1) for p in parents]
        return min((h for h in found if h is not None), default=None)

    async def get_descendants(self, verdict_id: str) -> APIResult:
        hops = {v["id"]: self._hops(v, verdict_id) for v in self.verdicts.values()}
        ids = sorted((i for i, h in hops.items() if h is not None), key=lambda i: hops[i])
        return _ok([self.verdicts[i] for i in ids])

    async def resolve_outcome(self, verdict_id: str, body: dict) -> APIResult:
        original = self.verdicts[verdict_id]
        self.resolutions[verdict_id] = body["outcome_status"]
        record = {
            "id": f"out-{verdict_id}",
            "type": "outcome_resolution",
            "service": original.get("service"),
            "created_at": datetime.now(UTC).isoformat(),
            "parent_ids": [verdict_id],
            "chain_depth": 1,
            "pipeline_latency_ms": None,
            "outcome_status": body["outcome_status"],
            "resolution": body.get("resolution"),
            "reasoning": None,
        }
        self.verdicts[record["id"]] = record
        return _ok({"id": record["id"]}, status_code=201)

    def client(self, page: list[dict]) -> AsyncMock:
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=_ok(page))
        client.get_descendants = AsyncMock(side_effect=self.get_descendants)
        client.resolve_outcome = AsyncMock(side_effect=self.resolve_outcome)
        client.submit_assessment = AsyncMock(return_value=_ok(status_code=201))
        return client


class TestResolutionRecordOfAChildIsNotAConfirmation:
    async def test_expiring_the_autonomy_change_does_not_confirm_its_breach(self):
        """Measure's autonomy_change names its quality_breach in parent_ids.
        Core returns the page newest first, so the autonomy_change is expired
        first and its resolution record becomes a descendant of the breach."""
        verdicts = await _measure_verdicts()
        breach = _aged(verdicts["quality_breach"], days=10, minutes=1)
        change = _aged(verdicts["autonomy_change"], days=10)
        assert change["parent_ids"] == [breach["id"]]
        core = _FakeCore([breach, change])
        client = core.client([change, breach])

        await LearnOutcomeModule(client=client, expiry_threshold_days=7).process_cycle()

        assert core.resolutions == {change["id"]: "expired", breach["id"]: "expired"}
        client.submit_assessment.assert_not_awaited()

    async def test_a_verdicts_own_resolution_record_means_it_is_resolved(self):
        """A verdict whose own ``out-<id>`` child exists is resolved already:
        nothing is posted and no signal is emitted [opensrm-lpbi]. Before, the
        child read as a lineage confirmation and was posted again each cycle."""
        verdict = _aged((await _measure_verdicts())["quality_breach"], hours=2)
        core = _FakeCore([verdict])
        await core.resolve_outcome(verdict["id"], {"outcome_status": "confirmed"})
        client = core.client([verdict])

        await LearnOutcomeModule(client=client).process_cycle()

        client.get_descendants.assert_awaited_once_with(verdict["id"])
        client.resolve_outcome.assert_not_awaited()
        client.submit_assessment.assert_not_awaited()

    async def test_execution_under_a_child_still_confirms_the_ancestor(self):
        """Transitive confirmation by a real downstream action is kept."""
        verdicts = await _measure_verdicts()
        breach = _aged(verdicts["quality_breach"], hours=3)
        change = _aged(verdicts["autonomy_change"], hours=3)
        execution = {"id": "vrd-exec", "type": "execution", "service": "fraud-detect",
                     "created_at": datetime.now(UTC).isoformat(),
                     "parent_ids": [change["id"]], "outcome": {"status": "confirmed"}}
        core = _FakeCore([breach, change, execution])
        client = core.client([breach])

        await LearnOutcomeModule(client=client).process_cycle()

        assert core.resolutions == {breach["id"]: "confirmed"}


class TestDescendantAndChainReads:
    async def test_descendant_with_null_outcome_does_not_block_lineage(self):
        """A descendant stored with ``outcome: null`` is read as pending, and
        the execution after it still confirms the parent."""
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        verdict = {"id": "vrd-p", "created_at": old, "type": "approval", "service": "svc",
                   "outcome": {"status": "pending"}}
        client = _learn_client([verdict], descendants=[
            {"id": "vrd-null", "type": "triage", "outcome": None},
            {"id": "vrd-exec", "type": "execution"},
        ])

        await LearnOutcomeModule(client=client).process_cycle()

        client.resolve_outcome.assert_awaited_once()
        assert client.resolve_outcome.call_args[0][1]["outcome_status"] == "confirmed"

    @pytest.mark.parametrize("extra", _NO_STATUS)
    def test_timeline_keeps_a_missing_status_as_none(self, extra):
        """The timeline reports what the verdict carries; it does not crash on
        a null outcome and does not invent a status."""
        timeline = _build_chain_timeline(
            [{"id": "v1", "created_at": "2026-01-01T00:00:00+00:00", "type": "quality_breach",
              "service": "svc", **extra}]
        )
        assert timeline[0]["outcome"] is None

    async def test_retrospective_coverage_reads_outcome_children_not_the_outcome_key(self):
        """Whatever a decision's own ``outcome`` says or lacks, it counts as
        resolved only when core holds an outcome child for it
        [opensrm-91pa]: core never updates the key."""
        from tests.learn.core_fake import FakeCore

        core = FakeCore()
        # Shape of the correlation_snapshot assessment correlate emits
        # (correlate/worker.py): the trigger ids are ``data.parent_ids``.
        chain = [
            {"id": f"v-{n}", "created_at": "2026-01-01T00:10:00+00:00",
             "type": "quality_breach", "service": "svc", "parent_ids": [], **extra}
            for n, extra in enumerate(_NO_STATUS_SHAPES)
        ] + [{"id": "v-done", "created_at": "2026-01-01T00:20:00+00:00", "type": "approval",
              "service": "svc", "parent_ids": [], "outcome": {"status": "confirmed"}}]
        for verdict in chain:
            core.put_verdict(verdict)
        core.put_verdict({"id": "out-v-0", "type": "outcome_resolution",
                          "created_at": "2026-01-01T02:00:00+00:00", "parent_ids": ["v-0"]})
        core.assessments.append({
            "id": "csn-1", "kind": "correlation_snapshot", "service": "svc",
            "created_at": "2026-01-01T01:00:00+00:00",
            "data": {"domain": {"service": "svc"},
                     "window": {"duration_seconds": 3600,
                                "opened_at": "2026-01-01T00:00:00+00:00",
                                "closed_at": "2026-01-01T01:00:00+00:00"},
                     "affected_services": ["svc"], "correlation_groups": [],
                     "parent_ids": [v["id"] for v in chain]},
        })

        await LearnRetrospectiveModule(client=core).process_cycle()

        (retro,) = core.retrospectives
        # v-0 has a child and no outcome key; v-done says confirmed and has none.
        assert retro["data"]["outcome_coverage"] == {
            "resolved": 1, "pending": len(chain) - 1, "total": len(chain),
        }
