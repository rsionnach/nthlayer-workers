"""Session window logic for the correlate worker module.

Events are grouped by correlation domain (service + environment) into
session windows. Windows close on three conditions:
- Temporal gap: no new events for gap_seconds (default 60s)
- Max duration: window has been open for max_duration_seconds (default 15m)
- Trigger: a quality_breach verdict arrives in the domain

This is the architectural core of the correlate module — session windows
are what make correlation novel vs. simple alert aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from nthlayer_workers.correlate.types import SitRepEvent


@dataclass(frozen=True)
class CorrelationDomain:
    """Key for session window grouping."""

    service: str
    environment: str

    @classmethod
    def from_event(cls, event: SitRepEvent) -> CorrelationDomain:
        return cls(service=event.service, environment=event.environment)

    def __str__(self) -> str:
        return f"{self.service}:{self.environment}"


@dataclass
class SessionWindow:
    """An open session window accumulating events for a correlation domain."""

    domain: CorrelationDomain
    events: list[SitRepEvent] = field(default_factory=list)
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_event_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    has_trigger: bool = False

    def _close_flags(
        self,
        now: datetime,
        gap_seconds: float = 60.0,
        max_duration_seconds: float = 900.0,
    ) -> tuple[bool, bool, bool]:
        """Compute the three close condition flags: (gap, max_duration, trigger)."""
        gap = (now - self.last_event_at).total_seconds() >= gap_seconds
        duration = (now - self.opened_at).total_seconds() >= max_duration_seconds
        return (gap, duration, self.has_trigger)

    def should_close(
        self,
        now: datetime,
        gap_seconds: float = 60.0,
        max_duration_seconds: float = 900.0,
    ) -> bool:
        """Check whether this window should close.

        Returns True if ANY of the three close conditions is met:
        - Temporal gap: no new event for gap_seconds since last_event_at
        - Max duration: window has been open longer than max_duration_seconds
          (prevents never-closing windows under continuous load)
        - Trigger: a quality_breach verdict arrived in this domain
        """
        return any(self._close_flags(now, gap_seconds, max_duration_seconds))

    def close_reason(
        self,
        now: datetime,
        gap_seconds: float = 60.0,
        max_duration_seconds: float = 900.0,
    ) -> str:
        """Return the reason this window closed. Trigger takes priority."""
        gap, duration, trigger = self._close_flags(now, gap_seconds, max_duration_seconds)
        if trigger:
            return "trigger"
        if duration:
            return "max_duration"
        if gap:
            return "gap"
        return "unknown"

    def add_event(self, event: SitRepEvent) -> None:
        """Add an event to the window, updating last_event_at."""
        self.events.append(event)
        ts = _parse_event_ts(event)
        if ts > self.last_event_at:
            self.last_event_at = ts


class SessionWindowManager:
    """Manages open session windows across correlation domains."""

    def __init__(
        self,
        gap_seconds: float = 60.0,
        max_duration_seconds: float = 900.0,
    ):
        self.gap_seconds = gap_seconds
        self.max_duration_seconds = max_duration_seconds
        self._windows: dict[CorrelationDomain, SessionWindow] = {}

    @property
    def open_windows(self) -> dict[CorrelationDomain, SessionWindow]:
        return dict(self._windows)

    def ingest(self, event: SitRepEvent) -> None:
        """Assign an event to a session window. Opens a new window if needed."""
        domain = CorrelationDomain.from_event(event)
        ts = _parse_event_ts(event)

        if domain not in self._windows:
            # Cap opened_at at now — a stale backlogged event should not
            # push opened_at into the past (which would trigger instant
            # max_duration closure on the next cycle).
            now = datetime.now(UTC)
            self._windows[domain] = SessionWindow(
                domain=domain,
                events=[],
                opened_at=min(ts, now),
                last_event_at=ts,
            )

        window = self._windows[domain]
        window.add_event(event)

        # Mark trigger if quality_breach
        if event.source.startswith("verdict:quality_breach") or (
            hasattr(event, "payload")
            and isinstance(event.payload, dict)
            and event.payload.get("type") == "quality_breach"
        ):
            window.has_trigger = True

    def close_ready(self, now: datetime | None = None) -> list[SessionWindow]:
        """Close and return all windows that meet their close conditions."""
        if now is None:
            now = datetime.now(UTC)

        closed: list[SessionWindow] = []
        for domain in list(self._windows):
            window = self._windows[domain]
            if window.should_close(now, self.gap_seconds, self.max_duration_seconds):
                closed.append(window)
                del self._windows[domain]

        return closed

    def restore_window(self, domain: CorrelationDomain, metadata: dict) -> None:
        """Restore a window from persisted metadata (crash recovery).

        Events are re-fetched separately; this only restores window state.
        """
        self._windows[domain] = SessionWindow(
            domain=domain,
            events=[],
            opened_at=datetime.fromisoformat(metadata["opened_at"]),
            last_event_at=datetime.fromisoformat(metadata["last_event_at"]),
            has_trigger=metadata.get("has_trigger", False),
        )

    def to_state(self) -> dict:
        """Serialise open windows to dict for component_state persistence."""
        return {
            str(domain): {
                "opened_at": window.opened_at.isoformat(),
                "last_event_at": window.last_event_at.isoformat(),
                "event_count": len(window.events),
                "has_trigger": window.has_trigger,
            }
            for domain, window in self._windows.items()
        }


def _parse_event_ts(event: SitRepEvent) -> datetime:
    """Parse event timestamp to datetime. Always timezone-aware (UTC)."""
    ts = event.timestamp
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
