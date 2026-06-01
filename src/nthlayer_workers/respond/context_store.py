"""SQLite-backed context store for incident crash recovery."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Protocol

from nthlayer_workers.respond.types import (
    TERMINAL_STATES,
    CommunicationResult,
    CommunicationUpdate,
    Hypothesis,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    RemediationResult,
    TriageResult,
)


class ContextStore(Protocol):
    def save(self, context: IncidentContext) -> None: ...
    def load(self, incident_id: str) -> IncidentContext | None: ...
    def list_active(self) -> list[str]: ...
    def list_all(self, limit: int = 50) -> list[IncidentContext]: ...
    def get_metadata(self, key: str) -> str | None: ...
    def set_metadata(self, key: str, value: str) -> None: ...
    def close(self) -> None: ...


def incident_context_to_dict(ctx: IncidentContext) -> dict:
    """Serialise IncidentContext to a plain dict suitable for JSON encoding.

    dataclasses.asdict() recursively converts nested dataclasses to dicts and
    automatically calls .value on str-enums, which is exactly what we need.

    ``verdict_chain`` is a dataclass field on IncidentContext, so asdict()
    preserves it. This is load-bearing for lineage continuity across worker
    restarts: post-restore, ``_emit_verdict`` reads ``context.verdict_chain[-1]``
    to chain new verdicts to predecessors. An empty or missing chain
    post-restore would cause new verdicts to be created with parent=None,
    breaking the lineage. See ``test_state_roundtrip_preserves_all_fields``.
    """
    return dataclasses.asdict(ctx)


_REQUIRED_INCIDENT_FIELDS = ("id", "state", "created_at", "updated_at", "trigger_source")


def incident_context_from_dict(data: dict) -> IncidentContext:
    """Reconstruct an IncidentContext from its serialised dict form.

    Required fields (id, state, created_at, updated_at, trigger_source) MUST
    be present — corrupt/malformed dicts raise ValueError so the worker's
    restore_state can skip them rather than producing an incident with default
    values that don't match the dict key.
    """
    if not isinstance(data, dict):
        raise ValueError(f"incident_context_from_dict: expected dict, got {type(data).__name__}")
    missing = [f for f in _REQUIRED_INCIDENT_FIELDS if f not in data]
    if missing:
        raise ValueError(f"incident_context_from_dict: missing required fields {missing}")
    """Reconstruct a fully typed IncidentContext from a plain dict."""
    # Reconstruct nested dataclasses manually because dict unpacking alone
    # would leave them as plain dicts.

    triage: TriageResult | None = None
    if data.get("triage") is not None:
        triage = TriageResult(**data["triage"])

    investigation: InvestigationResult | None = None
    if data.get("investigation") is not None:
        inv = data["investigation"]
        hypotheses = [Hypothesis(**h) for h in inv.get("hypotheses", [])]
        investigation = InvestigationResult(
            hypotheses=hypotheses,
            root_cause=inv.get("root_cause"),
            root_cause_confidence=inv.get("root_cause_confidence", 0.0),
            reasoning=inv.get("reasoning", ""),
            confidence=inv.get("confidence"),
        )

    communication: CommunicationResult | None = None
    if data.get("communication") is not None:
        comm = data["communication"]
        updates_sent = [CommunicationUpdate(**u) for u in comm.get("updates_sent", [])]
        communication = CommunicationResult(
            updates_sent=updates_sent,
            reasoning=comm.get("reasoning", ""),
            confidence=comm.get("confidence"),
        )

    remediation: RemediationResult | None = None
    if data.get("remediation") is not None:
        remediation = RemediationResult(**data["remediation"])

    # ``state`` must round-trip from get_state(), so missing/unknown is a
    # genuine corruption. Default to TRIGGERED (an actual IncidentState value)
    # rather than the previous "created" default, which is NOT a valid enum
    # value and silently raised ValueError on every restore.
    raw_state = data.get("state") or IncidentState.TRIGGERED.value
    return IncidentContext(
        id=data.get("id", "unknown"),
        state=IncidentState(raw_state),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        trigger_source=data.get("trigger_source", ""),
        trigger_verdict_ids=data.get("trigger_verdict_ids", []),
        topology=data.get("topology", {}),
        triage=triage,
        investigation=investigation,
        communication=communication,
        remediation=remediation,
        verdict_chain=data.get("verdict_chain", []),
        last_completed_step_index=data.get("last_completed_step_index"),
        error=data.get("error"),
        metadata=data.get("metadata", {}),
    )


_CREATE_INCIDENTS = """
CREATE TABLE IF NOT EXISTS incidents (
    id         TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    error      TEXT,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_METADATA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_IDX_STATE = "CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents (state)"
_CREATE_IDX_UPDATED = "CREATE INDEX IF NOT EXISTS idx_incidents_updated_at ON incidents (updated_at DESC)"


class SQLiteContextStore:
    """SQLite-backed store for IncidentContext objects.

    Uses WAL journal mode and a 5 000 ms busy timeout so concurrent readers
    (e.g. CLI status queries) do not block the coordinator.
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_INCIDENTS)
        self._conn.execute(_CREATE_METADATA)
        self._conn.execute(_CREATE_IDX_STATE)
        self._conn.execute(_CREATE_IDX_UPDATED)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def save(self, context: IncidentContext) -> None:
        """Persist context, overwriting any previous record with the same id."""
        data_json = json.dumps(incident_context_to_dict(context))
        self._conn.execute(
            """
            INSERT INTO incidents (id, state, error, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state      = excluded.state,
                error      = excluded.error,
                data       = excluded.data,
                updated_at = excluded.updated_at
            """,
            (
                context.id,
                context.state.value,
                context.error,
                data_json,
                context.created_at,
                context.updated_at,
            ),
        )
        self._conn.commit()

    def load(self, incident_id: str) -> IncidentContext | None:
        """Return a fully typed IncidentContext, or None if not found."""
        row = self._conn.execute(
            "SELECT data FROM incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        return incident_context_from_dict(json.loads(row[0]))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_active(self) -> list[str]:
        """Return ids of all incidents not in a terminal state."""
        terminal_values = tuple(s.value for s in TERMINAL_STATES)
        placeholders = ",".join("?" * len(terminal_values))
        rows = self._conn.execute(
            f"SELECT id FROM incidents WHERE state NOT IN ({placeholders})",
            terminal_values,
        ).fetchall()
        return [row[0] for row in rows]

    def list_all(self, limit: int = 50) -> list[IncidentContext]:
        """Return up to *limit* incidents ordered by most recently updated."""
        rows = self._conn.execute(
            "SELECT data FROM incidents ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(incident_context_from_dict(json.loads(row[0])))
            except (KeyError, ValueError, json.JSONDecodeError):
                continue  # skip corrupted rows
        return results

    # ------------------------------------------------------------------
    # Metadata key-value store
    # ------------------------------------------------------------------

    def get_metadata(self, key: str) -> str | None:
        """Return a stored metadata value, or None if the key is absent."""
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        return row[0] if row is not None else None

    def set_metadata(self, key: str, value: str) -> None:
        """Insert or replace a metadata key-value pair."""
        self._conn.execute(
            """
            INSERT INTO metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
