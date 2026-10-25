# ruff: noqa: E501  (long SQL, report text and f-strings are clearer unwrapped)
"""Environment fingerprint and the readiness report (JSON for machines, Markdown for people).

The classification line is fixed text. This framework measures one synthetic table on one machine; it can show that
invariants hold and give numbers to compare, but it cannot establish that production is ready.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

import psycopg

CLASSIFICATION = "NOT YET VALIDATED"
CLASSIFICATION_REASON = (
    "These are measurements of a synthetic table on a single developer machine. They do not describe production hardware, "
    "storage, data distribution, concurrent workload or replication. Re-run on a restored copy of production data (or a "
    "staging system of equivalent size and hardware) before any production rollout decision."
)
SETTINGS = (
    "server_version",
    "shared_buffers",
    "maintenance_work_mem",
    "max_parallel_maintenance_workers",
    "work_mem",
    "max_wal_size",
    "checkpoint_timeout",
    "synchronous_commit",
    "fsync",
    "wal_level",
    "effective_io_concurrency",
    "random_page_cost",
    "lock_timeout",
    "statement_timeout",
    "track_io_timing",
)


def fingerprint(conn: psycopg.Connection) -> dict:
    settings = dict(
        conn.execute(
            "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name = ANY(%s)", (list(SETTINGS),)
        ).fetchall()
    )
    mem_total = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) * 1024
    except OSError:
        pass
    cgroup_limit = None
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        cgroup_limit = None if raw == "max" else int(raw)
    except OSError:
        pass
    return {
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "postgres_version": conn.execute("SELECT version()").fetchone()[0],
        "postgres_settings": settings,
        "runner_platform": platform.platform(),
        "runner_cpu_count": os.cpu_count(),
        "runner_mem_total_bytes": mem_total,
        "runner_cgroup_memory_limit_bytes": cgroup_limit,
        "host_notes": os.environ.get("BENCH_HOST_NOTES", ""),
        "data_directory_storage": "container volume; NOT representative of production storage unless stated in host_notes",
    }


def write(out_dir: Path, report: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    json_path = out_dir / f"fk_index_benchmark_{report['dataset']['rows']}_{stamp}.json"
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(render_markdown(report))
    return json_path, md_path


def _mb(n) -> str:
    return "n/a" if n is None else f"{n / 1024 / 1024:,.1f} MiB"


def _lat(window: dict, kind: str = "write") -> str:
    w = window[kind]
    if w["count"] == 0:
        return f"no successful operations (failed={w['failed']})"
    return f"p50 {w['p50_ms']:.1f} / p95 {w['p95_ms']:.1f} / p99 {w['p99_ms']:.1f} / max {w['max_ms']:.1f} ms (n={w['count']}, failed={w['failed']})"


def render_markdown(r: dict) -> str:
    lines = [
        f"# Migration 0057 benchmark: {r['dataset']['rows']:,} telemetry_reading rows",
        "",
        f"**Classification: {CLASSIFICATION}.** {CLASSIFICATION_REASON}",
        "",
        "## Dataset and environment",
        f"- Rows: telemetry_reading {r['dataset']['rows']:,}, daily_telemetry_aggregate_lineage {r['dataset']['lineage_rows']:,}, "
        f"mappings {r['dataset']['mappings']:,}. Synthetic, deterministic. Other FK tables were left empty (their builds are metadata-scale).",
        f"- {r['environment']['postgres_version']}",
        f"- Runner: {r['environment']['runner_platform']}, {r['environment']['runner_cpu_count']} CPUs, memory {_mb(r['environment']['runner_mem_total_bytes'])}",
        f"- Settings: {', '.join(f'{k}={v}' for k, v in r['environment']['postgres_settings'].items())}",
        f"- Workload: {r['workload']}",
        "",
        "## Invariants (hard pass/fail)",
    ]
    for scenario, result in r["scenarios"].items():
        for name, ok in result.get("invariants", {}).items():
            lines.append(f"- [{'x' if ok else ' '}] {scenario}: {name}")
    s1 = r["scenarios"].get("s1_s5")
    if s1:
        up = s1["upgrade_under_ingest"]
        lines += [
            "",
            "## S1 upgrade under paced ingest",
            f"- Wall time: {up['wall_seconds']} s (exit {up['exit_code']})",
            f"- Ingest baseline (no migration): {_lat(s1['baseline_ingest'])}",
            f"- Ingest during upgrade: {_lat(up['ingest_during'])}",
            f"- Ingest after upgrade: {_lat(up['ingest_after'])}",
            f"- Read probe baseline / during: {_lat(s1['baseline_ingest'], 'read')} / {_lat(up['ingest_during'], 'read')}",
            f"- WAL generated: {_mb(up['resources_delta']['wal_bytes'])}; temp files: {_mb(up['resources_delta']['temp_bytes'])}; "
            f"database growth: {_mb(up['resources_delta']['db_bytes'])}; peak database size: {_mb(up['peak_database_bytes'])}",
            "- Lock modes seen on target tables: " + json.dumps(up["lock_modes_seen_on_target_tables"]),
            "- Large-table builds (sample-interval resolution):",
        ]
        for name, b in up["builds"]["large_table_indexes"].items():
            lines.append(f"  - {name}: observed {b['observed_seconds']} s, phases {json.dumps(b['phase_seconds'])}")
        d, a = s1["parent_delete_before"], s1["parent_delete_after"]
        lines += [
            "",
            "## S7 parent-delete referential checks (unreferenced mapping, rolled back)",
            f"- Before: median {d['median_execution_ms']} ms; after: median {a['median_execution_ms']} ms",
            f"- Per-trigger times, before: {json.dumps(d['repeats'][-1]['referential_check_ms'])}",
            f"- Per-trigger times, after: {json.dumps(a['repeats'][-1]['referential_check_ms'])}",
            "",
            "## S5 downgrade and re-upgrade under ingest",
            f"- Downgrade: {s1['downgrade_under_ingest']['wall_seconds']} s; ingest during: {_lat(s1['downgrade_under_ingest']['ingest_during'])}",
            f"- Re-upgrade: {s1['reupgrade_under_ingest']['wall_seconds']} s; ingest during: {_lat(s1['reupgrade_under_ingest']['ingest_during'])}",
        ]
    s2 = r["scenarios"].get("s2")
    if s2:
        up = s2["upgrade_idle"]
        sz = s2["sizes"]
        lines += [
            "",
            "## S2/S6 idle upgrade: time, WAL and size",
            f"- Wall time: {up['wall_seconds']} s; WAL {_mb(up['resources_delta']['wal_bytes'])}; temp {_mb(up['resources_delta']['temp_bytes'])}; "
            f"growth {_mb(up['resources_delta']['db_bytes'])}; peak {_mb(up['peak_database_bytes'])}",
            "- New index sizes: " + ", ".join(f"{k}={_mb(v)}" for k, v in sz["new_indexes_bytes"].items() if v > 1024 * 1024),
            "- Table sizes: " + ", ".join(f"{k}={_mb(v)}" for k, v in sz["table_bytes"].items()),
        ]
    s3 = r["scenarios"].get("s3")
    if s3:
        lines += [
            "",
            "## S3 old-snapshot blocker",
            f"- Held {s3['held_seconds']} s; phases seen: {s3['phases_seen_on_first_index']}; finish {s3['seconds_from_release_to_finish']} s after release",
            f"- Ingest while the build waited: {_lat(s3['ingest_while_build_waited'])}",
        ]
    s4 = r["scenarios"].get("s4")
    if s4:
        lines += ["", "## S4 cancellation and recovery"]
        for variant, v in s4.items():
            if variant in ("invariants", "scenario_wall_seconds"):
                continue
            lines.append(
                f"- {variant}: interrupted-run exit {v['exit_code_of_interrupted_run']}, version {v['state_after_interrupt']['version']}, "
                f"invalid left {v['state_after_interrupt']['invalid_named']}; re-run {v['rerun']['wall_seconds']} s "
                f"(exit {v['rerun']['exit_code']}), {v['state_after_rerun']['valid_and_ready']}/22 valid"
            )
    lines += ["", "## Limitations", *[f"- {item}" for item in r["limitations"]], ""]
    return "\n".join(lines)


LIMITATIONS = (
    "Synthetic data: uniform value and key distributions, one pinned/unpinned ratio, fixed row width; real telemetry skew, bloat and "
    "update history are absent.",
    "Single machine, single PostgreSQL instance, no replicas: replication lag, replica conflicts and WAL shipping are not measured.",
    "Writers run with referential triggers disabled (session_replication_role=replica); foreign-key lookups on ingest are excluded.",
    "Per-index times come from 0.1 s progress sampling; builds shorter than that are not listed. Host CPU is not sampled.",
    "Cache state: clones are file copies, so the OS and shared-buffer caches start cold-ish and differ from a long-running system.",
    "Only the two large tables are populated; the other 19 FK tables are empty, so their (small) build cost is not measured.",
    "Offered ingest load is a fixed paced rate, not production's; latency under a different rate or batch shape will differ.",
)
