# Phase 11 Edge Collector Security Threat Model & Risk Assessment

**Status**: Hold until PR #29 merges
**Baseline Commit SHA**: `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`
**Repository**: `AhmedMahmoud2222/DCIM`
**Target Branch**: `main`
**Document Scope**: Independent, evidence-based security threat model and risk assessment of the Edge Collector runtime (`edge_collector/`) and Central DCIM collector trust boundary (`app/application/collector_auth.py`, `app/api/v1/collectors.py`, `app/core/secrets.py`).

---

## Executive Summary

This document provides a formal, evidence-based security threat model evaluating the Edge Collector runtime, protocol drivers, local SQLite storage, HMAC-SHA256 machine authentication, credential encryption, and multi-site isolation boundaries. Assessment was conducted via direct source inspection against `main` at commit `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`.

All findings distinguish **confirmed codebase facts in `main`** from pending PR #29 features. No code or configuration changes were executed in this branch.

---

## 1. Assessment Methodology & Scope

The security review evaluated six primary threat surfaces:
1. **Collector Registration, Authentication, Authorization & Assignment Boundaries**
2. **Credential Storage & Encryption Key Lifecycle**
3. **Network Target Allowlists, SNMP Protocol Handling, SSRF & Lateral Movement**
4. **Offline Queue Persistence, Replay Protection, Deduplication & Delivery ACKs**
5. **Collector Compromise Scenarios & Multi-Site / Customer Environment Isolation**
6. **Sensitive Data Logging, Key Rotation, Observability & Incident Response**

---

## 2. Detailed Threat Analysis & Findings

### 2.1 Domain 1: Registration, Authentication, Authorization & Assignment Boundaries

#### Finding SEC-01: Telemetry Batch Ingest Lacks Nested Transaction Savepoint Isolation
- **Affected Component**: `backend/app/api/v1/telemetry.py`, `ingest_collector_telemetry()`
- **Severity**: **MEDIUM**
- **Attack Preconditions**: An authenticated edge collector submits a telemetry batch containing multiple records where an unexpected database or validation error occurs on an intermediate record.
- **Technical Evidence**:
  Unlike `ingest_batch()` in `app/api/v1/collectors.py` (which uses `async with db.begin_nested():` savepoints per record), `ingest_collector_telemetry()` loops over `body.records` without per-record transaction savepoints. An unhandled exception on record *N* causes the entire endpoint transaction to abort and roll back, discarding previously processed valid records (*1..N-1*) and returning HTTP 500 rather than partial ACKs.
- **Existing Mitigation**:
  Per-record assignment check (`assignment.collector_id != collector.id`) produces a `NOT_ASSIGNED` rejection status without throwing an exception. `MetricMappingNotFound` is caught and returned as `UNKNOWN_METRIC_MAPPING`.
- **Residual Risk**:
  Transient DB failures or unexpected schema constraints on one reading cause full batch rejections and edge queue re-delivery loops.
- **Testable Remediation Recommendation**:
  Wrap each record in `ingest_collector_telemetry()` inside an `async with db.begin_nested():` savepoint, releasing claims and logging errors per record on failure.

---

### 2.2 Domain 2: Credential Storage & Encryption-Key Lifecycle

#### Finding SEC-02: Single Static Fernet Encryption Key Without KMS or Secret Rotation Endpoint
- **Affected Component**: `backend/app/core/secrets.py` (`encrypt_secret()`, `decrypt_secret()`), `backend/app/api/v1/collectors.py`
- **Severity**: **HIGH**
- **Attack Preconditions**:
  Environment file `.env` or process environment is compromised, exposing `CREDENTIAL_ENCRYPTION_KEY`.
- **Technical Evidence**:
  `secrets.py` initializes `Fernet(settings.credential_encryption_key.encode())`. In `main`:
  1. The key is static and loaded directly from environment variables.
  2. No key-versioning header or multi-key decryption fallback exists.
  3. No central API route exists for rotating collector secrets (`Collector.secret_ciphertext`) or re-encrypting integration credentials (`Integration.credential_ciphertext`).
- **Existing Mitigation**:
  Credentials are encrypted at rest using AES-128-CBC + HMAC-SHA256 (Fernet) rather than stored in plaintext. `CollectorRegisterOut` displays the generated raw secret exactly once upon initial registration.
