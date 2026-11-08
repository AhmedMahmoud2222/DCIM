# Phase 11 Edge Collector Security Remediation Plan

**Parent Tracking Issue**: `#38`
**Source Document**: `PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md`
**Current Known Main Commit**: `47141e83335ffef1d6eebf7d7ec0b34541f7d80d`
**Repository**: `AhmedMahmoud2222/DCIM`
**Document Scope**: Implementation roadmap, owner decision gates, work packages, delivery sequence, and ready-to-post child issue templates for the Phase 11 Edge Collector security assessment.

> **2026-10-14 reconciliation**: This plan predates the merge of PR #45/#49 (SEC-07) and PR #46 (SEC-05). Both are now resolved and merged to `main` — see the updated status rows and Work Packages 4 and 6 below. The baseline table and child-issue checklists below previously showed them as outstanding; this was stale and is corrected here. SEC-01/02/06 remain unresolved and owner-decision-gated.
>
> **Correction (same day, later pass)**: an earlier version of this note claimed PR #110 added SNMPv3 only to the central backend driver stack and left `edge_collector/` completely untouched. That was wrong — it was based on a truncated directory listing that missed several new files. PR #110 actually added genuine SNMPv3 USM auth/priv code *inside* `edge_collector/` itself: `snmp_usm.py` (USM auth/priv primitives), `snmp_wire.py` (shared BER/wire codec), `snmp_v3.py` (`SNMPv3Session`), plus `credentials.py` and `discovery_sessions.py` wiring. However, that new SNMPv3 support is wired exclusively into the **discovery/LLDP-CDP neighbor-walk path** (`discovery_sessions.py`, version-selected per credential-store entry) — `snmp_v2c.py`'s own docstring says so explicitly ("The original `SNMPv2cCollector` in `snmp.py` ... is unchanged"). The **telemetry metric-polling collector**, `SNMPv2cCollector.get()` in `edge_collector/snmp.py`, which is what SEC-04 is actually about, has no SNMPv3 variant and is not invoked from any `v3` session anywhere in the package (confirmed by searching every non-test file for `SNMPv2cCollector`/`SNMPv3`/`snmp_v3` usage). **SEC-04 is still open for the telemetry polling path**, but the owner-approved remediation (see Issue #38) will reuse the existing `snmp_usm.py`/`snmp_wire.py` primitives rather than duplicate them.

---

## Executive Summary

This document presents a structured, staged implementation plan to remediate the findings identified in `PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md` and tracked under parent issue `#38`.

The plan strictly distinguishes **independent low-risk hardening tasks and scoping work** from **owner decision-gated work packages** (which require owner approval on transaction semantics, KMS integration and key migration, SNMPv3 schema and migration policy, retention policies, and edge storage encryption).

---

## 1. Finding Revalidation & Baseline Status

All findings from the security assessment were revalidated against `main` at commit `25d17a4cbee805c1b0f8edb6904282212de11543`:

