# Phase 8 — Edge Collector Architectural Contract

## 1. Ownership Contract (Master Prompt §23)

| CENTRAL DCIM OWNS | EDGE COLLECTOR OWNS | EDGE DOES NOT OWN |
|---|---|---|
| Inventory (`ManagedAsset`/Rack/Equipment/PDU/UPS/Generator/PowerPanel — Phases 2/3/7) | Local device acquisition (running the ICMP/SNMP/REST driver against devices on its own site network) | Authoritative inventory |
| Integration configuration (`Integration`: target, protocol, poll interval, credential) | Local protocol execution (the actual socket/HTTP/SNMP round trip) | Global asset identity |
| Collector identity (`Collector`: name, type, site, status) and assignment (`CollectorAssignment`) | Temporary durable buffering while WAN is unavailable (§4 below — the boundary of what is and is not implemented in this phase) | Final reconciliation decision (`accept_reconciliation`/`reject_reconciliation` are central-only, human-triggered) |
| Authoritative telemetry *after* ingestion (once `ingest_discovery`/a future Phase 9 telemetry write commits) | Secure forwarding of buffered batches to central, with retry/backoff | Central audit history (`AuditLog` is written only by central, never forwarded as a log) |
| Audit (`AuditLog`), Outbox events, RBAC/permission decisions | Heartbeat (health self-report — `queue_depth`/`cpu_pct`/`mem_pct`/`status`) | Global alarm state (does not exist yet — Phase 10) |
| Reconciliation decisions (`ReconciliationDiff.status`, `decided_by_user_id`) | Local acquisition state (what it has and hasn't successfully sent, tracked locally) | Central topology truth (`PowerNode`/`PowerConnection` graphs remain Phase 3's, untouched by Phase 8) |

**The one sentence version**: Central DCIM is the system of record for identity, configuration,
authorization, audit, and every decision a human makes. The Edge Collector is acquisition
and transport only — it never has its own copy of "the truth," only a temporary local
queue of not-yet-forwarded observations.

## 2. What "Edge Collector" Means in This Phase, Concretely

This phase implements the **central-side** contract an edge collector program talks to:
collector identity/registration (`POST /collectors`), capability declaration, integration
assignment, the HMAC machine-trust authentication scheme (`app/application/collector_auth.py`),
the heartbeat endpoint, and the batch-ingest endpoint with its idempotency/ACK semantics.

**No actual edge collector binary/agent is built or deployed in this phase.** There is
nothing in this repository that runs *on* a site network, opens a local durable queue, or
performs the collector side of the store-and-forward protocol described in §4 below. The
protocol drivers (`app/application/drivers/`) are real and tested, but in this phase they
run *inside the central process itself* (`run_polling_cycle`, triggered by the
`POST /collectors/{id}/poll-now` manual endpoint) — this is the "central-only deployment"
diagram in the master prompt, not yet the "Site → Edge Collector → Internet → Central"
diagram. Both diagrams share the identical `Integration → Protocol Driver → Device`
abstraction (§5), so a real edge collector agent, when built, plugs into the same
`Collector`/`CollectorAssignment`/ingest-API surface without any change to this phase's
domain model or central API — but it is a distinct future deliverable, not part of what
this task implements or claims to implement.

## 3. Secure Transport (Master Prompt §9)

Implemented in this phase: collector identity, HMAC-SHA256 request signing over
`{collector_id}.{timestamp}.{nonce}.{raw_body}`, replay protection (`CollectorRequestNonce`
unique-constraint atomic claim, checked only after signature verification), a
300-second timestamp window, and payload validation/size limits (record count and
per-record attribute size). This is a genuine machine-to-machine trust mechanism, never
composed with the user JWT/RBAC path (`get_current_collector` is a wholly separate FastAPI
dependency from `get_current_user`).

