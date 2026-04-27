"""SQLite FTS5 implementation of EventStore."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from nthlayer_workers.correlate.types import EventType, SitRepEvent

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    severity REAL NOT NULL DEFAULT 0.5,
    payload TEXT NOT NULL,
    dependencies TEXT,
    dependents TEXT,
    ttl INTEGER NOT NULL DEFAULT 86400,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_service_time
    ON events(service, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_time
    ON events(type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_changes
    ON events(type, service, timestamp DESC) WHERE type = 'change';
CREATE INDEX IF NOT EXISTS idx_events_expiry
    ON events(created_at, ttl);
"""

_FTS_SCHEMA_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    id, service, source, type, payload_text,
    content=events, content_rowid=rowid,
    tokenize='porter'
);
"""


class SQLiteEventStore:
    """SQLite FTS5 event store with WAL mode and BM25 ranking."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.executescript(_FTS_SCHEMA_SQL)
        self._conn.commit()

    # -- context manager ------------------------------------------------

    def __enter__(self) -> SQLiteEventStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _flatten_payload(payload: dict[str, Any]) -> str:
        """Recursively flatten a dict to searchable text.

        Example: {"alert_name": "latency_breach", "value": 0.5}
              -> "alert_name latency_breach value 0.5"
        """
        parts: list[str] = []

        def _walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    parts.append(str(k))
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)
            else:
                parts.append(str(obj))

        _walk(payload)
        return " ".join(parts)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> SitRepEvent:
        """Deserialize a database row into a SitRepEvent."""
        deps_raw = row["dependencies"]
        deps = json.loads(deps_raw) if deps_raw else []
        depts_raw = row["dependents"]
        depts = json.loads(depts_raw) if depts_raw else []

        return SitRepEvent(
            id=row["id"],
            timestamp=row["timestamp"],
            source=row["source"],
            type=EventType(row["type"]),
            service=row["service"],
            environment=row["environment"],
            severity=row["severity"],
            payload=json.loads(row["payload"]),
            dependencies=deps,
            dependents=depts,
            ttl=row["ttl"],
        )

    # -- mutations ------------------------------------------------------

    def insert(self, event: SitRepEvent) -> None:
        """Insert a single event into the store."""
        payload_json = json.dumps(event.payload)
        deps_json = json.dumps(event.dependencies) if event.dependencies else None
        depts_json = json.dumps(event.dependents) if event.dependents else None
        payload_text = self._flatten_payload(event.payload)

        self._conn.execute(
            """INSERT INTO events
               (id, timestamp, source, type, service, environment,
                severity, payload, dependencies, dependents, ttl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.timestamp,
                event.source,
                event.type.value,
                event.service,
                event.environment,
                event.severity,
                payload_json,
                deps_json,
                depts_json,
                event.ttl,
            ),
        )

        # Insert into FTS5 index
        self._conn.execute(
            """INSERT INTO events_fts
               (rowid, id, service, source, type, payload_text)
               SELECT rowid, id, service, source, type, ?
               FROM events WHERE id = ?""",
            (payload_text, event.id),
        )
        self._conn.commit()

    def insert_batch(self, events: list[SitRepEvent]) -> None:
        """Insert multiple events in a single transaction."""
        if not events:
            return
        with self._conn:
            for event in events:
                payload_json = json.dumps(event.payload)
                deps_json = (
                    json.dumps(event.dependencies) if event.dependencies else None
                )
                depts_json = (
                    json.dumps(event.dependents) if event.dependents else None
                )
                payload_text = self._flatten_payload(event.payload)

                self._conn.execute(
                    """INSERT INTO events
                       (id, timestamp, source, type, service, environment,
                        severity, payload, dependencies, dependents, ttl)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.id,
                        event.timestamp,
                        event.source,
                        event.type.value,
                        event.service,
                        event.environment,
                        event.severity,
                        payload_json,
                        deps_json,
                        depts_json,
                        event.ttl,
                    ),
                )

                self._conn.execute(
                    """INSERT INTO events_fts
                       (rowid, id, service, source, type, payload_text)
                       SELECT rowid, id, service, source, type, ?
                       FROM events WHERE id = ?""",
                    (payload_text, event.id),
                )

    # -- queries --------------------------------------------------------

    def get_by_time_window(
        self,
        start: str,
        end: str,
        *,
        service: str | None = None,
        event_type: EventType | None = None,
        min_severity: float | None = None,
    ) -> list[SitRepEvent]:
        """Query events within a time window with optional filters."""
        clauses = ["timestamp >= ?", "timestamp <= ?"]
        params: list[Any] = [start, end]

        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if event_type is not None:
            clauses.append("type = ?")
            params.append(event_type.value)
        if min_severity is not None:
            clauses.append("severity >= ?")
            params.append(min_severity)

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM events WHERE {where} ORDER BY timestamp DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        time_window: tuple[str, str] | None = None,
        service: str | None = None,
    ) -> list[SitRepEvent]:
        """Full-text search using FTS5 with BM25 ranking."""
        clauses = ["events_fts MATCH ?"]
        params: list[Any] = [query]

        if time_window is not None:
            clauses.append("e.timestamp >= ?")
            clauses.append("e.timestamp <= ?")
            params.extend(time_window)
        if service is not None:
            clauses.append("e.service = ?")
            params.append(service)

        params.append(limit)

        where = " AND ".join(clauses)
        sql = f"""\
            SELECT e.* FROM events e
            JOIN events_fts ON e.rowid = events_fts.rowid
            WHERE {where}
            ORDER BY bm25(events_fts)
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get_by_topology(
        self, service: str, hops: int = 1
    ) -> list[SitRepEvent]:
        """Query events related to a service via topology (deps/dependents).

        For hops > 1, iteratively expand the set of related services.
        """
        visited: set[str] = {service}
        frontier: set[str] = {service}

        for _ in range(hops):
            next_frontier: set[str] = set()
            for svc in frontier:
                # Find events where this service is the primary service
                rows = self._conn.execute(
                    "SELECT dependencies, dependents FROM events WHERE service = ?",
                    (svc,),
                ).fetchall()
                for row in rows:
                    if row["dependencies"]:
                        for dep in json.loads(row["dependencies"]):
                            if dep not in visited:
                                next_frontier.add(dep)
                    if row["dependents"]:
                        for dept in json.loads(row["dependents"]):
                            if dept not in visited:
                                next_frontier.add(dept)

                # Find events that list this service in their dependencies or dependents
                all_events = self._conn.execute(
                    """SELECT service, dependencies, dependents FROM events
                       WHERE dependencies LIKE ? OR dependents LIKE ?""",
                    (f'%"{svc}"%', f'%"{svc}"%'),
                ).fetchall()
                for row in all_events:
                    if row["service"] not in visited:
                        next_frontier.add(row["service"])

            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break

        # Now fetch all events for the visited services
        if not visited:
            return []

        placeholders = ",".join("?" for _ in visited)
        rows = self._conn.execute(
            f"""\
            SELECT DISTINCT e.* FROM events e
            WHERE e.service IN ({placeholders})
            ORDER BY e.timestamp DESC
            """,
            list(visited),
        ).fetchall()

        # Also get events that mention any visited service in dependencies/dependents
        seen_ids: set[str] = {row["id"] for row in rows}
        result = [self._row_to_event(row) for row in rows]

        for svc in visited:
            extra_rows = self._conn.execute(
                """SELECT * FROM events
                   WHERE (dependencies LIKE ? OR dependents LIKE ?)
                     AND service NOT IN ({})""".format(
                    ",".join("?" for _ in visited)
                ),
                (f'%"{svc}"%', f'%"{svc}"%', *visited),
            ).fetchall()
            for row in extra_rows:
                if row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    result.append(self._row_to_event(row))

        return result

    def get_recent_changes(
        self, service: str, window_minutes: int = 30,
        reference_time: str | None = None,
    ) -> list[SitRepEvent]:
        """Get recent change events for a service.

        Args:
            service: Service name to query changes for.
            window_minutes: How far back to look.
            reference_time: ISO 8601 timestamp to use as "now".
                            Defaults to actual current time if None.
                            Essential for replay with historical timestamps.
        """
        if reference_time is not None:
            sql = """\
                SELECT * FROM events
                WHERE type = 'change'
                  AND service = ?
                  AND timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ', ?, ? || ' minutes')
                  AND timestamp <= ?
                ORDER BY timestamp DESC
            """
            rows = self._conn.execute(
                sql, (service, reference_time, f"-{window_minutes}", reference_time)
            ).fetchall()
        else:
            sql = """\
                SELECT * FROM events
                WHERE type = 'change'
                  AND service = ?
                  AND timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ? || ' minutes')
                ORDER BY timestamp DESC
            """
            rows = self._conn.execute(sql, (service, f"-{window_minutes}")).fetchall()
        return [self._row_to_event(row) for row in rows]

    def expire_old(self) -> int:
        """Delete events whose TTL has been exceeded. Return count deleted."""
        # Find IDs to delete (for FTS cleanup)
        rows = self._conn.execute(
            """\
            SELECT id, rowid FROM events
            WHERE (julianday('now') - julianday(created_at)) * 86400 > ttl
            """
        ).fetchall()

        if not rows:
            return 0

        ids = [row["id"] for row in rows]
        rowids = [row["rowid"] for row in rows]

        # Delete from FTS5 first — must pass exact original values
        for rowid in rowids:
            row = self._conn.execute(
                "SELECT rowid, id, service, source, type, payload FROM events WHERE rowid = ?",
                (rowid,),
            ).fetchone()
            if row:
                payload_text = self._flatten_payload(json.loads(row["payload"]))
                self._conn.execute(
                    """INSERT INTO events_fts(events_fts, rowid, id, service, source, type, payload_text)
                       VALUES ('delete', ?, ?, ?, ?, ?, ?)""",
                    (row["rowid"], row["id"], row["service"], row["source"], row["type"], payload_text),
                )

        # Delete from events table
        placeholders = ",".join("?" for _ in ids)
        self._conn.execute(
            f"DELETE FROM events WHERE id IN ({placeholders})", ids
        )
        self._conn.commit()

        return len(ids)

    def get_state_hash(self, time_window: tuple[str, str]) -> str:
        """SHA256 hash of sorted event IDs in the time window."""
        rows = self._conn.execute(
            "SELECT id FROM events WHERE timestamp >= ? AND timestamp <= ? ORDER BY id",
            (time_window[0], time_window[1]),
        ).fetchall()
        ids = [row["id"] for row in rows]
        content = ",".join(ids)
        return hashlib.sha256(content.encode()).hexdigest()

    def get_stats(self) -> dict[str, Any]:
        """Return basic store statistics."""
        row = self._conn.execute(
            """\
            SELECT
                COUNT(*) as event_count,
                MIN(timestamp) as min_timestamp,
                MAX(timestamp) as max_timestamp
            FROM events
            """
        ).fetchone()

        return {
            "event_count": row["event_count"],
            "min_timestamp": row["min_timestamp"],
            "max_timestamp": row["max_timestamp"],
        }
