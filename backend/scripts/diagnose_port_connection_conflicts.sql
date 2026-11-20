-- DCIM NET-INTEGRITY-01 — Read-Only Historical Endpoint Conflict Diagnostic
-- File: backend/scripts/diagnose_port_connection_conflicts.sql
--
-- PURPOSE
--   Detect pre-existing port_connection rows that violate the endpoint-exclusivity
--   invariant enforced by NET-INTEGRITY-01 (a port may participate in at most one
--   logical PortConnection, either as source_port_id or as target_port_id, regardless
--   of whether the connection is port-to-port or power-node).
--
-- SAFETY
--   This script is READ-ONLY. It performs no INSERT, UPDATE, DELETE, or DDL.
--   Run it against any database (including production) without risk of data
--   modification. No migration or data correction is performed.
--
-- USAGE
--   psql -U postgres -d <database> -f backend/scripts/diagnose_port_connection_conflicts.sql
--
-- SEVERITY CLASSIFICATION
--   P1 — Port participating as both source and target (cross-role occupancy).
--         Blocks future connect_port / create_cable operations on that port.
--   P2 — Multiple connections targeting the same port (fan-in).
--         Blocks future connect_port operations targeting that port.
--   P3 — Reversed duplicate connections (A->B and B->A coexist).
--         Semantically ambiguous; may cause trace/impact service confusion.
--   P4 — Power-node target conflicts (port is target of port-to-port AND source of power-node).
--         Same as P1 but specifically involving power-node relationships.
--   P5 — Duplicate cable ownership (multiple live cables referencing the same port_connection).
--         Cable lifecycle cannot safely realize or reconcile.
--
-- REMEDIATION STRATEGY (non-destructive, requires separate authorization)
--   1. Run this diagnostic on the target environment and review results.
--   2. For each conflicting relationship, determine the authoritative connection
--      (consult cable records, audit_log, and outbox_event for evidence).
--   3. Resolve conflicts manually or via an owner-authorized migration script
--      that preserves cable ownership, topology history, audit evidence, and data lineage.
--   4. Do NOT delete, reconnect, or rewrite historical relationships automatically.
--   5. Do NOT introduce a destructive migration without separate authorization.
--   6. If a pre-upgrade validation gate is needed, design it to report conflicts
--      safely (exit non-zero on P1/P2 findings) rather than silently modifying data.

-- ============================================================
-- 1. Summary counts
-- ============================================================
SELECT 'port_connection total rows' AS check_name, count(*) AS count FROM port_connection
UNION ALL
SELECT 'distinct source ports', count(DISTINCT source_port_id) FROM port_connection
UNION ALL
SELECT 'distinct target ports (non-null)', count(DISTINCT target_port_id) FROM port_connection WHERE target_port_id IS NOT NULL
UNION ALL
SELECT 'distinct power-node targets', count(DISTINCT target_power_node_id) FROM port_connection WHERE target_power_node_id IS NOT NULL
UNION ALL
SELECT 'power-node connections', count(*) FROM port_connection WHERE target_power_node_id IS NOT NULL
UNION ALL
SELECT 'port-to-port connections', count(*) FROM port_connection WHERE target_port_id IS NOT NULL;

-- ============================================================
-- 2. P1 — Ports participating as both source and target (cross-role occupancy)
-- ============================================================
SELECT
    'P1-cross-role' AS severity,
    pc1.source_port_id AS port_id,
    pc1.id AS connection_as_source,
    pc2.id AS connection_as_target,
    pc1.target_port_id AS source_conn_target,
    pc2.source_port_id AS target_conn_source
FROM port_connection pc1
JOIN port_connection pc2 ON pc1.source_port_id = pc2.target_port_id
WHERE pc1.source_port_id != pc2.source_port_id
ORDER BY pc1.source_port_id;

-- ============================================================
-- 3. P2 — Multiple connections targeting the same port (fan-in)
-- ============================================================
SELECT
    'P2-fan-in' AS severity,
    target_port_id AS port_id,
    count(*) AS connection_count,
    array_agg(id ORDER BY created_at) AS connection_ids,
    array_agg(source_port_id ORDER BY created_at) AS source_port_ids