**Not implemented in this phase, and explicitly an open decision (§6 below)**: TLS 1.3/mTLS
termination is a deployment-layer concern (reverse proxy / load balancer configuration),
not application code this phase can implement or test — the HMAC scheme is designed as
defense-in-depth *alongside* TLS, explicitly documented as not a replacement for it
(`app/application/collector_auth.py`'s own docstring). Certificate lifecycle/rotation is
an open decision. Credential rotation for the collector's own HMAC secret has no
dedicated "rotate" endpoint in this phase (only initial issuance) — also an open decision.

## 4. WAN Outage / Store-and-Forward — Precise Implementation Boundary

**Implemented and tested (central-side ingestion contract)**:
- `POST /collectors/{id}/ingest` accepts a batch of records, each idempotent on its own
  `dedup_key` (reusing the existing `IdempotencyKey` mechanism — no second framework).
- `occurred_at` (the device's own observation time) is preserved verbatim in
  `raw_attributes` alongside `received_at` (when central actually processed it) — never
  overwritten.
- Per-record ACK semantics: the response names each record's outcome
  (`accepted`/`duplicate`/`rejected`) so a real collector agent could drop only the
  acknowledged records from its own local queue and retry the rest.
- Partial batch failure isolation: one record's failure (FK violation, not-assigned-to-
  this-collector) does not fail sibling records in the same batch.
- Batch size (`MAX_BATCH_RECORDS = 500`) and per-record payload size
  (`MAX_RAW_ATTRIBUTES_BYTES = 8192`) bounds.

**NOT implemented in this phase (no collector agent exists to implement it in)**:
- A durable local queue, its maximum size, and its retention duration.
- Retry policy / exponential backoff on the collector side.
- Queue-full behavior (what a real collector does when its local buffer is full and WAN is
  still down — drop oldest, drop newest, or refuse new acquisition — is undecided).
- Backlog visibility (a way for central to know how much unforwarded data a collector is
  currently holding) — `CollectorOut`'s frontend fields for "backlog size/age" exist in the
  UI's intent but have no data source yet, since nothing populates a backlog metric without
  a real collector reporting one.
- Recovery behavior after a collector process restart or power loss (there is no collector
  process to restart).

These are listed as explicit open decisions in §6, per the master prompt's own instruction
not to invent arbitrary production values for a component that does not yet exist.

## 5. Contract for Phase 9 (Telemetry) — Master Prompt §24

The chain this phase establishes, and Phase 9 is expected to extend without redesigning it:

```
Collector → Integration → Protocol Driver → Device → Metric Mapping → Normalized
acquisition result (AcquisitionResult: external_identifier, observed_at, raw_attributes,
metrics) → [Phase 8 stops here: ingest_discovery only records "a device was observed"] →
[Phase 9 begins here: TelemetryReading pipeline, time-series storage]
```

Concretely, Phase 9 needs to:
1. Add its own `TelemetryReading` (or similarly named) table and write path — this phase
   deliberately does not create one, per master prompt §17.
2. Extend `run_polling_cycle` (or a Phase-9-owned equivalent) to write `AcquisitionResult.metrics`
   into that new table, instead of (or alongside) today's `ingest_discovery` call — no change
   to `Collector`/`Integration`/`CollectorAssignment`/the driver abstraction is required to do
   this, since `AcquisitionResult` already carries a `metrics: dict` field Phase 8 defines but
   does not yet consume beyond discovery.
3. Reuse the *same* `MetricMapping` concept already defined in
   `app/application/drivers/snmp.py` (oid → metric_name → unit) rather than inventing a second
   metric-naming scheme.
4. The ingest endpoint's `IngestRecordIn` already carries `occurred_at`/`external_identifier`/
   `raw_attributes` in the exact shape a telemetry ingestion path would need — Phase 9 can add
   a parallel telemetry-specific endpoint or extend this one; either way, no Phase 8 contract
   needs to change for that to work.

## 6. Open Decisions (Master Prompt §28)

| Decision | Options | Technical consequence | Owner | Required before | Status |
|---|---|---|---|---|---|
| Collector local buffer duration | Fixed count (e.g. 10k records) vs. fixed duration (e.g. 24h) vs. both | Determines memory/disk footprint of a real edge collector agent | Ops/Platform | Building an actual edge collector agent | OPEN |
| Collector local storage capacity | RAM-only (lost on restart) vs. on-disk (survives restart, needs a real embedded store e.g. SQLite) | On-disk is required for genuine "recovery after power loss," which master prompt §10 explicitly asks about | Ops/Platform | Building an actual edge collector agent | OPEN |
| Heartbeat stale/offline timeouts | Currently `HEARTBEAT_STALE_AFTER_SECONDS=90`, `HEARTBEAT_OFFLINE_AFTER_SECONDS=300` — reasoned defaults, not an ops-approved SLA | A shorter timeout gives faster alerting but more false "stale" flapping over imperfect networks | Ops | Production rollout | OPEN (defaults in place, named constants, easy to change) |
| Certificate lifecycle/rotation for collector HMAC secret | Manual re-registration vs. a dedicated rotate-secret endpoint vs. short-lived + refresh | No rotation endpoint exists today; a compromised secret today requires disabling and re-registering the collector | Security | Production rollout with real edge sites | OPEN |
| Supported SNMP versions/security modes | v1/v2c (community string) vs. v3 (USM, real auth/priv) | `SNMPDriver` currently validates `("v1","v2c","v3")` as configuration values but has NO real transport for any of them (`SimulatedSNMPTransport` is a test double) — real SNMP polling requires a real transport implementation (e.g. `pysnmp`), not built in this phase | Ops/Vendor-integration owner | Any real SNMP device polling | OPEN — explicitly disclosed, not silently implied |
| Initial vendor/model priority | Which PDU/UPS/generator vendors get a real driver/profile first | Determines the first "vendor profile" concrete implementations built on top of this phase's abstraction | Product/Ops | Real-device SNMP/vendor-profile work | OPEN |
| Central deployment model | Single central instance vs. multi-region/HA central | Affects whether "central restart recovery" needs distributed coordination beyond what a single-instance Postgres-backed idempotency/nonce store already provides | Platform | Multi-region rollout | OPEN |
| RPO/RTO for collector-buffered data | Target maximum data loss / recovery time if a collector's local buffer is lost | Directly determines the buffer-duration/storage-capacity decisions above | Ops | Production rollout | OPEN |
| Object storage for large device payloads | None yet — `raw_attributes` bounded to 8 KiB fits in Postgres JSONB; a future device type with large payloads (e.g. images, packet captures) would need object storage | Would require a new attachment mechanism, not built here | Platform | A device/integration type with genuinely large payloads | OPEN — not needed by anything in this phase's scope |
| Secrets/KMS | `credential_encryption_key`/collector secret Fernet key currently lives in application config (`.env`), not a managed KMS | A leaked config file exposes every stored credential/collector secret at once | Security | Production rollout | OPEN — explicitly disclosed in `app/core/secrets.py`'s own docstring |
| Notification/ITSM integrations | None built — `CollectorDisconnected` event type named in the master prompt's example list is NOT implemented (no background sweep exists to detect and fire it) | A collector going silent is only visible by a human checking `GET /collectors` (computed health), not proactively notified | Ops/Product | Phase 10 (Alarm+Event) or a dedicated notification phase | OPEN / DEFERRED |
| Scheduled (non-manual) polling | `poll-now` is a manual, on-demand trigger only; no Celery-beat or equivalent scheduler wires `run_polling_cycle` to `Integration.poll_interval_seconds` automatically | Without this, "polling" only happens when an operator or test calls the endpoint | Platform | Any real production telemetry acquisition | OPEN / DEFERRED — `poll_interval_seconds` is stored and displayed but not yet acted on automatically |
