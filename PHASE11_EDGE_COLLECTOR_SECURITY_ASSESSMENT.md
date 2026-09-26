# Phase 11 Edge Collector Security Threat Model & Risk Assessment

**Status**: Hold until PR #29 merges
**Baseline Commit SHA**: `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`
**Repository**: `AhmedMahmoud2222/DCIM`
**Target Branch**: `main`
**Document Scope**: Refreshed, evidence-based security threat model and risk assessment of the Edge Collector runtime (`edge_collector/`) and Central DCIM collector trust boundary (`app/application/collector_auth.py`, `app/api/v1/collectors.py`, `app/core/secrets.py`, `app/application/drivers/rest.py`).

---

## Executive Summary

This document presents an updated security threat model evaluating the Edge Collector runtime, protocol drivers, local SQLite storage, HMAC-SHA256 machine authentication, credential encryption, and multi-site isolation boundaries against `main` at baseline commit `d96676c0534397c7320f09ca3fb4c5c3d72f86ba`.

All findings strictly distinguish **confirmed code facts in `main`**, **deployment-dependent risks**, **architectural limitations**, and **hardening recommendations**. Findings from earlier drafts have been re-evaluated and corrected or withdrawn where source code inspection proved effective existing mitigations.

---

## 1. Threat Taxonomy & Assessment Methodology

The security assessment classifies findings into four distinct categories:
1. **Confirmed Vulnerability**: A reproducible code defect that violates a stated security invariant or authorization boundary.
2. **Deployment-Dependent Risk**: A security posture factor that relies on environment configuration (e.g. host OS permissions, KMS integration, network segmentation).
3. **Architectural Limitation**: An inherent constraint of a supported protocol or standard (e.g. SNMP v2c cleartext transmission).
4. **Hardening Recommendation**: An optional performance, hygiene, or defense-in-depth enhancement.

---

## 2. Re-evaluated Findings & Security Analysis

### 2.1 SEC-01: Telemetry Batch Transaction Boundary Behavior
- **Classification**: **Hardening Recommendation** (Reclassified from Security Defect)
- **Affected Component**: `backend/app/api/v1/telemetry.py`, `ingest_collector_telemetry()`
- **Severity**: **LOW**
- **Attack Preconditions**: N/A (Functional error handling behavior).
- **Technical Evidence**:
  Source code review of `ingest_collector_telemetry()` confirms that handled per-record rejections (`assignment.collector_id != collector.id` and `MetricMappingNotFound`) produce structured ACK results (`NOT_ASSIGNED` and `UNKNOWN_METRIC_MAPPING`) without raising exceptions. An unhandled exception (e.g. DB connection loss) triggers a standard FastAPI endpoint transaction rollback.
- **Existing Mitigation**:
  Handled domain errors produce structured per-record ACK rejections, allowing the edge client to process partial acknowledgements.
- **Residual Risk**:
  Unexpected database failures on an intermediate record cause the full endpoint transaction to abort, prompting edge client retry.
- **Testable Remediation Recommendation**:
  Optionally wrap each reading ingestion in `ingest_collector_telemetry()` within an `async with db.begin_nested():` savepoint for extra granularity during DB-level constraints.

---

### 2.2 SEC-02: Static Fernet Encryption Key & KMS Lifecycle
- **Classification**: **Deployment-Dependent Risk / Architectural Limitation** (Reclassified from High Security Defect)
- **Affected Component**: `backend/app/core/secrets.py` (`encrypt_secret()`, `decrypt_secret()`)
- **Severity**: **MEDIUM**
- **Attack Preconditions**: Attacker accesses the host environment file (`.env`) or process environment variables containing `CREDENTIAL_ENCRYPTION_KEY`.
- **Technical Evidence**:
  `secrets.py` uses `Fernet` (AES-128-CBC + HMAC-SHA256) to reversibly encrypt credentials (`Integration.credential_ciphertext` and `Collector.secret_ciphertext`). Key loading relies on `settings.credential_encryption_key`. As documented in `secrets.py` module docstrings, this is an intentional foundation architecture primitive, with KMS integration explicitly planned for production deployment.
- **Existing Mitigation**:
  Credentials are never stored in plaintext in PostgreSQL. Raw collector secrets are returned exactly once upon registration (`CollectorRegisterOut`).
- **Residual Risk**:
  Static key disclosure in the environment compromises at-rest confidentiality of integration credentials across sites.
- **Testable Remediation Recommendation**:
  Implement key-versioning header tags (`v1:...`) in `secrets.py` to support key rotation, and integrate an external KMS / Vault envelope encryption provider for production deployments.

---

### 2.3 SEC-03: Central REST Driver Network Policy Validation
- **Classification**: **WITHDRAWN (Fully Mitigated in Main)**
- **Affected Component**: `backend/app/application/drivers/rest.py`, `RESTDriver.connect()`
- **Severity**: **N/A**
- **Technical Evidence & Re-evaluation**:
  Direct inspection of `backend/app/application/drivers/rest.py` confirms that `RESTDriver.connect()` explicitly executes `target = await validate_target(scheme=scheme, host=target_host, port=target_port, method=method, policy=self.network_policy)` before making any HTTP request. `network_policy.py` validates target hosts against explicit IP/subnet allowlists, blocks loopback and reserved ranges by default (`allow_loopback=False`), disables HTTP redirects (`follow_redirects=False`), and ignores ambient HTTP proxies (`trust_env=False`).
- **Conclusion**:
  **Finding SEC-03 is withdrawn.** The central REST driver already enforces strict network policy target validation.

---

