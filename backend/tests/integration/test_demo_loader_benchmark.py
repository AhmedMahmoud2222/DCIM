"""Throughput benchmark: sequential vs one-session-per-collector ingest on identical workloads.

Skipped unless DEMO_BENCH=1. Writes a JSON report to DEMO_BENCH_OUT (default /tmp/demo_loader_bench.json)."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.demo_loader import cli
from app.application.demo_loader.purge import purge
from app.application.demo_loader.selection import discover
from tests.demo_world import build_world
from tests.integration.test_demo_loader import args

pytestmark = pytest.mark.skipif(os.environ.get("DEMO_BENCH") != "1", reason="benchmark; set DEMO_BENCH=1")


def postgres_cpu_seconds() -> float:
    ticks = os.sysconf("SC_CLK_TCK")
    total = 0.0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/stat") as handle:
                raw = handle.read()
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            if comm.startswith("postgres"):
                fields = raw[raw.rindex(")") + 2:].split()
                total += (int(fields[11]) + int(fields[12])) / ticks
        except (OSError, ValueError):
            continue
    return total


async def measure(db, label, factory, as_of):
    commits = lambda: db.scalar(text("select xact_commit from pg_stat_database where datname = current_database()"))  # noqa: E731
    await db.commit()
    c0, cpu0, pg0, t0 = await commits(), time.process_time(), postgres_cpu_seconds(), time.perf_counter()
    result = await cli.run_apply(db, args(steps=["existing", "sensors", "devices"], as_of=as_of, session_factory=factory, no_verify=True,
                                          device_history_hours=6.0))
    elapsed = time.perf_counter() - t0
    await db.commit()
    parts = [result["existing"], result["sensors"]["telemetry"], result["devices"]["telemetry"]]
    accepted = sum(p["accepted"] for p in parts)
    return {
        "mode": label, "records_accepted": accepted, "rejected": sum(sum(p["rejected"].values()) for p in parts),
        "duplicates": sum(p["duplicate"] for p in parts), "elapsed_s": round(elapsed, 1), "records_per_s": round(accepted / elapsed, 1),
        "client_cpu_s": round(time.process_time() - cpu0, 1), "postgres_cpu_s": round(postgres_cpu_seconds() - pg0, 1),
        "db_transactions": await commits() - c0,
    }


async def test_benchmark(db_session, db_engine, make_user):
    await make_user("bench@example.com", "Passw0rd!Passw0rd", "Administrator")
    await build_world(db_session, racks_per_site=4, devices_per_rack=5)
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    as_of = datetime.now(UTC)
    runs = [await measure(db_session, "sequential", None, as_of)]
    await purge(db_session, await discover(db_session), dry_run=False)
    runs.append(await measure(db_session, "parallel (one session per collector)", factory, as_of))
    out = {"cpu_count": os.cpu_count(), "workload": "3 collectors; 9 existing + 24 sensor series x 289 slots + 120 device series x 73 slots", "runs": runs}
    Path(os.environ.get("DEMO_BENCH_OUT", "/tmp/demo_loader_bench.json")).write_text(json.dumps(out, indent=2))
    assert runs[0]["records_accepted"] == runs[1]["records_accepted"] and runs[0]["rejected"] == runs[1]["rejected"] == 0