FROM port_connection
WHERE target_port_id IS NOT NULL
GROUP BY target_port_id
HAVING count(*) > 1
ORDER BY count(*) DESC;

-- ============================================================
-- 4. P3 — Reversed duplicate connections (A->B and B->A coexist)
-- ============================================================
SELECT
    'P3-reversed' AS severity,
    a.source_port_id AS port_a,
    a.target_port_id AS port_b,
    a.id AS connection_ab,
    b.id AS connection_ba
FROM port_connection a
JOIN port_connection b ON a.source_port_id = b.target_port_id AND a.target_port_id = b.source_port_id
WHERE a.target_port_id IS NOT NULL
  AND a.source_port_id < a.target_port_id
ORDER BY a.source_port_id;

-- ============================================================
-- 5. P4 — Power-node target conflicts
--    Port is target of a port-to-port connection AND source of a power-node connection
-- ============================================================
SELECT
    'P4-power-node-conflict' AS severity,
    pc_port.source_port_id AS port_id,
    pc_port.id AS port_to_port_connection,
    pc_power.id AS power_node_connection,
    pc_power.target_power_node_id AS power_node_id
FROM port_connection pc_port
JOIN port_connection pc_power ON pc_port.target_port_id = pc_power.source_port_id
WHERE pc_port.target_port_id IS NOT NULL
  AND pc_power.target_power_node_id IS NOT NULL
  AND pc_port.source_port_id != pc_power.source_port_id
ORDER BY pc_port.target_port_id;

-- ============================================================
-- 6. P5 — Duplicate cable ownership
--    Multiple live cables referencing the same port_connection
-- ============================================================
SELECT
    'P5-duplicate-cable-ownership' AS severity,
    c.port_connection_id,
    count(*) AS live_cable_count,
    array_agg(c.id ORDER BY c.created_at) AS cable_ids,
    array_agg(c.label ORDER BY c.created_at) AS cable_labels
FROM cable c
WHERE c.is_live = true
  AND c.port_connection_id IS NOT NULL
GROUP BY c.port_connection_id
HAVING count(*) > 1
ORDER BY count(*) DESC;

-- ============================================================
-- 7. Conflicting port_connection rows that would block future operations
--    (ports that are both source and target across different rows)
-- ============================================================
SELECT
    'blocked-port' AS severity,
    p.id AS port_id,
    p.source_count,
    p.target_count,
    p.total_connections
FROM (
    SELECT
        COALESCE(s.port_id, t.port_id) AS id,
        COALESCE(s.cnt, 0) AS source_count,
        COALESCE(t.cnt, 0) AS target_count,
        COALESCE(s.cnt, 0) + COALESCE(t.cnt, 0) AS total_connections
    FROM (SELECT source_port_id AS port_id, count(*) AS cnt FROM port_connection GROUP BY source_port_id) s
    FULL OUTER JOIN (SELECT target_port_id AS port_id, count(*) AS cnt FROM port_connection WHERE target_port_id IS NOT NULL GROUP BY target_port_id) t
    ON s.port_id = t.port_id
) p
WHERE p.source_count > 0 AND p.target_count > 0
ORDER BY p.total_connections DESC;

-- ============================================================
-- 8. Overall conflict summary
-- ============================================================
SELECT
    'TOTAL CONFLICTS' AS check_name,
    (
        (SELECT count(*) FROM port_connection pc1 JOIN port_connection pc2 ON pc1.source_port_id = pc2.target_port_id WHERE pc1.source_port_id != pc2.source_port_id)
        + (SELECT count(*) FROM (SELECT target_port_id FROM port_connection WHERE target_port_id IS NOT NULL GROUP BY target_port_id HAVING count(*) > 1) x)
        + (SELECT count(*) FROM port_connection a JOIN port_connection b ON a.source_port_id = b.target_port_id AND a.target_port_id = b.source_port_id WHERE a.target_port_id IS NOT NULL AND a.source_port_id < a.target_port_id)
        + (SELECT count(*) FROM cable c WHERE c.is_live = true AND c.port_connection_id IS NOT NULL GROUP BY c.port_connection_id HAVING count(*) > 1)
    ) AS total_conflict_rows;