### 2.4 SEC-04: SNMP v2c Cleartext Transmission
- **Classification**: **Architectural Limitation**
- **Affected Component**: `edge_collector/snmp.py`, `SNMPv2cCollector.get()`
- **Severity**: **MEDIUM**
- **Attack Preconditions**: Attacker has passive network packet capturing capability on the local data center network segment between the Edge Collector and monitored network devices.
- **Technical Evidence**:
  Standard SNMP v2c GET requests transmit community strings in plaintext inside BER headers over UDP port 161.
- **Existing Mitigation**:
  SNMP community strings are encrypted at rest on central (`credential_ciphertext`), redacted from logs, and target hosts are restricted via `SNMPTargetPolicy`.
- **Residual Risk**:
  On-path eavesdroppers can capture read-only SNMP v2c community strings on unencrypted local LAN segments.
- **Testable Remediation Recommendation**:
  Implement SNMPv3 protocol support with USM user authentication and privacy encryption (`authPriv`).

---

### 2.5 SEC-05: Nonce and Heartbeat Retention Lifecycle
- **Classification**: **Hardening Recommendation**
- **Affected Component**: `backend/app/domain/integration/models.py` (`CollectorRequestNonce`, `CollectorHeartbeat`)
- **Severity**: **LOW**
- **Attack Preconditions**: Long-running production deployment with continuous collector activity.
- **Technical Evidence**:
  HMAC nonces are stored in `CollectorRequestNonce` to enforce single-use replay protection. Heartbeats are inserted into `CollectorHeartbeat`. In `main`, these tables grow monotonically.
- **Existing Mitigation**:
  Composite unique index `(collector_id, nonce)` ensures $O(1)$ B-tree claim lookups.
- **Residual Risk**:
  Database storage footprint increases gradually over long operational periods.
- **Testable Remediation Recommendation**:
  Implement a scheduled Celery maintenance task to prune nonces older than 1 hour and truncate heartbeat records older than 30 days.

---

### 2.6 SEC-06: Edge Collector Unencrypted SQLite Storage
- **Classification**: **Deployment-Dependent Risk**
- **Affected Component**: `edge_collector/queue.py` (`SQLiteQueue`), `edge_collector/config.py`
- **Severity**: **MEDIUM**
- **Attack Preconditions**: Attacker obtains direct physical or host OS disk read access to the Edge Collector appliance.
- **Technical Evidence**:
  `SQLiteQueue` stores unacknowledged observations in a standard SQLite database file in WAL mode (`queue.db`). Raw attributes and payload JSON are stored unencrypted at rest.
- **Existing Mitigation**:
  Host OS file permissions restrict directory access (`0700`).
- **Residual Risk**:
  Unauthorized physical disk access or snapshot extraction exposes unacknowledged sensor readings.
- **Testable Remediation Recommendation**:
  Utilize OS-level transparent disk encryption (LUKS/dm-crypt) or SQLCipher for edge appliance deployments.

---

### 2.7 SEC-07: Ingest Error Logging Sanitization
- **Classification**: **Hardening Recommendation**
- **Affected Component**: `backend/app/api/v1/collectors.py`, `ingest_batch()`
- **Severity**: **INFORMATIONAL**
- **Attack Preconditions**: An edge collector submits malformed payload structures that cause unexpected backend exceptions.
- **Technical Evidence**:
  `ingest_batch()` logs `error=str(exc)` on unhandled exceptions. Response messages returned to collectors are sanitized (`error_code="INTERNAL_PROCESSING_ERROR"`).
- **Existing Mitigation**:
  `structlog` processor `_redact_sensitive` scrubs standard sensitive keys (`password`, `secret`, `credential`).
- **Residual Risk**:
  Database constraint exception text logged on central may contain unhandled attribute values.
- **Testable Remediation Recommendation**:
  Sanitize raw exception messages prior to logging.

---

## 3. Prioritized Remediation Backlog

| Rank | Finding ID | Title | Category | Severity | Targeted File |
|---|---|---|---|---|---|
| **1** | **SEC-02** | KMS Envelope Encryption & Key Rotation | Deployment-Dependent | **MEDIUM** | `app/core/secrets.py` |
| **2** | **SEC-04** | SNMPv3 USM Encryption Support | Architectural Limitation | **MEDIUM** | `edge_collector/snmp.py` |
| **3** | **SEC-06** | Edge Appliance Disk Encryption (SQLCipher/LUKS) | Deployment-Dependent | **MEDIUM** | `edge_collector/queue.py` |
| **4** | **SEC-05** | Automated Nonce & Heartbeat Pruning Task | Hardening | **LOW** | `app/infrastructure/tasks/` |
| **5** | **SEC-01** | Telemetry Batch Ingestion Savepoint Isolation | Hardening | **LOW** | `app/api/v1/telemetry.py` |
| **6** | **SEC-07** | Ingest Exception Logging Sanitization | Hardening | **INFORMATIONAL** | `app/api/v1/collectors.py` |

---

## 4. Suggested Security Regression Test Suite

1. **Replay Protection Test**:
   Submit an identical `(X-Collector-Nonce, X-Collector-Timestamp)` pair within the 300s window and verify HTTP 401 ("Nonce already used").
2. **Site Boundary Isolation Test**:
   Verify that a collector registered to Site A attempting to ingest records for an Integration assigned to Site B receives a `NOT_ASSIGNED` rejection status without failing valid sibling records.
3. **Network Policy Target Validation Test**:
   Verify that `RESTDriver.connect()` and `SNMPTargetPolicy` reject loopback (`127.0.0.1`), link-local (`169.254.169.254`), and non-permitted subnet targets.