- **Residual Risk**:
  Compromise of `CREDENTIAL_ENCRYPTION_KEY` exposes all stored integration credentials (SNMP community strings, REST API keys) across all sites.
- **Testable Remediation Recommendation**:
  Introduce a KMS envelope encryption primitive or key-versioning header (`v1:...`) in `secrets.py`, and implement an admin-authenticated `POST /collectors/{id}/rotate-secret` endpoint with key rotation re-encryption utilities.

---

### 2.3 Domain 3: Target Allowlists, SNMP Protocol Handling, SSRF & Lateral Movement

#### Finding SEC-03: Central REST Driver Lacks Egress Network Policy Filtering
- **Affected Component**: `backend/app/application/drivers/rest.py`, `RESTDriver.poll()`
- **Severity**: **HIGH**
- **Attack Preconditions**:
  An administrator configures an `Integration` with `target_host` pointing to internal management endpoints (`169.254.169.254`, `localhost`, or internal subnet addresses).
- **Technical Evidence**:
  `edge_collector/snmp.py` explicitly enforces `SNMPTargetPolicy` (`_validate_target()` checking loopback, multicast, and allowed networks). Conversely, central's `RESTDriver` (`app/application/drivers/rest.py`) initializes `httpx.AsyncClient(verify=True)` directly against `target_host` without IP range validation or loopback blocking.
- **Existing Mitigation**:
  Requires `integration:manage` RBAC permission to create or update an Integration target URL.
- **Residual Risk**:
  An authorized user with `integration:manage` could abuse central polling to perform SSRF port scanning or query cloud metadata services (`169.254.169.254`).
- **Testable Remediation Recommendation**:
  Apply egress IP target validation in `RESTDriver` using the same explicit network/loopback policy enforcement implemented in `edge_collector/snmp.py`.

#### Finding SEC-04: Cleartext Transmission of SNMP v2c Community Strings Over Local Network
- **Affected Component**: `edge_collector/snmp.py`, `SNMPv2cCollector.get()`
- **Severity**: **MEDIUM**
- **Attack Preconditions**:
  An attacker has network sniffing access (e.g. SPAN port or unencrypted LAN) on the local data center segment between Edge Collector and monitored network devices.
- **Technical Evidence**:
  `SNMPv2cCollector` implements standard SNMP v2c GET requests (`_build_get_request()`), transmitting the plaintext community string in BER header tag `0x04`.
- **Existing Mitigation**:
  SNMP community strings are encrypted at rest on central (`credential_ciphertext`) and scrubbed from logs via `_redact_sensitive`.
- **Residual Risk**:
  Eavesdroppers on the local network segment can intercept read-only SNMP community strings.
- **Testable Remediation Recommendation**:
  Prioritize SNMPv3 implementation with USM (User-based Security Model) authNoPriv / authPriv encryption for edge polling.

---

### 2.4 Domain 4: Offline Queue Persistence, Replay Protection & Delivery ACKs

#### Finding SEC-05: Monotonic Nonce and Heartbeat Table Growth
- **Affected Component**: `backend/app/domain/integration/models.py` (`CollectorRequestNonce`, `CollectorHeartbeat`)
- **Severity**: **LOW**
- **Attack Preconditions**: Long-running production deployment with high-frequency collector ingestion and heartbeats.
- **Technical Evidence**:
  Every HMAC-verified request inserts a row into `CollectorRequestNonce` (`claim_nonce()`). Every heartbeat inserts into `CollectorHeartbeat`. In `main`, no automated Celery task or SQL retention policy exists to prune nonces older than `REQUEST_TIMESTAMP_WINDOW_SECONDS` (300s).
- **Existing Mitigation**:
  Composite unique index `(collector_id, nonce)` ensures $O(1)$ B-tree lookup performance.
- **Residual Risk**:
  Database disk usage for `collector_request_nonce` and `collector_heartbeat` grows unbounded over time.
- **Testable Remediation Recommendation**:
  Add a scheduled maintenance task (`prune_collector_nonces_and_heartbeats`) that deletes nonces older than 1 hour and truncates heartbeat history past 30 days.

---

### 2.5 Domain 5: Collector Compromise Scenarios & Site Isolation

