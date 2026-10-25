# ruff: noqa: E501
"""Host/VM resource observations and exact per-statement timing, each reported with whether it was actually available.

CPU, memory and I/O pressure are read from /proc of the machine the benchmark client runs on. In the Docker Desktop/WSL2 setup
the PostgreSQL container shares that VM, so these are VM-wide figures (they include PostgreSQL, the client and everything else
in the VM). Where the client and server do not share a kernel they describe the client only, and the report says which
metrics were available instead of printing zeros. None of this measures production headroom.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import psycopg


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def _cpu_times() -> tuple[int, int] | None:
    raw = _read("/proc/stat")
    if not raw:
        return None
    fields = [int(x) for x in raw.splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def _meminfo() -> dict[str, int]:
    raw = _read("/proc/meminfo") or ""
    out = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            out[key] = int(rest.split()[0]) * 1024
    return out


def _psi(name: str) -> float | None:
    raw = _read(f"/proc/pressure/{name}")
    if not raw:
        return None
    match = re.search(r"^some avg10=([0-9.]+)", raw, re.M)
    return float(match.group(1)) if match else None


def _oom_kills() -> int | None:
    raw = _read("/sys/fs/cgroup/memory.events")
    if not raw:
        return None
    match = re.search(r"^oom_kill (\d+)", raw, re.M)
    return int(match.group(1)) if match else None


class ResourceProbe:
    """Accumulates min/max/mean over repeated `sample()` calls; `summary()` reports availability per metric."""

    def __init__(self, disk_path: str = "/"):
        self.disk_path = disk_path
        self._prev_cpu = _cpu_times()
        self.cpu: list[float] = []
        self.mem_available: list[int] = []
        self.swap_used: list[int] = []
        self.psi: dict[str, list[float]] = {"cpu": [], "io": [], "memory": []}
        self.disk_free: list[int] = []
        self._oom_start = _oom_kills()
        self.lock_waiter_samples = 0
        self.lock_waiters_max = 0
        self.lock_wait_age_max_s = 0.0
        self.samples = 0

    def sample(self, conn: psycopg.Connection | None = None) -> None:
        self.samples += 1
        cur = _cpu_times()
        if cur and self._prev_cpu and cur[0] > self._prev_cpu[0]:
            total, idle = cur[0] - self._prev_cpu[0], cur[1] - self._prev_cpu[1]
            self.cpu.append(100.0 * (1 - idle / total))
        self._prev_cpu = cur
        mem = _meminfo()
        if "MemAvailable" in mem:
            self.mem_available.append(mem["MemAvailable"])
        if "SwapTotal" in mem and "SwapFree" in mem:
            self.swap_used.append(mem["SwapTotal"] - mem["SwapFree"])
        for name in self.psi:
            value = _psi(name)
            if value is not None:
                self.psi[name].append(value)
        try:
            self.disk_free.append(shutil.disk_usage(self.disk_path).free)
        except OSError:
            pass
        if conn is not None:
            waiters, age = conn.execute(
                "SELECT count(*), coalesce(max(extract(epoch FROM now() - query_start)), 0) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            ).fetchone()
            if waiters:
                self.lock_waiter_samples += 1
                self.lock_waiters_max = max(self.lock_waiters_max, waiters)
                self.lock_wait_age_max_s = max(self.lock_wait_age_max_s, float(age))

    def summary(self) -> dict:
        def span(values, scale=1.0, digits=1):
            return None if not values else {"min": round(min(values) / scale, digits), "max": round(max(values) / scale, digits), "start": round(values[0] / scale, digits), "end": round(values[-1] / scale, digits)}

        oom_now = _oom_kills()
        return {
            "availability": {
                "cpu_percent_vm": bool(self.cpu),
                "memory_available": bool(self.mem_available),
                "swap": bool(self.swap_used),
                "pressure_stall_info": any(self.psi.values()),
                "disk_free": bool(self.disk_free),
                "oom_kill_counter": self._oom_start is not None,
                "lock_waits": True,
                "disk_io_throughput": False,  # not exposed reliably from the client; PostgreSQL's own pg_stat_io deltas are reported instead
            },
            "scope_note": "figures come from /proc of the benchmark client's kernel; in the Docker Desktop/WSL2 setup that is the VM shared with PostgreSQL",
            "cpu_percent_vm": None if not self.cpu else {"mean": round(sum(self.cpu) / len(self.cpu), 1), "max": round(max(self.cpu), 1)},
            "memory_available_mib": span(self.mem_available, 1024 * 1024),
            "swap_used_mib": span(self.swap_used, 1024 * 1024),
            "pressure_some_avg10_max": {k: (max(v) if v else None) for k, v in self.psi.items()},
            "disk_free_gib": span(self.disk_free, 1024**3, 2),
            "oom_kills_in_runner_cgroup": None if oom_now is None or self._oom_start is None else oom_now - self._oom_start,
            "lock_waits": {
                "samples_with_waiters": self.lock_waiter_samples,
                "max_concurrent_waiters": self.lock_waiters_max,
                "max_age_of_waiting_statement_s": round(self.lock_wait_age_max_s, 2),
                "note": "age of the waiting statement is an upper bound on its wait, not the wait itself",
            },
            "samples": self.samples,
        }


INDEX_NAME = re.compile(r'"?(ix_[a-z0-9_]+)"?')


class StatementTimer:
    """Exact per-index build time from pg_stat_statements (utility statements keep their exact text), when it is installed.

    Time is the statement's wall-clock execution time, so it includes every wait the statement made for other transactions
    (the number an operator experiences). If the extension is unavailable the caller falls back to sampled progress and the
    report says so.
    """

    def __init__(self, admin_postgres_conninfo: str, database: str):
        self.conninfo, self.database = admin_postgres_conninfo, database

    def available(self) -> bool:
        try:
            with psycopg.connect(self.conninfo, autocommit=True) as conn:
                conn.execute("SELECT 1 FROM pg_stat_statements LIMIT 1")
            return True
        except psycopg.Error:
            return False

    def reset(self) -> bool:
        try:
            with psycopg.connect(self.conninfo, autocommit=True) as conn:
                conn.execute("SELECT pg_stat_statements_reset(0, (SELECT oid FROM pg_database WHERE datname = %s), 0)", (self.database,))
            return True
        except psycopg.Error:
            return False

    def read(self) -> dict[str, dict] | None:
        try:
            with psycopg.connect(self.conninfo, autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT query, calls, total_exec_time FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = %s) "
                    "AND (query ILIKE 'CREATE INDEX CONCURRENTLY%%' OR query ILIKE 'DROP INDEX%%')",
                    (self.database,),
                ).fetchall()
        except psycopg.Error:
            return None
        out: dict[str, dict] = {}
        for query, calls, total_ms in rows:
            match = INDEX_NAME.search(query)
            if match:
                kind = "create_concurrently" if query.upper().startswith("CREATE") else "drop"
                entry = out.setdefault(match.group(1), {})
                entry[kind] = {"calls": calls, "total_ms": round(total_ms, 1)}
        return out
