# ruff: noqa: E501
"""Paced concurrent ingest and read probes, with latency measured against the INTENDED start of each operation.

Pacing is open-loop: a writer that falls behind does not quietly lower the offered load, because each operation's latency is
measured from the instant it was scheduled to start (this avoids coordinated omission, where a stall hides itself by
delaying the next request).

Three ingest modes, reported separately because they measure different things:

* ``raw``: raw SQL INSERT as the admin role with foreign-key triggers OFF (`session_replication_role = replica`). Index
  maintenance cost only.
* ``fk``: raw SQL INSERT as the APPLICATION role with foreign keys ENFORCED against real parent rows. Adds the FK checks and
  the permission boundary; still not the application's code path.
* ``service``: the application's own `ingest_reading()` through an AsyncSession as the application role (mapping lookup,
  contract resolution, conversion, dedup handling, FK enforcement). The closest this harness gets to real ingestion; it is
  still a single-process synthetic client, not the HTTP API.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg

MODES = ("raw", "fk", "service")

RAW_WRITE_SQL = (
    "INSERT INTO telemetry_reading (collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key, "
    "metric, unit, value, occurred_at, received_at) "
    "SELECT md5('c' || mod(g, %s))::uuid, md5('i' || mod(mod(g + %s, %s) + 1, %s))::uuid, md5('m' || (mod(g + %s, %s) + 1))::uuid, "
    "'live-' || g, 'site/live-' || g || '/power_kw', %s || '-' || g, 'power_kw', 'kW', g, now(), now() FROM generate_series(1, %s) g"
)
READ_SQL = (
    "SELECT count(*) FROM (SELECT 1 FROM telemetry_reading WHERE integration_id = md5('i' || %s)::uuid AND metric = 'power_kw' "
    "AND occurred_at >= %s::timestamptz AND occurred_at < %s::timestamptz + interval '1 hour' LIMIT 100) s"
)


def md5_uuid(text: str) -> uuid.UUID:
    return uuid.UUID(bytes=hashlib.md5(text.encode()).digest())  # the same UUIDs PostgreSQL's md5(...)::uuid yields


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
    dsn: str  # psycopg conninfo: admin role for `raw`, application role otherwise
    mappings: int
    mode: str = "raw"
    sa_url: str | None = None  # asyncpg SQLAlchemy URL of the application role (service mode only)
    collectors: int = 8
    integrations: int = 20
    writers: int = 2
    batch_rows: int = 10
    writes_per_second: float = 10.0  # per writer; offered load = writers * writes_per_second * batch_rows rows/s
    readers: int = 1
    reads_per_second: float = 10.0
    events: list = field(default_factory=list)  # (intended_epoch, latency_ms, ok, kind)
    errors: list = field(default_factory=list)  # first few distinct error messages, for the report
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list = field(default_factory=list)
    _token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.mode == "service" and not self.sa_url:
            raise ValueError("service mode needs sa_url")

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
            t.join(timeout=60)
        self._threads.clear()

    def _note_error(self, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {str(exc)[:200]}"
        if text not in self.errors and len(self.errors) < 5:
            self.errors.append(text)

    def _paced(self, rate: float, kind: str, make_op) -> None:
        """Run `op(n)` at `rate` per second until stopped. `make_op()` builds the per-thread state and returns (op, close)."""
        period = 1.0 / rate
        op, close = make_op()
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
                    op(n)
                except Exception as exc:  # noqa: BLE001  any failed operation is recorded, not fatal to the run
                    ok = False
                    self._note_error(exc)
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                    op, close = self._reopen(make_op)
                self.events.append((intended, (time.time() - intended) * 1000.0, ok, kind))
                n += 1
        finally:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _reopen(self, make_op):
        while not self._stop.is_set():
            try:
                return make_op()
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        return (lambda n: None), (lambda: None)

    # -- writers -----------------------------------------------------------------------------------------------------
    def _writer(self, wid: int) -> None:
        make = (lambda: self._service_op(wid)) if self.mode == "service" else (lambda: self._sql_write_op(wid))
        self._paced(self.writes_per_second, "write", make)

    def _sql_write_op(self, wid: int):
        conn = psycopg.connect(self.dsn)
        if self.mode == "raw":
            conn.execute("SET session_replication_role = replica")
        conn.commit()

        def op(n: int) -> None:
            try:
                conn.execute(
                    RAW_WRITE_SQL,
                    (self.collectors, n, self.mappings, self.integrations, n, self.mappings, f"{self._token}-w{wid}-{n}", self.batch_rows),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return op, conn.close

    def _service_op(self, wid: int):
        from datetime import UTC, datetime
        from decimal import Decimal

        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from app.application.telemetry_service import ingest_reading

        loop = asyncio.new_event_loop()
        engine = create_async_engine(self.sa_url, pool_size=1, max_overflow=0)
        mappings, integrations, collectors, batch = self.mappings, self.integrations, self.collectors, self.batch_rows

        async def one(n: int) -> None:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                for g in range(1, batch + 1):
                    mi = (n * batch + g) % mappings + 1
                    result = await ingest_reading(
                        db,
                        collector_id=md5_uuid(f"c{(n + g) % collectors}"),
                        integration_id=md5_uuid(f"i{mi % integrations}"),
                        dedup_key=f"{self._token}-w{wid}-{n}-{g}",
                        external_identifier=f"live-{g}",
                        source_identifier=f"src-{mi}",
                        occurred_at=datetime.now(UTC),
                        value=Decimal(g),
                    )
                    if result.duplicate:
                        raise RuntimeError("unexpected duplicate: dedup keys are unique per operation")
                await db.commit()

        def op(n: int) -> None:
            loop.run_until_complete(one(n))

        def close() -> None:
            try:
                loop.run_until_complete(engine.dispose())
            finally:
                loop.close()

        return op, close

    # -- readers -----------------------------------------------------------------------------------------------------
    def _reader(self, rid: int) -> None:
        anchor = "2026-01-01 00:00:00+00"

        def make():
            conn = psycopg.connect(self.dsn)
            conn.commit()

            def op(n: int) -> None:
                try:
                    conn.execute(READ_SQL, (n % self.integrations, anchor, anchor)).fetchone()
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            return op, conn.close

        self._paced(self.reads_per_second, "read", make)

    # -- results -----------------------------------------------------------------------------------------------------
    def window(self, start: float, end: float) -> dict:
        """Latency, throughput and completed/failed counts per kind for operations whose intended start fell in [start, end)."""
        out: dict = {}
        seconds = max(end - start, 1e-9)
        for kind in ("write", "read"):
            rows = [e for e in self.events if start <= e[0] < end and e[3] == kind]
            ok = [e[1] for e in rows if e[2]]
            per_op = self.batch_rows if kind == "write" else 1
            out[kind] = {
                **summarize(ok),
                "completed": len(ok),
                "failed": sum(1 for e in rows if not e[2]),
                "offered": len(rows),
                "ops_per_second": round(len(ok) / seconds, 2),
                "rows_per_second": round(len(ok) * per_op / seconds, 2),
            }
        return out
