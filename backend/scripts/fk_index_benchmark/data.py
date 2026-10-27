# ruff: noqa: E501
"""Deterministic synthetic data for the 0057 benchmark. SYNTHETIC; scratch databases only.

Everything is derived from the row number (md5-derived UUIDs, modular arithmetic), so the same arguments give the same table.

Two load modes, kept separate because they measure different things:

* ``raw``: ``session_replication_role = replica`` (foreign-key triggers off). Fast to load; the parents of the readings other
  than mappings and revisions exist only as UUIDs. This is the mode of the first recorded runs.
* ``fk``: REAL parent rows exist for every foreign key that is populated (collector, integration, mapping, mapping revision,
  daily aggregate) and nothing is bypassed, so every insert pays its foreign-key checks. Rows are loaded as the application
  role. `managed_asset_id` is left NULL (it is nullable and its parent graph is not what 0057 indexes).

A ``Profile`` sets the shape: series count, key skew, retention window, pinned share, and churn (dead-tuple bloat). The default
profile is uniform. ``Profile.from_json`` loads a profile derived from real data, which is the path to production-like shape.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

import psycopg

ANCHOR = "2026-01-01 00:00:00+00"  # fixed instant so generated timestamps do not depend on when the load runs
CHUNK = 500_000
TARGET_TABLES = ("telemetry_reading", "daily_telemetry_aggregate_lineage")
MODES = ("raw", "fk")


@dataclass(frozen=True)
class Profile:
    name: str = "uniform-synthetic"
    series_count: int = 5000  # distinct series (devices)
    skew: float = 1.0  # 1.0 = uniform; > 1 concentrates readings on few series (series index = floor(N * u**skew))
    pinned_share: float = 0.8  # share of readings pinned to a mapping revision (the rest are NULL/NULL)
    retention_days: int = 90  # readings span this many days after the anchor
    churn_fraction: float = 0.0  # fraction of rows updated and deleted after load with autovacuum off (leaves dead tuples)
    integrations: int = 20
    collectors: int = 8

    @classmethod
    def from_json(cls, path: Path) -> Profile:
        raw = json.loads(Path(path).read_text())
        unknown = set(raw) - set(asdict(cls()))
        if unknown:
            raise ValueError(f"unknown profile fields: {sorted(unknown)}")
        profile = cls(**raw)
        profile.validate()
        return profile

    def validate(self) -> None:
        if not (0.0 <= self.pinned_share <= 1.0 and 0.0 <= self.churn_fraction < 0.5):
            raise ValueError("pinned_share must be in [0,1] and churn_fraction in [0,0.5)")
        if self.series_count < 1 or self.skew < 1.0 or self.retention_days < 1 or self.integrations < 1 or self.collectors < 1:
            raise ValueError("series_count, retention_days, integrations, collectors must be >= 1 and skew >= 1.0")


@dataclass(frozen=True)
class DatasetSpec:
    rows: int
    lineage_rows: int
    mappings: int = 2_000
    mode: str = "raw"
    profile: Profile = field(default_factory=Profile)

    @classmethod
    def for_rows(
        cls, rows: int, lineage_ratio: float = 0.1, mappings: int = 2_000, mode: str = "raw", profile: Profile | None = None
    ) -> DatasetSpec:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return cls(rows=rows, lineage_rows=max(1, int(rows * lineage_ratio)), mappings=mappings, mode=mode, profile=profile or Profile())

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:10]


def _log(message: str) -> None:
    print(f"[data {time.strftime('%H:%M:%S')}] {message}", flush=True)


def count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _u(expr: str, salt: str) -> str:
    """SQL for a 0..1 pseudo-random value derived from the row number (stable across runs)."""
    return f"(('x' || substr(md5('{salt}' || {expr}), 1, 8))::bit(32)::bigint / 4294967296.0)"


def _series_idx(p: Profile) -> str:
    return f"floor({p.series_count} * power({_u('g', 's')}, {p.skew}))::bigint"


def seed(conn: psycopg.Connection, spec: DatasetSpec) -> dict[str, int]:
    """Load `spec` into a database already at 0056. In `fk` mode `conn` must be the APPLICATION role's connection."""
    p, m = spec.profile, spec.mappings
    p.validate()
    raw = spec.mode == "raw"
    if raw:
        conn.execute("SET session_replication_role = replica")
    else:
        _seed_real_parents(conn, spec)
    conn.execute(
        "INSERT INTO integration_metric_mapping (id, integration_id, source_identifier, canonical_metric, unit) "
        "SELECT md5('m' || g)::uuid, md5('i' || (g %% %s))::uuid, 'src-' || g, 'power_kw', 'kW' FROM generate_series(1, %s::bigint) g",
        (p.integrations, m + 1),  # +1: the last mapping is the unreferenced "orphan" used by the parent-delete probe
    )
    conn.execute(
        "INSERT INTO integration_metric_mapping_revision (id, mapping_id, integration_id, source_identifier, revision, "
        "canonical_metric, source_unit, source_scale, conversion_hash, registry_version, provenance, effective_from, created_at) "
        "SELECT md5('rv' || g)::uuid, md5('m' || g)::uuid, md5('i' || (g %% %s))::uuid, 'src-' || g, 1, 'power_kw', 'kW', 1, %s, %s, "
        "'authored', %s::timestamptz, %s::timestamptz FROM generate_series(1, %s::bigint) g",
        (p.integrations, *_revision_contract(), ANCHOR, ANCHOR, m),
    )
    pinned = f"({_u('g', 'p')} < {p.pinned_share})"
    span = p.retention_days * 86400
    started = time.monotonic()
    for lo in range(1, spec.rows + 1, CHUNK):
        hi = min(lo + CHUNK - 1, spec.rows)
        conn.execute(
            "INSERT INTO telemetry_reading (id, collector_id, integration_id, mapping_id, mapping_revision_id, contract_evidence, "
            "external_identifier, series_key, dedup_key, metric, unit, value, occurred_at, received_at) "
            f"SELECT md5('r' || g)::uuid, md5('c' || (g %% {p.collectors}))::uuid, md5('i' || ((g %% {m} + 1) %% {p.integrations}))::uuid, "
            f"md5('m' || (g %% {m} + 1))::uuid, CASE WHEN {pinned} THEN md5('rv' || (g %% {m} + 1))::uuid END, CASE WHEN {pinned} THEN 'pinned' END, "
            f"'dev-' || {_series_idx(p)}, 'site/dev-' || {_series_idx(p)} || '/power_kw', 'd' || g, 'power_kw', 'kW', (g %% 1000) / 10.0, "
            f"%s::timestamptz + (g %% {span}) * interval '1 second', %s::timestamptz + (g %% {span}) * interval '1 second' "
            "FROM generate_series(%s::bigint, %s::bigint) g",
            (ANCHOR, ANCHOR, lo, hi),
        )
        _log(f"telemetry_reading {hi:,}/{spec.rows:,} ({time.monotonic() - started:,.0f}s)")
    _seed_lineage(conn, spec)
    if raw:
        conn.execute("RESET session_replication_role")
    if p.churn_fraction > 0:
        _churn(conn, spec)
    for table in ("integration_metric_mapping", "integration_metric_mapping_revision", *TARGET_TABLES):
        if table == "telemetry_reading" and p.churn_fraction > 0:
            conn.execute(f"ANALYZE {table}")  # ANALYZE only: VACUUM would remove the dead tuples the profile asks for
        else:
            conn.execute(f"VACUUM (ANALYZE) {table}")
    try:
        conn.execute("CHECKPOINT")
    except psycopg.errors.InsufficientPrivilege:
        pass  # role lacks pg_checkpoint; only affects how settled the template is
    return {
        "telemetry_reading": count(conn, "telemetry_reading"),
        "daily_telemetry_aggregate_lineage": count(conn, "daily_telemetry_aggregate_lineage"),
        "integration_metric_mapping": count(conn, "integration_metric_mapping"),
        "dead_tuples_telemetry_reading": dead_tuples(conn, "telemetry_reading"),
    }


