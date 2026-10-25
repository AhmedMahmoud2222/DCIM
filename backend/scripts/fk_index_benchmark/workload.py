"""Paced concurrent ingest and read probes, with latency measured against the INTENDED start of each operation.

Pacing is open-loop: a writer that falls behind does not quietly lower the offered load, because each operation's latency is
measured from the instant it was scheduled to start (this avoids coordinated omission, where a stall hides itself by
delaying the next request). Writers use `session_replication_role = replica`, so the foreign-key parents of the synthetic
readings need not exist; the insert still maintains every index on `telemetry_reading`, which is what the benchmark is about.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg

WRITE_SQL = (
    "INSERT INTO telemetry_reading (collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key, "
    "metric, unit, value, occurred_at, received_at) "
    "SELECT md5('c' || mod(g, 8))::uuid, md5('i' || mod(g, 20))::uuid, md5('m' || (mod(g + %s, %s) + 1))::uuid, 'live-' || g, "
    "'site/live-' || g || '/power_kw', %s || '-' || g, 'power_kw', 'kW', g, now(), now() FROM generate_series(1, %s) g"
)
READ_SQL = (
    "SELECT count(*) FROM (SELECT 1 FROM telemetry_reading WHERE integration_id = md5('i' || %s)::uuid AND metric = 'power_kw' "
    "AND occurred_at >= %s::timestamptz AND occurred_at < %s::timestamptz + interval '1 hour' LIMIT 100) s"
)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation, so it never reports a latency nobody observed)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil
    return ordered[int(rank) - 1]


def summarize(latencies_ms: list[float]) -> dict:
    return {
        "count": len(latencies_ms),
        "p50_ms": percentile(latencies_ms, 50),
        "p95_ms": percentile(latencies_ms, 95),
        "p99_ms": percentile(latencies_ms, 99),
        "max_ms": max(latencies_ms) if latencies_ms else None,
    }


@dataclass
class Workload:
    dsn: str
    mappings: int
    writers: int = 2
    batch_rows: int = 10
    writes_per_second: float = 10.0  # per writer; offered load = writers * writes_per_second * batch_rows rows/s
    readers: int = 1
    reads_per_second: float = 10.0
    events: list = field(default_factory=list)  # (intended_epoch, latency_ms, ok, kind)
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list = field(default_factory=list)
    _token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def start(self) -> None:
        self._stop.clear()
        for w in range(self.writers):
            self._threads.append(threading.Thread(target=self._writer, args=(w,), daemon=True))
        for r in range(self.readers):
            self._threads.append(threading.Thread(target=self._reader, args=(r,), daemon=True))
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=30)
        self._threads.clear()

    def _connect(self, replica: bool) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn)
        if replica:
            conn.execute("SET session_replication_role = replica")
        conn.commit()
        return conn

    def _paced(self, rate: float, op, kind: str, replica: bool) -> None:
        period = 1.0 / rate
        conn = self._connect(replica)
        next_at = time.time()
        n = 0
        try:
            while not self._stop.is_set():
                delay = next_at - time.time()
                if delay > 0:
                    self._stop.wait(delay)
                    if self._stop.is_set():
                        break
                intended = next_at
                next_at += period
                ok = True
                try:
                    op(conn, n)
                    conn.commit()
                except psycopg.Error:
                    ok = False
                    try:
                        conn.rollback()
                    except psycopg.Error:
                        conn = self._reconnect(replica)
                self.events.append((intended, (time.time() - intended) * 1000.0, ok, kind))
                n += 1
        finally:
            try:
                conn.close()
            except psycopg.Error:
                pass

    def _reconnect(self, replica: bool) -> psycopg.Connection:
        while not self._stop.is_set():
            try:
                return self._connect(replica)
            except psycopg.Error:
                time.sleep(0.2)
        raise psycopg.OperationalError("stopped while reconnecting")

    def _writer(self, wid: int) -> None:
        def op(conn: psycopg.Connection, n: int) -> None:
            conn.execute(WRITE_SQL, (n, self.mappings, f"{self._token}-w{wid}-{n}", self.batch_rows))

        self._paced(self.writes_per_second, op, "write", replica=True)

    def _reader(self, rid: int) -> None:
        anchor = "2026-01-01 00:00:00+00"

        def op(conn: psycopg.Connection, n: int) -> None:
            conn.execute(READ_SQL, (n % 20, anchor, anchor)).fetchone()

        self._paced(self.reads_per_second, op, "read", replica=False)

    def window(self, start: float, end: float) -> dict:
        """Latency summary per kind for operations whose intended start fell in [start, end)."""
        out: dict = {}
        for kind in ("write", "read"):
            rows = [e for e in self.events if start <= e[0] < end and e[3] == kind]
            ok = [e[1] for e in rows if e[2]]
            out[kind] = {
                **summarize(ok),
                "failed": sum(1 for e in rows if not e[2]),
                "ops_per_second": round(len(rows) / (end - start), 2) if end > start else None,
            }
        return out
