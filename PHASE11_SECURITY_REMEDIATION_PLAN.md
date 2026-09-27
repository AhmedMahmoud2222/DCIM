# Phase 11 Edge Collector Security Remediation Plan

**Parent Tracking Issue**: `#38`
**Source Document**: `PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md`
**Current Known Main Commit**: `8d1793e161864ef804b4f3e0268177bff82b483e`
**Repository**: `AhmedMahmoud2222/DCIM`
**Document Scope**: Implementation roadmap, owner decision gates, work packages, delivery sequence, and ready-to-post child issue templates for the Phase 11 Edge Collector security assessment.

---

## Executive Summary

This document presents a structured, staged implementation plan to remediate the findings identified in `PHASE11_EDGE_COLLECTOR_SECURITY_ASSESSMENT.md` and tracked under parent issue `#38`.

The plan strictly distinguishes **independent low-risk hardening tasks** (which can proceed immediately without API or architecture changes) from **owner decision-gated work packages** (which require owner approval on transaction semantics, KMS integration, SNMPv3 migration, and disk encryption).

---

## 1. Finding Revalidation & Baseline Status

All findings from the security assessment were revalidated against `main` at commit `8d1793e161864ef804b4f3e0268177bff82b483e`:

| Finding ID | Classification | Severity | Summary & Status |
|---|---|---|---|
| **SEC-01** | Hardening | **LOW** | Telemetry batch ingestion transaction semantics in `app/api/v1/telemetry.py`. Handled domain rejections (`NOT_ASSIGNED`, `UNKNOWN_METRIC_MAPPING`) work as designed. Owner decision required on nested savepoints vs whole-batch rollback. |
| **SEC-02** | Deployment-Dependent / Architecture | **MEDIUM** | Fernet credential encryption key loading in `app/core/secrets.py`. Requires key versioning (`v1:...`), KMS provider interface, and secret rotation API. |
| **SEC-03** | WITHDRAWN | **N/A** | Confirmed fully mitigated in `main`. `RESTDriver` (`app/application/drivers/rest.py`) enforces `network_policy.validate_target()`, deny-all default, and disables redirects. |
| **SEC-04** | Protocol Limitation | **MEDIUM** | Cleartext SNMP v2c community transmission on local wire in `edge_collector/snmp.py`. Requires SNMPv3 USM (`authPriv`) support. |
| **SEC-05** | Hardening | **LOW** | Monotonic row insertion in `collector_request_nonce` and `collector_heartbeat`. Requires Celery retention pruning task. |
| **SEC-06** | Deployment-Dependent | **MEDIUM** | Local Edge SQLite database stored unencrypted on disk (`edge_collector/queue.py`). Requires host LUKS / SQLCipher encryption guidance. |
| **SEC-07** | Hardening | **INFORMATIONAL** | Exception message logging in `app/api/v1/collectors.py`. Requires logging sanitization for raw exception details. |

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
- **Target Files**: `backend/app/core/secrets.py`, `backend/app/api/v1/collectors.py`
- **Severity**: Medium (Deployment-Dependent Risk)
- **Verified Evidence**: Single static Fernet key (`CREDENTIAL_ENCRYPTION_KEY`) in `.env`/settings. No key-versioning header or `POST /collectors/{id}/rotate-secret` endpoint.
- **Owner Decision Required**:
  1. Approve key versioning prefix format (`v1:<ciphertext>`).
  2. Choose KMS provider interface (AWS KMS, HashiCorp Vault, Azure Key Vault, or local environment fallback).
  3. Approve admin rotation endpoint design for collector secrets and integration credentials.
- **Acceptance Criteria**:
  - `secrets.py` supports decrypting ciphertexts with legacy keys or key versions.
  - `POST /collectors/{id}/rotate-secret` invalidates old secret, issues new secret, and writes audit/outbox events.
- **Negative Tests**: Decrypt a payload encrypted under an old key version; verify successful fallback decryption. Attempt secret rotation without `collector:manage` permission; verify 403.

---