def _revision_contract() -> tuple[str, str]:
    """(conversion_hash, registry_version) that the application's own registry computes for power_kw/kW at scale 1."""
    from app.domain.telemetry.contract import conversion_contract_hash
    from app.domain.telemetry.registry import REGISTRY_VERSION

    return conversion_contract_hash("power_kw", "kW", Decimal(1), REGISTRY_VERSION), REGISTRY_VERSION


def _seed_real_parents(conn: psycopg.Connection, spec: DatasetSpec) -> None:
    p = spec.profile
    conn.execute(
        "INSERT INTO collector (id, collector_type, name, secret_ciphertext, secret_rotated_at) "
        "SELECT md5('c' || g)::uuid, 'central', 'bench-collector-' || g, 'bench-not-a-secret', now() FROM generate_series(0, %s) g",
        (p.collectors - 1,),
    )
    conn.execute(
        "INSERT INTO integration (id, integration_type, name, target_host) "
        "SELECT md5('i' || g)::uuid, 'snmp', 'bench-integration-' || g, '192.0.2.' || (g %% 250 + 1) FROM generate_series(0, %s) g",
        (p.integrations - 1,),
    )


def _seed_lineage(conn: psycopg.Connection, spec: DatasetSpec) -> None:
    p, m = spec.profile, spec.mappings
    pinned = f"({_u('g', 'l')} < {p.pinned_share})"
    if spec.mode == "fk":  # lineage.aggregate_id is a real foreign key: create the daily aggregates it points at
        for lo in range(1, spec.lineage_rows + 1, CHUNK):
            hi = min(lo + CHUNK - 1, spec.lineage_rows)
            conn.execute(
                "INSERT INTO daily_telemetry_aggregate (id, integration_id, external_identifier, series_key, metric, unit, day, "
                "average_value, minimum_value, maximum_value, sample_count) "
                f"SELECT md5('a' || g)::uuid, md5('i' || (g %% {p.integrations}))::uuid, 'agg-' || g, 'agg/' || g, 'power_kw', 'kW', "
                "date '2026-01-01', 2, 1, 3, 3 FROM generate_series(%s::bigint, %s::bigint) g",
                (lo, hi),
            )
    for lo in range(1, spec.lineage_rows + 1, CHUNK):
        hi = min(lo + CHUNK - 1, spec.lineage_rows)
        conn.execute(
            "INSERT INTO daily_telemetry_aggregate_lineage (id, aggregate_id, contract_key, sample_count, first_occurred_at, "
            "last_occurred_at, mapping_revision_id, contract_evidence) "
            "SELECT md5('l' || g)::uuid, md5('a' || g)::uuid, 'k' || g, 1 + g %% 3, %s::timestamptz, %s::timestamptz + interval '1 hour', "
            f"CASE WHEN {pinned} THEN md5('rv' || (g %% {m} + 1))::uuid END, CASE WHEN {pinned} THEN 'pinned' END "
            "FROM generate_series(%s::bigint, %s::bigint) g",
            (ANCHOR, ANCHOR, lo, hi),
        )
    _log(f"daily_telemetry_aggregate_lineage {spec.lineage_rows:,}")


