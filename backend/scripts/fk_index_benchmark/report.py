# ruff: noqa: E501
"""Environment fingerprint and the readiness report (JSON for machines, Markdown for people).

The classification line is fixed text. This framework measures one synthetic table on one machine; it can show that
invariants hold and give numbers to compare, but it cannot establish that production is ready, and a migration that completes
on this machine says nothing about the headroom (memory, disk, I/O, CPU) a production system has.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

import psycopg

from .sampler import ATTRIBUTION_NOTE as ATTRIBUTION_NOTE_FALLBACK
from .sampler import SCOPE_LABELS

CLASSIFICATION = "NOT YET VALIDATED"
CLASSIFICATION_REASON = (
    "These are measurements of a synthetic table on a single developer machine. They do not describe production hardware, "
    "storage, data distribution, concurrent workload or replication, and a migration that completes here does not show that a "
    "production system has the memory, disk, I/O or CPU headroom to run it. Re-run on a restored copy of production data (or a "
    "staging system of equivalent size and hardware) before any production rollout decision."
)
SETTINGS = (
    "server_version", "shared_buffers", "maintenance_work_mem", "max_parallel_maintenance_workers", "work_mem", "max_wal_size",
    "checkpoint_timeout", "synchronous_commit", "fsync", "wal_level", "effective_io_concurrency", "random_page_cost",
    "lock_timeout", "statement_timeout", "track_io_timing", "cluster_name",
)


def fingerprint(conn: psycopg.Connection) -> dict:
    settings = dict(conn.execute("SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name = ANY(%s)", (list(SETTINGS),)).fetchall())
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
    rate = f"{w['rows_per_second']} rows/s" if kind == "write" else f"{w['ops_per_second']} ops/s"
    return (
        f"p50 {w['p50_ms']:.1f} / p95 {w['p95_ms']:.1f} / p99 {w['p99_ms']:.1f} / max {w['max_ms']:.1f} ms "
        f"(completed={w['completed']}, failed={w['failed']}, {rate})"
    )


def _host(h: dict) -> list[str]:
    avail = h["availability"]
    missing = sorted(k for k, v in avail.items() if not v)
    lines = [f"  - Metric availability: unavailable = {missing or 'none'}. {h['scope_note']}"]
    if h["cpu_percent_vm"]:
        lines.append(f"  - CPU (VM-wide): mean {h['cpu_percent_vm']['mean']}%, max {h['cpu_percent_vm']['max']}%")
    if h["memory_available_mib"]:
        m = h["memory_available_mib"]
        lines.append(f"  - Memory available (MiB): start {m['start']}, min {m['min']}, end {m['end']}")
    if h["swap_used_mib"]:
        lines.append(f"  - Swap used (MiB): max {h['swap_used_mib']['max']}")
    if any(v is not None for v in h["pressure_some_avg10_max"].values()):
        lines.append(f"  - Pressure stall (some avg10, max): {json.dumps(h['pressure_some_avg10_max'])}")
    if h["disk_free_gib"]:
        d = h["disk_free_gib"]
        lines.append(
            f"  - Filesystem free (GiB, runner root fs): start {d['start']}, min {d['min']}, end {d['end']} "
            "(on Docker Desktop/WSL2 this is the VM's virtual disk and can greatly overstate free space on the host drive; check the host separately)"
        )
    lines.append(f"  - OOM kills in runner cgroup: {h['oom_kills_in_runner_cgroup']}")
    lw = h["lock_waits"]
    lines.append(
        f"  - Lock waits: samples with waiters {lw['samples_with_waiters']}, max concurrent {lw['max_concurrent_waiters']}, "
        f"max age of waiting statement {lw['max_age_of_waiting_statement_s']} s ({lw['note']})"
    )
    return lines


def _resources(run: dict) -> list[str]:
    """Resource deltas, each labelled with what it actually measures (instance-wide, database, host/VM)."""
    d = run["resources_delta"]
    scopes = run.get("resource_scopes", {})

    def tag(key: str) -> str:
        return f" [{SCOPE_LABELS.get(scopes.get(key, ''), 'scope not recorded')}]"

    lines = [
        f"- WAL written during the run: {_mb(d['wal_bytes'])}{tag('wal_bytes')}",
        f"- I/O during the run (pg_stat_io): read {_mb(d.get('io_read_bytes'))}, write {_mb(d.get('io_write_bytes'))}{tag('io_read_bytes')}",
        f"- Temp files: {_mb(d['temp_bytes'])}{tag('temp_bytes')}; database growth: {_mb(d['db_bytes'])}{tag('db_bytes')}; peak database size: {_mb(run['peak_database_bytes'])}{tag('db_bytes')}",
    ]
    if "other_databases_xacts" in d:
        lines.append(f"- Transactions in OTHER databases during the run: {d['other_databases_xacts']:,}{tag('other_databases_xacts')} (how much else the instance was doing)")
    lines.append(f"- Attribution: {run.get('attribution_note', ATTRIBUTION_NOTE_FALLBACK)}")
    return lines


def _cell(value) -> str:
    if isinstance(value, dict) and value.get("available") is False:
        return f"n/a ({value['reason']})"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _repeat_tables(name: str, res: dict) -> list[str]:
    details = res.get("repeat_details")
    if details is None:
        return [
            "- Per-repeat detail was NOT recorded in this report (it predates per-repeat retention). Only the last run's detail and the "
            "min/median/max values above exist; missing per-repeat figures are not reconstructed."
        ]
    lines: list[str] = []
    if name == "s1_s5":
        lines += [
            "",
            "| run | upgrade s | downgrade s | re-upgrade s | window | write p50 / p95 / p99 ms | writes completed / failed | rows/s | breaches |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for d in details:
            m = d["migration_seconds"]
            breaches = "; ".join(f"{b['metric']}={b['value']} (limit {b['limit']})" for b in d["threshold_breaches"]) or "none"
            for window, w in d["windows"].items():
                lines.append(
                    f"| {d['run']} | {_cell(m['upgrade'])} | {_cell(m['downgrade'])} | {_cell(m['reupgrade'])} | {window} | "
                    f"{_cell(w['p50_ms'])} / {_cell(w['p95_ms'])} / {_cell(w['p99_ms'])} | {_cell(w['writes_completed'])} / {_cell(w['writes_failed'])} | "
                    f"{_cell(w['write_rows_per_second'])} | {breaches if window == 'during_upgrade' else ''} |"
                )
    else:
        for d in details:
            extra = {k: v for k, v in d.items() if k not in ("run", "invariants", "threshold_breaches")}
            breaches = "; ".join(f"{b['metric']}={b['value']}" for b in d["threshold_breaches"]) or "none"
            lines.append(f"- run {d['run']}: {json.dumps(extra, default=str)}; breaches: {breaches}")
    return lines


def _builds(b: dict) -> list[str]:
    lines = [f"- Per-index timing source: {b['timing_source']}"]
    if b["exact_statement_times_ms"] is not None:
        timed = sorted(
            ((n, e["create_concurrently"]["total_ms"]) for n, e in b["exact_statement_times_ms"].items() if "create_concurrently" in e),
            key=lambda kv: -kv[1],
        )
        for name, ms in [t for t in timed if t[1] >= 10]:
            lines.append(f"  - {name}: {ms / 1000:.2f} s (statement wall time, includes waits)")
        small = [t for t in timed if t[1] < 10]
        if small:
            lines.append(f"  - {len(small)} further indexes each took under 10 ms (total {sum(t[1] for t in small):.1f} ms)")
    else:
        for name, x in b["sampled_large_table_indexes"].items():
            lines.append(f"  - {name}: observed {x['observed_seconds']} s, phases {json.dumps(x['phase_seconds'])}")
    return lines


def render_markdown(r: dict) -> str:
    ds, env = r["dataset"], r["environment"]
    lines = [
        f"# Migration 0057 benchmark: {ds['rows']:,} telemetry_reading rows",
        "",
        f"**Classification: {CLASSIFICATION}.** {CLASSIFICATION_REASON}",
        "",
        "## Dataset and environment",
        f"- Rows: telemetry_reading {ds['rows']:,}, daily_telemetry_aggregate_lineage {ds['lineage_rows']:,}, mappings {ds['mappings']:,}. "
        f"Synthetic, deterministic. Dataset mode: `{ds.get('mode', 'raw')}`. Other FK tables were left empty (their builds are metadata-scale).",
        f"- Profile: {json.dumps(ds.get('profile', {}))}",
        f"- {env['postgres_version']}",
        f"- Runner: {env['runner_platform']}, {env['runner_cpu_count']} CPUs, memory {_mb(env['runner_mem_total_bytes'])}",
        f"- Settings: {', '.join(f'{k}={v}' for k, v in env['postgres_settings'].items())}",
        f"- Target verification: {json.dumps(env.get('target_verification', 'not recorded (first-generation report)'))}",
        f"- Workload: {r['workload']}",
        "",
        "## Invariants (hard pass/fail)",
    ]
    for scenario, result in r["scenarios"].items():
        for name, ok in result.get("invariants", {}).items():
            lines.append(f"- [{'x' if ok else ' '}] {scenario}: {name}")
    breaches = r.get("threshold_breaches", [])
    lines += ["", "## Provisional acceptance-threshold breaches", f"- {r.get('thresholds_note', '')}"]
    lines += [f"- BREACH {b['scenario']} run {b.get('run', 1)}: {b['metric']} = {b['value']} (limit {b['limit']})" for b in breaches] or ["- none breached (this is not an acceptance)"]
    for name, res in r["scenarios"].items():
        if res.get("repeats", 1) > 1 or res.get("repeat_metrics") or res.get("repeat_details"):
            lines += ["", f"## Repeat runs: {name} (n={res.get('repeats', 1)})"]
            for label, m in res.get("repeat_metrics", {}).items():
                lines.append(f"- {label}: min {m['min']} / median {m['median']} / max {m['max']}  (values {m['values']})")
            lines += _repeat_tables(name, res)
    s1 = r["scenarios"].get("s1_s5")
    if s1:
        up = s1["upgrade_under_ingest"]
        lines += [
            "",
            f"## S1 upgrade under paced ingest (ingest mode: {s1.get('ingest_mode', 'raw')})",
            f"- Wall time: {up['wall_seconds']} s (exit {up['exit_code']})",
            f"- Ingest baseline (no migration): {_lat(s1['baseline_ingest'])}",
            f"- Ingest during upgrade: {_lat(up['ingest_during'])}",
            f"- Ingest after upgrade: {_lat(up['ingest_after'])}",
            f"- Read probe baseline / during: {_lat(s1['baseline_ingest'], 'read')} / {_lat(up['ingest_during'], 'read')}",
            *_resources(up),
            "- Lock modes seen on target tables: " + json.dumps(up["lock_modes_seen_on_target_tables"]),
            *_builds(up["builds"]),
            "- Host observations during the upgrade:",
            *_host(up["host_observations"]),
        ]
        if s1.get("ingest_errors_seen"):
            lines.append(f"- Ingest errors seen (first distinct): {json.dumps(s1['ingest_errors_seen'])}")
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
        if "overheads_seconds" in s1:
            o = s1["overheads_seconds"]
            lines.append(
                f"- Time split (S1+S5 scenario): migration {o['migration_alembic_seconds']} s of {o['scenario_total_seconds']} s total; "
                f"clone {o['database_clone_seconds']} s, cleanup {o['database_cleanup_seconds']} s, other (warm-up, probes, tails) {o['other_seconds']} s"
            )
    s2 = r["scenarios"].get("s2")
    if s2:
        up = s2["upgrade_idle"]
        sz = s2["sizes"]
        lines += [
            "",
            "## S2/S6 idle upgrade: time, WAL and size",
            f"- Wall time: {up['wall_seconds']} s",
            *_resources(up),
            *_builds(up["builds"]),
            "- New index sizes: " + ", ".join(f"{k}={_mb(v)}" for k, v in sz["new_indexes_bytes"].items() if v > 1024 * 1024),
            "- Table sizes: " + ", ".join(f"{k}={_mb(v)}" for k, v in sz["table_bytes"].items()),
            "- Host observations during the upgrade:",
            *_host(up["host_observations"]),
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
            if variant in ("invariants", "scenario_wall_seconds", "overheads_seconds", "repeats", "repeat_metrics", "repeat_details"):
                continue
            if v.get("status") == "not_run":
                lines.append(f"- {variant}: NOT RUN. {v['reason']}")
                continue
            lines.append(
                f"- {variant} ({v.get('mechanism', 'mechanism not recorded')}): interrupted-run exit {v['exit_code_of_interrupted_run']}, version {v['state_after_interrupt']['version']}, "
                f"invalid left {v['state_after_interrupt']['invalid_named']}, valid {v['state_after_interrupt']['valid_and_ready']}/22 right after; "
                f"re-run {v['rerun']['wall_seconds']} s (exit {v['rerun']['exit_code']}), {v['state_after_rerun']['valid_and_ready']}/22 valid"
            )
    lines += ["", "## Limitations", *[f"- {item}" for item in r["limitations"]], ""]
    return "\n".join(lines)


def limitations(dataset_mode: str, ingest_mode: str) -> tuple[str, ...]:
    items = [
        "Synthetic data: fixed row width and a parametric key distribution; real telemetry skew, bloat and update history are approximated only by the profile, which is not derived from production unless the operator supplies one.",
        "Single machine, single PostgreSQL instance, no replicas: replication lag, replica conflicts and WAL shipping are not measured.",
        "Per-index times are exact only when pg_stat_statements is available (the report says which source was used); otherwise they are 0.1 s samples. Host CPU, memory and pressure figures come from the client's kernel and are VM-wide in Docker Desktop/WSL2.",
        "Disk I/O throughput is not measured directly (PostgreSQL's pg_stat_io deltas are reported instead). Free space is the runner's root filesystem; under Docker Desktop/WSL2 that is a sparse virtual disk and does not reflect free space on the host drive, so host disk headroom must be checked separately.",
        "WAL and pg_stat_io figures are PostgreSQL instance-wide counters: they include autovacuum, checkpoints, other databases and any concurrent ingest, so they are upper bounds on the migration's cost, never migration-only values. Temp bytes and database growth are database-scoped and include every writer in that database.",
        "Cancellation is tested only through the protocol CancelRequest with the migration connection's own BackendKeyData (and by killing the alembic client process). PostgreSQL has no authenticated terminate, and pg_terminate_backend/pg_cancel_backend are PID-only with an unavoidable lookup-to-kill window, so no terminate scenario exists and no PID-only signalling is used. The 32-bit cancel key guards against recycled PIDs and accidents, not against an attacker on the server's network path.",
        "Cache state: clones are file copies, so the OS and shared-buffer caches start cold-ish and differ from a long-running system.",
        "Only the two large tables are populated; the other 19 FK tables are empty, so their (small) build cost is not measured.",
        "Offered ingest load is a fixed paced rate from one client process, not production's; latency under a different rate, batch shape or concurrency will differ.",
        "A migration that completes here does not establish that a production system has the memory, disk, I/O or CPU headroom to run it.",
    ]
    if dataset_mode == "raw" or ingest_mode == "raw":
        items.append("Raw mode: writers run with referential triggers disabled (session_replication_role=replica) as the admin role; foreign-key lookups on ingest and the application permission boundary are excluded.")
    if ingest_mode == "fk":
        items.append("fk ingest mode: raw SQL INSERTs as the application role with foreign keys enforced; this is not the application's code path.")
    if ingest_mode == "service":
        items.append("service ingest mode: the application's ingest_reading() through one process; it is not the HTTP API, authentication, rate limiting or the collector protocol.")
    if dataset_mode == "fk":
        items.append("fk dataset: managed_asset_id is left NULL and the other populated parents are minimal rows.")
    return tuple(items)


# Kept for the CI-sized harness tests, which render a report without going through the CLI.
LIMITATIONS = limitations("raw", "raw")
