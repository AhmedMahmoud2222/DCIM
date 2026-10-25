# ruff: noqa: E501  (long SQL, report text and f-strings are clearer unwrapped)
"""Deterministic synthetic data for the 0057 benchmark. SYNTHETIC; scratch databases only.

Everything is derived from the row number (md5-based UUIDs, modular arithmetic), so two runs with the same arguments
produce the same table. Loading uses `session_replication_role = replica` so referential triggers do not slow a
multi-million-row load: the parent rows that matter to the benchmark (`integration_metric_mapping` and its revisions, the
parents of the two indexed `telemetry_reading` columns) are real rows; the remaining parents (collector, integration,
managed asset) are referenced by UUID only. CHECK and UNIQUE constraints are enforced throughout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg

ANCHOR = "2026-01-01 00:00:00+00"  # fixed instant so generated timestamps do not depend on when the load runs
CHUNK = 500_000
PINNED_SHARE = 5  # 4 of every 5 rows carry a pinned revision (mapping_revision_id + contract_evidence), 1 of 5 is NULL/NULL
TARGET_TABLES = ("telemetry_reading", "daily_telemetry_aggregate_lineage")


@dataclass(frozen=True)
class DatasetSpec:
    rows: int
    lineage_rows: int
    mappings: int = 2_000

    @classmethod
    def for_rows(cls, rows: int, lineage_ratio: float = 0.1, mappings: int = 2_000) -> DatasetSpec:
        return cls(rows=rows, lineage_rows=max(1, int(rows * lineage_ratio)), mappings=mappings)


def _log(message: str) -> None:
    print(f"[data {time.strftime('%H:%M:%S')}] {message}", flush=True)


def seed(conn: psycopg.Connection, spec: DatasetSpec) -> dict[str, int]:
    """Load `spec` into a database that is already at 0056. `conn` must be a superuser connection in autocommit mode."""
    conn.execute("SET session_replication_role = replica")
    m = spec.mappings
    conn.execute(
        "INSERT INTO integration_metric_mapping (id, integration_id, source_identifier, canonical_metric, unit) "
        "SELECT md5('m' || g)::uuid, md5('i' || (g %% 20))::uuid, 'src-' || g, 'power_kw', 'kW' "
        "FROM generate_series(1, %s::bigint) g",
        (m + 1,),  # +1: the last mapping is the unreferenced "orphan" used by the parent-delete probe
    )
    conn.execute(
        "INSERT INTO integration_metric_mapping_revision (id, mapping_id, integration_id, source_identifier, revision, "
        "canonical_metric, source_unit, source_scale, conversion_hash, provenance, effective_from, created_at) "
        "SELECT md5('rv' || g)::uuid, md5('m' || g)::uuid, md5('i' || (g %% 20))::uuid, 'src-' || g, 1, 'power_kw', 'kW', 1, "
        "md5('h1' || g) || md5('h2' || g), 'authored', %s::timestamptz, %s::timestamptz FROM generate_series(1, %s::bigint) g",
        (ANCHOR, ANCHOR, m),
    )
    started = time.monotonic()
    for lo in range(1, spec.rows + 1, CHUNK):
        hi = min(lo + CHUNK - 1, spec.rows)
        conn.execute(
            "INSERT INTO telemetry_reading (id, collector_id, integration_id, mapping_id, mapping_revision_id, "
            "contract_evidence, external_identifier, series_key, dedup_key, metric, unit, value, occurred_at, received_at) "
            "SELECT md5('r' || g)::uuid, md5('c' || (g %% 8))::uuid, md5('i' || (g %% 20))::uuid, "
            f"md5('m' || (g %% {m} + 1))::uuid, "
            f"CASE WHEN g %% {PINNED_SHARE} <> 0 THEN md5('rv' || (g %% {m} + 1))::uuid END, "
            f"CASE WHEN g %% {PINNED_SHARE} <> 0 THEN 'pinned' END, "
            "'dev-' || (g %% 5000), 'site/dev-' || (g %% 5000) || '/power_kw', 'd' || g, 'power_kw', 'kW', (g %% 1000) / 10.0, "
            "%s::timestamptz + (g %% 7776000) * interval '1 second', %s::timestamptz + (g %% 7776000) * interval '1 second' "
            "FROM generate_series(%s::bigint, %s::bigint) g",
            (ANCHOR, ANCHOR, lo, hi),
        )
        _log(f"telemetry_reading {hi:,}/{spec.rows:,} ({time.monotonic() - started:,.0f}s)")
    for lo in range(1, spec.lineage_rows + 1, CHUNK):
        hi = min(lo + CHUNK - 1, spec.lineage_rows)
        conn.execute(
            "INSERT INTO daily_telemetry_aggregate_lineage (id, aggregate_id, contract_key, sample_count, "
            "first_occurred_at, last_occurred_at, mapping_revision_id, contract_evidence) "
            "SELECT md5('l' || g)::uuid, md5('a' || g)::uuid, 'k' || g, 1 + g %% 50, %s::timestamptz, "
            f"%s::timestamptz + interval '1 hour', CASE WHEN g %% {PINNED_SHARE} <> 0 THEN md5('rv' || (g %% {m} + 1))::uuid END, "
            f"CASE WHEN g %% {PINNED_SHARE} <> 0 THEN 'pinned' END FROM generate_series(%s::bigint, %s::bigint) g",
            (ANCHOR, ANCHOR, lo, hi),
        )
    _log(f"daily_telemetry_aggregate_lineage {spec.lineage_rows:,}")
    conn.execute("RESET session_replication_role")
    for table in ("integration_metric_mapping", "integration_metric_mapping_revision", *TARGET_TABLES):
        conn.execute(f"VACUUM (ANALYZE) {table}")
    conn.execute("CHECKPOINT")
    return {
        "telemetry_reading": count(conn, "telemetry_reading"),
        "daily_telemetry_aggregate_lineage": count(conn, "daily_telemetry_aggregate_lineage"),
        "integration_metric_mapping": count(conn, "integration_metric_mapping"),
    }


def count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def orphan_mapping_id(conn: psycopg.Connection, spec: DatasetSpec):
    """The one mapping no reading or revision references (its delete exercises the RESTRICT checks on the new indexes)."""
    return conn.execute("SELECT md5('m' || %s)::uuid", (spec.mappings + 1,)).fetchone()[0]
