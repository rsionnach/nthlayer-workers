"""Concurrency tests for SQLiteAssessmentStore."""

import threading

import pytest

from nthlayer_workers.observe.assessment import create
from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore
from nthlayer_workers.observe.store import AssessmentFilter


@pytest.fixture
def store(tmp_path):
    return SQLiteAssessmentStore(tmp_path / "concurrent.db")


class TestConcurrentAccess:
    def test_concurrent_puts(self, store):
        """Multiple threads writing assessments simultaneously."""
        barrier = threading.Barrier(5)
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            barrier.wait()
            try:
                for i in range(20):
                    store.put(create("slo_status", f"svc-{thread_id}-{i}", {}))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        results = store.query(AssessmentFilter(limit=0))
        assert len(results) == 100

    def test_concurrent_reads_during_writes(self, store):
        """Readers don't crash while a writer is active."""
        barrier = threading.Barrier(6)
        errors: list[Exception] = []

        def writer() -> None:
            barrier.wait()
            try:
                for i in range(50):
                    store.put(create("slo_status", f"svc-{i}", {}))
            except Exception as e:
                errors.append(e)

        def reader() -> None:
            barrier.wait()
            try:
                for _ in range(50):
                    store.query(AssessmentFilter(limit=10))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer)]
        threads += [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

    def test_wal_mode_enabled(self, store):
        conn = store._conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