#### Finding SEC-06: Edge Collector SQLite Database Stored Unencrypted on Local Disk
- **Affected Component**: `edge_collector/config.py` (`CollectorConfig.database_path`), `edge_collector/queue.py` (`SQLiteQueue`)
- **Severity**: **MEDIUM**
- **Attack Preconditions**:
  An attacker gains local shell or file-system read access on an Edge Collector host appliance.
- **Technical Evidence**:
  `SQLiteQueue` connects via `sqlite3.connect(str(config.database_path))` to a standard unencrypted SQLite database file. Unacknowledged telemetry readings and raw attributes buffered in `queue_records` are stored as plaintext JSON.
- **Existing Mitigation**:
  Database directory permissions can be restricted via host OS file mode (e.g. `0700`).
- **Residual Risk**:
  Disk theft or physical media extraction exposes buffered local telemetry and sensor metadata.
- **Testable Remediation Recommendation**:
  Support SQLCipher or OS-level transparent disk encryption (LUKS/dm-crypt) for edge appliance deployments.

---

### 2.6 Domain 6: Observability, Logging & Incident Response

#### Finding SEC-07: Unbounded Exception Logging Detail in Ingest Error Handlers
- **Affected Component**: `backend/app/api/v1/collectors.py`, `ingest_batch()`
- **Severity**: **INFORMATIONAL**
- **Attack Preconditions**: An edge collector submits payloads triggering unexpected backend exceptions.
- **Technical Evidence**:
  In `ingest_batch()`, the `except Exception as exc:` block logs `error=str(exc)`. While `structlog` filters standard sensitive key names (`password`, `secret`, `credential`), raw SQL exception strings can occasionally embed input attribute values in log files.
- **Existing Mitigation**:
  The response returned to the edge collector is sanitized (`error_code="INTERNAL_PROCESSING_ERROR"`).
- **Residual Risk**:
  Potentially sensitive raw attributes embedded in unhandled SQL exceptions are written to backend application logs.
- **Testable Remediation Recommendation**:
  Sanitize exception messages before passing them to loggers or use structured error codes in log contexts.

---

## 3. Prioritized Remediation Backlog

| Rank | Finding ID | Title | Severity | Targeted File |
|---|---|---|---|---|
| **1** | **SEC-02** | Single Static Fernet Key & Missing Secret Rotation | **HIGH** | `app/core/secrets.py`, `app/api/v1/collectors.py` |
| **2** | **SEC-03** | Central REST Driver Lacks Egress Target Validation | **HIGH** | `app/application/drivers/rest.py` |
| **3** | **SEC-01** | Telemetry Batch Ingest Lacks Savepoint Isolation | **MEDIUM** | `app/api/v1/telemetry.py` |
| **4** | **SEC-04** | SNMP v2c Cleartext Community Transmission | **MEDIUM** | `edge_collector/snmp.py` |
| **5** | **SEC-06** | Edge SQLite Database Unencrypted at Rest | **MEDIUM** | `edge_collector/queue.py` |
| **6** | **SEC-05** | Unbounded Growth in Nonce & Heartbeat Tables | **LOW** | `app/infrastructure/tasks/` |
| **7** | **SEC-07** | Raw Exception Detail in Application Logs | **INFORMATIONAL** | `app/api/v1/collectors.py` |

---

## 4. Suggested Security Regression Test Suite

1. **Replay Protection Regression Test**:
   Verify that re-submitting an identical `(X-Collector-Nonce, X-Collector-Timestamp)` pair within the 300s window returns HTTP 401 ("Nonce already used").
2. **Cross-Site Ingest Isolation Test**:
   Verify that an authenticated collector for Site A attempting to submit discovery or telemetry records for an Integration assigned to Site B receives a `NOT_ASSIGNED` / 403 status code for those records without failing sibling valid records.
3. **Stale / Future Timestamp Rejection Test**:
   Verify that requests with `X-Collector-Timestamp` skewed by more than 300 seconds (past or future) are rejected with HTTP 401 without consuming nonces or querying integration tables.
4. **SSRF Loopback & Metadata Protection Test**:
   Verify that REST and SNMP driver target validation rejects `127.0.0.1`, `::1`, and `169.254.169.254` targets unless explicitly enabled by an admin policy.