### Work Package 3: SEC-04 — SNMPv3 USM Authentication & Privacy Support
- **Target Files**: `edge_collector/snmp.py`, `backend/app/domain/integration/models.py`, `backend/app/application/drivers/snmp.py`
- **Severity**: Medium (Architectural Limitation)
- **Verified Evidence**: `SNMPv2cCollector` sends plaintext community strings in BER headers over UDP port 161.
- **Owner Decision Required**:
  1. Approve SNMPv3 credential payload structure (JSON payload storing `username`, `auth_protocol` [MD5/SHA], `auth_key`, `priv_protocol` [DES/AES], `priv_key`).
  2. Determine whether SNMPv2c remains supported as an opt-in legacy protocol alongside SNMPv3.
- **Acceptance Criteria**:
  - `SNMPv3Collector` supports USM authNoPriv and authPriv (AES-128/SHA-1) GET requests.
  - Integration configuration validates SNMPv3 parameters securely.
- **Negative Tests**: Attempt SNMPv3 GET with invalid auth/priv keys; verify `SNMPAuthenticationError`.

---

### Work Package 4: SEC-05 — Bounded Nonce & Heartbeat Database Retention
- **Target Files**: `backend/app/domain/integration/models.py`, `backend/app/infrastructure/tasks/maintenance.py`
- **Severity**: Low (Hardening Recommendation)
- **Verified Evidence**: `CollectorRequestNonce` and `CollectorHeartbeat` tables grow monotonically without automated pruning.
- **Owner Decision Required**:
  - Review proposed default retention durations and approve owner-configurable policy settings:
    - `NONCE_RETENTION_SECONDS` (default: 3600s / 1 hour; minimum required: > `REQUEST_TIMESTAMP_WINDOW_SECONDS` [300s]).
    - `HEARTBEAT_RETENTION_DAYS` (default: 30 days).
- **Acceptance Criteria**:
  - Celery maintenance task `prune_collector_nonces_and_heartbeats` runs on schedule using configured parameters.
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
[Phase 1: Independent Hardening] (Immediate)
├── WP-05 (SEC-05): Nonce & Heartbeat Retention Task (with configurable retention settings)
└── WP-07 (SEC-07): Ingest Exception Logging Sanitization

[Phase 2: Owner Decision Gates] (Sequential)
├── Gate 1: WP-01 (SEC-01) Telemetry Batch Transaction Semantics Decision
├── Gate 2: WP-02 (SEC-02) KMS & Secret Rotation Architecture Approval
├── Gate 3: WP-04 (SEC-04) SNMPv3 USM Credential Schema & Migration Policy
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
**Target Files**: `backend/app/core/secrets.py`, `backend/app/api/v1/collectors.py`
**Severity**: Medium (Deployment-Dependent Risk)

### Summary
Implement key versioning (`v1:<ciphertext>`), a pluggable KMS/Vault encryption provider interface, and an admin-authenticated collector secret rotation endpoint (`POST /collectors/{id}/rotate-secret`) per owner KMS architecture approval.

### Acceptance Criteria
- [ ] Add key versioning header prefix and legacy fallback decryption in `secrets.py`.
- [ ] Add `POST /collectors/{id}/rotate-secret` endpoint with `collector:manage` permission requirement.
- [ ] Audit and outbox events recorded on secret rotation.
```

### Child Issue 3: `[SEC-04] SNMPv3 USM Authentication & Privacy Encryption Support (#38)`
```markdown
**Parent Issue**: #38
**Finding ID**: SEC-04
**Target Files**: `edge_collector/snmp.py`, `backend/app/application/drivers/snmp.py`
**Severity**: Medium (Architectural Limitation)

### Summary
Add SNMPv3 USM (`authNoPriv` / `authPriv` with AES-128 and SHA-1) support to `edge_collector/snmp.py` to eliminate cleartext community string transmission over UDP port 161 per owner credential schema decision.

### Acceptance Criteria
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
Add a Celery background maintenance task `prune_collector_nonces_and_heartbeats` to delete expired `CollectorRequestNonce` rows (>1 hour default, configurable via `NONCE_RETENTION_SECONDS`) and prune `CollectorHeartbeat` history (>30 days default, configurable via `HEARTBEAT_RETENTION_DAYS`).

### Acceptance Criteria
- [ ] Implement `prune_collector_nonces_and_heartbeats` Celery beat task reading configurable retention settings.
- [ ] Add unit and integration tests verifying pruning thresholds.
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
- [ ] Sanitize `error` strings logged in `ingest_batch()`'s `except Exception:` handler.
- [ ] Add regression test verifying redacted log output on ingest exceptions.
```