| Finding ID | Classification | Severity | Summary & Status |
|---|---|---|---|
| **SEC-01** | Hardening | **LOW** | **Partially changed, still open.** `ingest_collector_telemetry()` now wraps each record in `async with db.begin_nested():` (added incidentally by unrelated #128 G-series work, not as a SEC-01 fix) so every *known* domain rejection (`NOT_ASSIGNED`, `UNKNOWN_METRIC_MAPPING`, `REPLAY_HORIZON_EXCEEDED`, etc.) already isolates per-record. However, the function still issues one `await db.commit()` after the whole loop — a genuinely *unexpected* exception (e.g. a dropped DB connection) still propagates past that commit and discards the entire batch's pending work, which is the literal SEC-01 scenario. Owner decision on whole-batch vs. partial-batch persistence for *that* case remains required. |
| **SEC-02** | Deployment-Dependent / Architecture | **MEDIUM** | Fernet credential encryption key loading in `app/core/secrets.py`. `docs/OPERATIONS.md` item 4 currently prohibits mixed-key ciphertext during rotation under single-key operation. Key versioning (`v1:...`), keyring management, and online secret rotation represent an unresolved architecture and migration decision requiring owner approval. Unchanged since last review. |
| **SEC-03** | WITHDRAWN | **N/A** | Confirmed fully mitigated in `main`. `RESTDriver` (`app/application/drivers/rest.py`) enforces `network_policy.validate_target()`, deny-all default, and disables redirects. |
| **SEC-04** | Protocol Limitation | **MEDIUM** | Cleartext SNMP v2c community transmission on local wire in `edge_collector/snmp.py`'s `SNMPv2cCollector.get()` (telemetry metric polling). **Re-checked against PR #110** (merged): that PR added real SNMPv3 USM auth/priv code *inside* `edge_collector/` (`snmp_usm.py`, `snmp_wire.py`, `snmp_v3.py`), but wired it only into the discovery/LLDP-CDP neighbor-walk path (`discovery_sessions.py`), not the telemetry-polling collector. `SNMPv2cCollector.get()` has no SNMPv3 variant and nothing calls a v3 session for metric GETs. **Owner-approved 2026-10-14**: add genuine SNMPv3 USM authPriv support to the telemetry polling path, reusing `snmp_usm.py`/`snmp_wire.py` rather than duplicating them; keep SNMPv2c as an explicitly documented/configured legacy option; encrypted/authenticated wire-level loopback tests; fail-closed behavior. |
| **SEC-05** | Hardening | **LOW** | **RESOLVED.** Merged via PR #46 (`d3a18775474669f5c2aa79a270f756125cfeeb6a`): hourly Celery beat task, bounded batches with `FOR UPDATE SKIP LOCKED`, strict `<` cutoffs, disposable-PostgreSQL regression tests (`backend/tests/integration/test_collector_retention.py`). See `docs/SEC05_COLLECTOR_RETENTION.md`. No further action needed. |
| **SEC-06** | Deployment-Dependent | **MEDIUM** | Local Edge SQLite database stored unencrypted on disk (`edge_collector/queue.py`). Requires host LUKS / SQLCipher encryption guidance. Unchanged since last review — zero SQLCipher/LUKS/dm-crypt references anywhere in `edge_collector/` or `docs/`. |
| **SEC-07** | Hardening | **INFORMATIONAL** | **RESOLVED.** Merged via PR #45 (`7112cd2eccc1a42c0b2fa4b7932c9817ed49165f`) and PR #49 (`a1ba578cb5ae5d5fea0c9e56dc7c863e47ca6577`, closing the claim-release exception-leak gap found in independent review). `ingest_batch()`'s failure handlers log only fixed event names/codes; regression coverage in `backend/tests/api/test_collectors.py` and `test_m3_error_sanitization.py`. No further action needed. |

---

## 2. Work Packages & Acceptance Criteria

### Work Package 1: SEC-01 — Telemetry Batch Transaction Semantics
- **Target File**: `backend/app/api/v1/telemetry.py`, `ingest_collector_telemetry()`
- **Severity**: Low (Hardening Recommendation)
- **Verified Evidence**: `ingest_collector_telemetry()` loops through `body.records` calling `ingest_reading()`. Handled errors (`assignment.collector_id != collector.id` and `MetricMappingNotFound`) append structured ACK items (`NOT_ASSIGNED` / `UNKNOWN_METRIC_MAPPING`) without throwing exceptions. An unhandled exception on an intermediate record rolls back the entire transaction.
- **Owner Decision Required**: Decide whether `ingest_collector_telemetry()` should adopt nested savepoint isolation (`async with db.begin_nested():`) like `ingest_batch()` in `collectors.py` or retain whole-batch transaction atomicity.
- **Acceptance Criteria**:
  - If savepoints adopted: Per-record DB failures isolate to that record, returning `INTERNAL_PROCESSING_ERROR` in `TelemetryAck` while committing preceding valid readings.
  - If atomic batch retained: Document whole-batch rollback contract explicitly in API docs.
- **Negative Tests**: Submit a batch where record 2 triggers a database constraint error; verify behavior matches chosen contract.

---

### Work Package 2: SEC-02 — Key Versioning, KMS Integration & Secret Rotation
- **Target Files**: `backend/app/core/secrets.py`, `backend/app/api/v1/collectors.py`, `docs/OPERATIONS.md`
- **Severity**: Medium (Deployment-Dependent Risk)
- **Verified Evidence**: Single static Fernet key (`CREDENTIAL_ENCRYPTION_KEY`) in `.env`/settings. No key-versioning header or `POST /collectors/{id}/rotate-secret` endpoint. `docs/OPERATIONS.md` item 4 explicitly states that rotation is an offline batch re-encryption process and explicitly prohibits introducing mixed-key ciphertext while the application supports only one key.
- **Owner Decision Required**:
  1. Reconcile key-versioning architecture with existing operational guidance in `docs/OPERATIONS.md`: determine whether to replace offline single-key batch migration with online multi-key decryption and version headers (`v1:<ciphertext>`). Existing operational guidance does **not** support mixed-key operation.
  2. Choose KMS provider interface (AWS KMS, HashiCorp Vault, Azure Key Vault, or local environment fallback) and keyring rotation policy.
  3. Approve admin rotation endpoint design for collector secrets and integration credentials.
- **Acceptance Criteria**:
  - If key versioning approved by owner: `secrets.py` supports decrypting ciphertexts with legacy keys or key versions.
  - `POST /collectors/{id}/rotate-secret` endpoint introduced per approved architecture, invalidating old secret, issuing new secret, and writing audit/outbox events.
- **Negative Tests**: Decrypt a payload encrypted under an old key version; verify successful fallback decryption. Attempt secret rotation without `collector:manage` permission; verify 403.

---

### Work Package 3: SEC-04 — SNMPv3 USM Authentication & Privacy Support
- **Target Files**: `edge_collector/snmp.py`, `backend/app/domain/integration/models.py`, `backend/app/application/drivers/snmp.py`, Alembic DB migrations
- **Severity**: Medium (Architectural Limitation)
- **Verified Evidence**: `SNMPv2cCollector` sends plaintext community strings in BER headers over UDP port 161.
- **Owner Decision Required**:
  1. Approve database schema migration (Alembic) and ORM credential model updates required to distinguish existing SNMPv2c configurations (plaintext community string) from proposed SNMPv3 USM credentials (storing `username`, `auth_protocol` [MD5/SHA], `auth_key`, `priv_protocol` [DES/AES], `priv_key`). No credential schema or migration policy is assumed as pre-approved.
  2. Determine whether SNMPv2c remains supported as an opt-in legacy protocol alongside SNMPv3, and define backward-compatibility migration requirements.
- **Acceptance Criteria**:
  - Alembic schema migration correctly extends integration configuration storage to support SNMPv3 credentials while preserving existing SNMPv2c configurations.
  - `SNMPv3Collector` supports USM authNoPriv and authPriv (AES-128/SHA-1) GET requests.
  - Integration configuration validates SNMPv3 parameters securely.
- **Negative Tests**: Attempt SNMPv3 GET with invalid auth/priv keys; verify `SNMPAuthenticationError`.

---

### Work Package 4: SEC-05 — Bounded Nonce & Heartbeat Database Retention
- **Target Files**: `backend/app/domain/integration/models.py`, `backend/app/infrastructure/tasks/maintenance.py`
- **Severity**: Low (Hardening Recommendation)
- **Verified Evidence**: `CollectorRequestNonce` and `CollectorHeartbeat` tables grow monotonically without automated pruning. Immediate work-package planning and task structure design can proceed, but code execution of pruning requires explicit owner confirmation of retention thresholds.
- **Owner Policy Decision Required**:
  - Confirm or adjust proposed policy assumptions for default retention periods before code implementation:
    - Proposed `NONCE_RETENTION_SECONDS`: 3600s (1 hour) default; minimum required: > `REQUEST_TIMESTAMP_WINDOW_SECONDS` (300s).
    - Proposed `HEARTBEAT_RETENTION_DAYS`: 30 days default.
- **Acceptance Criteria**:
  - Celery maintenance task `prune_collector_nonces_and_heartbeats` runs on schedule using confirmed retention parameters.
  - Nonces older than `NONCE_RETENTION_SECONDS` are deleted.
  - Heartbeat records older than `HEARTBEAT_RETENTION_DAYS` are pruned.
- **Negative Tests**: Run pruning task with active nonces (<300s) and expired nonces (>1h); verify only expired nonces are deleted.

---

### Work Package 5: SEC-06 — Edge Appliance SQLite Storage Encryption
- **Target Files**: `edge_collector/queue.py` (`SQLiteQueue`), `edge_collector/config.py`
- **Severity**: Medium (Deployment-Dependent Risk)
- **Verified Evidence**: `SQLiteQueue` stores unacknowledged observations in unencrypted local SQLite database file `queue.db`.
- **Owner Decision Required**:
  - Choose between host OS transparent block-device encryption (LUKS/dm-crypt) vs application-level SQLCipher page encryption for `SQLiteQueue`.
- **Acceptance Criteria & Negative Tests by Architecture Option**:
  - **Option A — Host OS LUKS Encryption**:
    - *Acceptance Criteria*: Edge appliance volume is backed by a LUKS encrypted block device (`cryptsetup luksOpen`). Mounted filesystem access works transparently for `edge_collector`.
    - *Negative Test*: Direct raw block read of the underlying physical storage device (e.g. `head -c 4096 /dev/sda2`) contains random ciphertext and does **not** contain the SQLite magic header string `"SQLite format 3"`.
  - **Option B — Application SQLCipher Encryption**:
    - *Acceptance Criteria*: `SQLiteQueue` uses `sqlcipher` / `pysqlcipher3` with key injected via environment or secure key file (`PRAGMA key = '...'`).
    - *Negative Test*: Opening `queue.db` using standard un-keyed `sqlite3` driver or `sqlite3` CLI returns error `"file is not a database"` or header corruption error.

---

### Work Package 6: SEC-07 — Ingest Exception Logging Sanitization
- **Target Files**: `backend/app/api/v1/collectors.py`, `ingest_batch()`
- **Severity**: Informational (Hardening Recommendation)
- **Verified Evidence**: `ingest_batch()` logs `error=str(exc)` on unhandled exceptions, which could theoretically embed raw attribute string values from SQL error text.
- **Owner Decision Required**: None (Independent hardening task).
- **Acceptance Criteria**:
  - Exception messages passed to `logger.error()` in `ingest_batch()` are sanitized to redact potential raw database or payload input strings.
- **Negative Tests**: Trigger a database constraint error during ingest; verify structured log outputs generic error code and redacted message.

---

## 3. Delivery Sequence & Owner Decision Gates

```text
[Phase 1: Immediate Scoping & Independent Hardening] — COMPLETE
├── WP-05 (SEC-05): Nonce & Heartbeat Retention — RESOLVED, PR #46
└── WP-07 (SEC-07): Ingest Exception Logging Sanitization — RESOLVED, PR #45 + #49

[Phase 2: Owner Decision Gates] (Sequential) — OUTSTANDING
├── Gate 1: WP-01 (SEC-01) Telemetry Batch Transaction Semantics Decision (per-record savepoints now exist for known rejections; the unexpected-exception whole-batch-rollback case is still gated)
├── Gate 2: WP-02 (SEC-02) KMS & Key Versioning Architecture Approval (reconciling docs/OPERATIONS.md single-key constraint)
├── Gate 3: WP-04 (SEC-04) SNMPv3 USM Credential Schema, DB Migration & Compatibility Policy — OWNER APPROVED 2026-10-14 (reuse `edge_collector/snmp_usm.py` + `snmp_wire.py` from PR #110's discovery-path SNMPv3 work; extend to the telemetry-polling collector, which PR #110 did not touch)
└── Gate 4: WP-06 (SEC-06) Edge Storage Encryption Strategy (SQLCipher vs LUKS)
```

---

## 4. Ready-to-Post Child Issue Templates (Linked to #38)

### Child Issue 1: `[SEC-01] Telemetry Batch Ingestion Transaction Semantics (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-01
**Target File**: `backend/app/api/v1/telemetry.py` (`ingest_collector_telemetry()`)
**Severity**: Low (Hardening Recommendation)

### Summary
Re-evaluate and finalize telemetry batch transaction semantics in `ingest_collector_telemetry()`. Handled per-record domain rejections (`NOT_ASSIGNED`, `UNKNOWN_METRIC_MAPPING`) currently append structured ACK items. This task implements per-record nested transaction savepoints (`async with db.begin_nested():`) OR explicitly documents whole-batch transaction atomicity per owner decision.

### Acceptance Criteria
- [ ] Implement savepoint isolation OR document whole-batch rollback contract per owner decision.
- [ ] Add negative test for mid-batch database constraint failures.
```

### Child Issue 2: `[SEC-02] Fernet Key Versioning, KMS Integration & Secret Rotation (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-02
**Target Files**: `backend/app/core/secrets.py`, `backend/app/api/v1/collectors.py`, `docs/OPERATIONS.md`
**Severity**: Medium (Deployment-Dependent Risk)

### Summary
Reconcile and implement key versioning (`v1:<ciphertext>`), a pluggable KMS/Vault encryption provider interface, and an admin-authenticated collector secret rotation endpoint (`POST /collectors/{id}/rotate-secret`) per owner KMS architecture approval. Note: `docs/OPERATIONS.md` item 4 currently enforces single-key operation and prohibits mixed-key ciphertext; adopting key versioning and online secret rotation represents an architectural shift subject to owner approval.

### Acceptance Criteria
- [ ] Reconcile key management architecture with `docs/OPERATIONS.md` per owner approval.
- [ ] Add key versioning header prefix and legacy fallback decryption in `secrets.py`.
- [ ] Add `POST /collectors/{id}/rotate-secret` endpoint with `collector:manage` permission requirement.
- [ ] Audit and outbox events recorded on secret rotation.
```

### Child Issue 3: `[SEC-04] SNMPv3 USM Authentication & Privacy Encryption Support (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-04
**Target Files**: `edge_collector/snmp.py`, `backend/app/application/drivers/snmp.py`, `backend/app/domain/integration/models.py`, Alembic DB migrations
**Severity**: Medium (Architectural Limitation)

### Summary
Add SNMPv3 USM (`authNoPriv` / `authPriv` with AES-128 and SHA-1) support to `edge_collector/snmp.py` to eliminate cleartext community string transmission over UDP port 161 per owner credential schema decision. Includes database schema migration (Alembic) and compatibility layer to distinguish existing SNMPv2c configurations from SNMPv3 credentials.

### Acceptance Criteria
- [ ] Implement Alembic database migration and schema updates to store SNMPv3 credentials while maintaining backward compatibility with existing SNMPv2c configurations per owner policy.
- [ ] Implement `SNMPv3Collector` with USM user authentication and privacy encryption.
- [ ] Support structured JSON credentials for SNMPv3 integrations.
- [ ] Maintain backward compatibility for existing SNMPv2c integrations.
```

### Child Issue 4: `[SEC-05] Automated Nonce & Heartbeat Retention Pruning (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-05
**Target Files**: `backend/app/infrastructure/tasks/maintenance.py`
**Severity**: Low (Hardening Recommendation)

### Summary
Complete work-package planning and implement a Celery background maintenance task `prune_collector_nonces_and_heartbeats` to delete expired `CollectorRequestNonce` rows (>1 hour default, configurable via `NONCE_RETENTION_SECONDS`) and prune `CollectorHeartbeat` history (>30 days default, configurable via `HEARTBEAT_RETENTION_DAYS`), following owner confirmation of retention policies.

### Acceptance Criteria
- [x] Obtain owner confirmation for proposed retention policies (`NONCE_RETENTION_SECONDS` and `HEARTBEAT_RETENTION_DAYS`).
- [x] Implement `prune_collector_nonces_and_heartbeats` Celery beat task reading configurable retention settings.
- [x] Add unit and integration tests verifying pruning thresholds.

**Resolved** via PR #46 (`d3a18775474669f5c2aa79a270f756125cfeeb6a`). See `docs/SEC05_COLLECTOR_RETENTION.md`.
```

### Child Issue 5: `[SEC-06] Edge Collector Storage Encryption (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-06
**Target Files**: `edge_collector/queue.py`, `edge_collector/config.py`
**Severity**: Medium (Deployment-Dependent Risk)

### Summary
Provide edge appliance storage security by supporting host OS transparent disk encryption (LUKS/dm-crypt) OR application-level SQLCipher page encryption for local unacknowledged telemetry queue storage per owner edge decision.

### Acceptance Criteria & Negative Tests
- [ ] For LUKS option: Document LUKS deployment guidelines and verify raw block device does not contain SQLite header string `"SQLite format 3"`.
- [ ] For SQLCipher option: Add SQLCipher key support and verify opening `queue.db` without key fails with database format error.
```

### Child Issue 6: `[SEC-07] Ingest Exception Logging Sanitization (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-07
**Target Files**: `backend/app/api/v1/collectors.py`
**Severity**: Informational (Hardening Recommendation)

### Summary
Sanitize raw exception messages logged during `ingest_batch()` failures to ensure raw database or payload input strings are not written to central application logs.

### Acceptance Criteria
- [x] Sanitize `error` strings logged in `ingest_batch()`'s `except Exception:` handler.
- [x] Add regression test verifying redacted log output on ingest exceptions.

**Resolved** via PR #45 (`7112cd2eccc1a42c0b2fa4b7932c9817ed49165f`) and PR #49 (`a1ba578cb5ae5d5fea0c9e56dc7c863e47ca6577`, which closed a residual claim-release exception-leak path found in independent review).
```