def _churn(conn: psycopg.Connection, spec: DatasetSpec) -> None:
    """Leave dead tuples behind: update and delete a fraction of rows with autovacuum off, and do NOT vacuum afterwards."""
    cut = int(spec.rows * spec.profile.churn_fraction)
    conn.execute("ALTER TABLE telemetry_reading SET (autovacuum_enabled = false)")
    conn.execute(
        "UPDATE telemetry_reading SET value = value + 1, attributes = '{\"churned\": true}' "
        "WHERE dedup_key IN (SELECT 'd' || g FROM generate_series(1, %s::bigint) g)",
        (cut,),
    )
    conn.execute(
        "DELETE FROM telemetry_reading WHERE dedup_key IN (SELECT 'd' || g FROM generate_series(%s::bigint, %s::bigint) g)",
        (cut + 1, 2 * cut),
    )
    _log(f"churn applied to ~{2 * cut:,} rows (autovacuum off, no vacuum afterwards)")


def dead_tuples(conn: psycopg.Connection, table: str) -> int:
    conn.execute("SELECT pg_stat_force_next_flush()")
    return conn.execute("SELECT coalesce(n_dead_tup, 0) FROM pg_stat_user_tables WHERE relname = %s", (table,)).fetchone()[0]


def orphan_mapping_id(conn: psycopg.Connection, spec: DatasetSpec):
    """The one mapping no reading or revision references (its delete exercises the RESTRICT checks on the new indexes)."""
    return conn.execute("SELECT md5('m' || %s)::uuid", (spec.mappings + 1,)).fetchone()[0]
