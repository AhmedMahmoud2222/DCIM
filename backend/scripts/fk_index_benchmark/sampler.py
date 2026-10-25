# ruff: noqa: E501  (long SQL, report text and f-strings are clearer unwrapped)
"""In-database sampling during a migration run: build progress, locks, WAL, size.

Every figure comes from PostgreSQL's own views at a fixed interval, so a build that finishes between two samples is simply
not observed (the report says so; it never invents a duration). Host CPU is not sampled here: PostgreSQL does not expose it.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

import psycopg

from .resources import ResourceProbe

PROGRESS_SQL = (
    "SELECT p.pid, c.relname, p.phase, p.blocks_total, p.blocks_done, p.tuples_total, p.tuples_done, "
    "p.lockers_total, p.lockers_done FROM pg_stat_progress_create_index p "
    "LEFT JOIN pg_class c ON c.oid = p.index_relid"
)
LOCKS_SQL = (
    "SELECT c.relname, l.mode, l.granted FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
    "WHERE c.relname = ANY(%s) AND l.pid <> pg_backend_pid()"
)
SNAPSHOT_SQL = (
    "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint AS wal_bytes, "
    "(SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database())::bigint AS temp_bytes, "
    "pg_database_size(current_database())::bigint AS db_bytes, "
    "(SELECT coalesce(sum(reads * op_bytes), 0)::bigint FROM pg_stat_io) AS io_read_bytes, "
    "(SELECT coalesce(sum(writes * op_bytes), 0)::bigint FROM pg_stat_io) AS io_write_bytes"
)


def snapshot(conn: psycopg.Connection) -> dict:
    row = conn.execute(SNAPSHOT_SQL).fetchone()
    return dict(zip(("wal_bytes", "temp_bytes", "db_bytes", "io_read_bytes", "io_write_bytes"), row, strict=True))


def delta(before: dict, after: dict) -> dict:
    return {k: after[k] - before[k] for k in before}


class Sampler(threading.Thread):
    def __init__(self, dsn: str, tables: list[str], interval: float = 0.1):
        super().__init__(daemon=True)
        self.dsn, self.tables, self.interval = dsn, tables, interval
        self.stop_event = threading.Event()
        self.progress: list[
            tuple
        ] = []  # (epoch, pid, index, phase, blocks_total, blocks_done, tuples_total, tuples_done, lockers_total, lockers_done)
        self.locks: dict[str, set] = defaultdict(set)  # table -> {(mode, granted)} seen held or awaited by OTHER backends
        self.peak_db_bytes = 0
        self.probe = ResourceProbe()

    def run(self) -> None:
        conn = psycopg.connect(self.dsn, autocommit=True)
        last_slow = 0.0
        last_res = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.time()
                for row in conn.execute(PROGRESS_SQL).fetchall():
                    self.progress.append((now, *row))
                if now - last_slow >= 0.1:
                    last_slow = now
                    for table, mode, granted in conn.execute(LOCKS_SQL, (self.tables,)).fetchall():
                        self.locks[table].add((mode, bool(granted)))
                    size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
                    self.peak_db_bytes = max(self.peak_db_bytes, size)
                if now - last_res >= 0.5:
                    last_res = now
                    self.probe.sample(conn)
                self.stop_event.wait(self.interval)
        finally:
            conn.close()

    def resources(self) -> dict:
        return self.probe.summary()

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=30)

    def builds(self) -> dict[str, dict]:
        """Per-index observed build interval and seconds spent in each phase (sample-interval resolution)."""
        per_index: dict[str, list[tuple]] = defaultdict(list)
        for sample in self.progress:
            per_index[sample[2]].append(sample)
        out: dict[str, dict] = {}
        for index, samples in per_index.items():
            phases: dict[str, float] = defaultdict(float)
            for current, following in zip(samples, samples[1:] + [None], strict=True):
                if following is not None:
                    phases[current[3]] += following[0] - current[0]
            last = samples[-1]
            out[index] = {
                "first_seen": samples[0][0],
                "last_seen": last[0],
                "observed_seconds": round(last[0] - samples[0][0] + self.interval, 2),
                "phase_seconds": {k: round(v, 2) for k, v in phases.items()},
                "last_phase": last[3],
                "blocks_done": last[5],
                "blocks_total": last[4],
                "max_lockers_total": max(s[8] or 0 for s in samples),
            }
        return out
