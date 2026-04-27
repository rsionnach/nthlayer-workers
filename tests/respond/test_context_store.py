"""Tests for context store."""
import pytest
from nthlayer_workers.respond.context_store import SQLiteContextStore
from nthlayer_workers.respond.types import (
    CommunicationResult,
    CommunicationUpdate,
    Hypothesis,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    RemediationResult,
    TriageResult,
)


@pytest.fixture
def store(tmp_path):
    s = SQLiteContextStore(str(tmp_path / "test-incidents.db"))
    yield s
    s.close()


def make_context(incident_id="INC-2026-0001", state=IncidentState.TRIGGERED):
    return IncidentContext(
        id=incident_id,
        state=state,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["vrd-001"],
        topology={},
    )


def test_save_and_load(store):
    ctx = make_context()
    store.save(ctx)
    loaded = store.load("INC-2026-0001")
    assert loaded is not None
    assert loaded.id == "INC-2026-0001"
    assert loaded.state == IncidentState.TRIGGERED
    assert loaded.trigger_verdict_ids == ["vrd-001"]


def test_save_overwrites(store):
    ctx = make_context()
    store.save(ctx)
    ctx.state = IncidentState.TRIAGING
    ctx.updated_at = "2026-03-19T10:01:00Z"
    store.save(ctx)
    loaded = store.load("INC-2026-0001")
    assert loaded.state == IncidentState.TRIAGING


def test_load_nonexistent(store):
    assert store.load("INC-NOPE") is None


def test_save_with_triage_result(store):
    ctx = make_context()
    ctx.triage = TriageResult(
        severity=1,
        blast_radius=["payment-api"],
        affected_slos=["availability"],
        assigned_team="payments",
        reasoning="test",
    )
    store.save(ctx)
    loaded = store.load("INC-2026-0001")
    assert loaded.triage is not None
    assert loaded.triage.severity == 1
    assert loaded.triage.blast_radius == ["payment-api"]


def test_save_failed_with_error(store):
    ctx = make_context(state=IncidentState.FAILED)
    ctx.error = "Unrecoverable: model API key expired"
    store.save(ctx)
    loaded = store.load("INC-2026-0001")
    assert loaded.error == "Unrecoverable: model API key expired"


def test_list_active(store):
    store.save(make_context("INC-001", IncidentState.TRIGGERED))
    store.save(make_context("INC-002", IncidentState.INVESTIGATING))
    store.save(make_context("INC-003", IncidentState.RESOLVED))
    store.save(make_context("INC-004", IncidentState.ESCALATED))
    active = store.list_active()
    assert set(active) == {"INC-001", "INC-002"}


def test_list_all(store):
    for i in range(5):
        store.save(make_context(f"INC-{i:03d}"))
    results = store.list_all(limit=3)
    assert len(results) == 3


def test_get_metadata_default(store):
    assert store.get_metadata("last_poll_timestamp") is None


def test_set_and_get_metadata(store):
    store.set_metadata("last_poll_timestamp", "2026-03-19T10:00:00Z")
    assert store.get_metadata("last_poll_timestamp") == "2026-03-19T10:00:00Z"


def test_set_metadata_overwrites(store):
    store.set_metadata("key", "value1")
    store.set_metadata("key", "value2")
    assert store.get_metadata("key") == "value2"


def test_round_trip_preserves_confidence(store):
    """confidence fields on InvestigationResult and CommunicationResult survive round-trip."""
    ctx = make_context()
    ctx.investigation = InvestigationResult(
        hypotheses=[Hypothesis("deploy broke it", 0.87, ["evidence"], "v2.3")],
        root_cause="deploy v2.3",
        root_cause_confidence=0.87,
        reasoning="test",
        confidence=0.92,
    )
    ctx.communication = CommunicationResult(
        updates_sent=[
            CommunicationUpdate("slack", "2026-04-04T10:00:00Z", "initial", "test")
        ],
        reasoning="test",
        confidence=0.85,
    )
    ctx.remediation = RemediationResult(
        proposed_action="rollback",
        target="payment-api",
        reasoning="test",
        confidence=0.78,
    )
    ctx.triage = TriageResult(
        severity=1,
        blast_radius=["payment-api"],
        affected_slos=["availability"],
        assigned_team="payments",
        reasoning="test",
        confidence=0.95,
    )
    store.save(ctx)
    loaded = store.load("INC-2026-0001")

    assert loaded.investigation.confidence == 0.92
    assert loaded.communication.confidence == 0.85
    assert loaded.remediation.confidence == 0.78
    assert loaded.triage.confidence == 0.95
