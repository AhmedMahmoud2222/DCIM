# Issue #103: event correlation, notifications, ServiceNow/ITSM and collector-offline events

Document version: 2 (implementation record; version 1 was the planning note)

Base: `main` at `c4d8674` (includes #99, #101, #102), migration head `0043_ops_correlation_itsm`.

Alarms, telemetry, collectors, heartbeats and the power topology stay the only source facts. Everything new either
records a derived observation (a collector crossing the offline threshold, an incident that *references* alarms) or the
delivery state of an outbound message. Nothing new is written by the ingestion path, and nothing external can write back
into a source fact.

## B. Collector offline and online

`collector_health_sweep.sweep_collector_states` records transitions of the health that `classify_collector_health` already
derives, using the same approved thresholds (`HEARTBEAT_OFFLINE_AFTER_SECONDS = 300`):

| Heartbeat age | State |
| --- | --- |
| up to and including 300 s | online (healthy or stale) |
| over 300 s | offline |
| never sent | judged by registration age with the same threshold |

Recovery needs a heartbeat strictly newer than the one the offline decision rested on, no older than the threshold, and not
more than 60 s in the future. A late old row or the same heartbeat seen again cannot revive a collector.

A sweep takes a transaction advisory lock and then does a compare-and-swap on `collector_state.generation`;
`(collector_id, generation)` is also unique in `collector_transition`. A transition writes the history row, an outbox event
(`CollectorOffline`/`CollectorOnline`, correlation id `collector-health:<id>:<generation>`), an audit row and, through the
notification policies, queued deliveries. A healthy collector seen for the first time gets a silent baseline. The sweep runs
from Celery beat every 60 s on `maintenance`; request handling never evaluates health. Issue #38 is not closed by this work.

## A. Correlation

`correlation_service.correlate` creates `correlation_incident` rows and `correlation_member` references. Rules, in order:

1. `collector_offline`: alarms on integrations assigned to a collector within 300 s of its offline transition. Cause is the
   transition. High confidence for availability alarms, otherwise medium.
2. `shared_power_cause`: alarms on equipment downstream of a protection device that is open or tripped (high), or of an asset
   that has its own alarm (medium). Uses the existing upstream traversal and the #102 protection state. Evidence carries the
   device, its state and the path.
3. `same_device` and 4. `same_integration`: two or more alarms within 300 s. Low confidence, stated as no topology link.

Alarms matching nothing stay separate. Identity (`dedup_key`) comes from the cause: transition id, device id plus the time of
its last state change, or the cause alarm id. Windows use source timestamps, so arrival order does not matter, replays find the
same incident, and a late alarm joins an established incident without moving its cause. A source event belongs to at most one
incident (partial unique indexes). One advisory lock serialises runs. The engine only reads alarms; the test suite hashes the
alarm, rule, transition and heartbeat tables before and after every run and after incident acknowledge and resolve.

Incident `correlation_id` is derived from the dedup key and `causation_id` is the cause's source id. Both appear in outbox
events, audit rows, notification headers and ticket text. Callers without `power:read` do not receive power-derived evidence,
rationale, cause label or causation id for `shared_power_cause` incidents.

## C. Notifications

Tables: `notification_channel` (webhook URL and signing key stored only as Fernet ciphertext; the API shows scheme, host and
port and a signed flag), `notification_policy` (events, optional site, minimum confidence), `notification_delivery`.

* Queued in the transaction that records the event, by `INSERT .. ON CONFLICT DO NOTHING` on `dedup_key`
  (`<policy>:<event>:<source id>`). A replay creates nothing and dispatches nothing.
* Sent by `deliver_notification` on the existing `notifications` queue (workers in both Compose files and the CI E2E job now
  consume it). Nothing is sent inside an alarm, telemetry or heartbeat request.
* The claim is one UPDATE that bumps `claim_generation`, sets a 120 s lease and counts the attempt. Every later write carries
  the generation, so a worker that lost its lease writes nothing. Expired leases are reclaimed by the 30 s redispatch task.
* 2xx sent. 429, 408, 5xx, timeouts and connection errors retry after 30 s, 60 s, 120 s and so on up to 1 h (a `Retry-After`
  raises the delay, capped at 1 h), at most 5 attempts, then `ATTEMPTS_EXHAUSTED`. Other 4xx, unfollowed redirects and blocked
  targets fail at once. The vocabulary is fixed; provider text, URLs and secrets are never stored or logged.
* The body is HMAC-SHA256 signed (`X-DCIM-Signature`, over `timestamp.body`) when a signing key is set, and carries
  `X-DCIM-Delivery-Id` and `X-DCIM-Correlation-Id`.
* Operators can retry a failed delivery (`integration:manage`), audited.

## D. ITSM (ServiceNow-compatible)

`itsm_service` is provider neutral; `itsm/servicenow.py` is one `ItsmAdapter`. Outbound only, Table API on `incident`, HTTP
Basic with a dedicated least-privilege user whose password is write-only. A ticket's `correlation_key` is derived from
(connection, incident) and written to ServiceNow's `correlation_id`. Create is *find then create*: an attempt that died after
the provider accepted the ticket is adopted on retry, never duplicated (tested with a lost response and a crash before commit).
Updates never rewrite the correlation key and are skipped when the payload hash is unchanged. `sys_id` and `number` must match
strict patterns, `state` maps to a fixed enum, responses are size bounded, so provider text never reaches storage or the UI.

All outbound calls go through `outbound_http`: the repository's network-target policy (deny-by-default allowlist, metadata and
link-local ranges always blocked, every resolved address checked, connection pinned to the validated address), https only
unless `OUTBOUND_ALLOW_HTTP`, no redirects, ambient proxies ignored, bounded timeouts and body.

## E. State synchronisation

Allowed: store the remote id, number and normalised state; record delivery status; send an outbound update when an operator
resolves an incident. Forbidden and tested: a remote state changing an incident, alarm, asset or topology; remote closure
clearing alarms; remote text reaching logs or UI unsanitised. There is no inbound path.

## F. API and authorization

`/api/v1/operations/...` (23 operations, pinned by a route sweep). Permissions reuse `alarm:read|manage`, `collector:read`
and `integration:read|manage`; none is site-aware, so a site-restricted user is denied on every endpoint with identical
bodies for existing and missing ids. Unauthenticated callers get 401 before any precondition check. Mutations use `If-Match`
versions where an entity is edited. One existing route-sweep assertion that expected only `/alarms` paths to appear when
`alarm:read` is made scope-aware now also accepts the incident reads gated by the same permission.

## G. UI

The existing Events page gains Incidents, Collector health, Notifications and Integrations tabs: incident cause, rationale,
evidence, source events, delivery and ticket state (including provider unavailable and retry), collector state and transition
history, channel/policy/ITSM forms whose secret fields are cleared after save and never rendered back.

## H/I. Schema and concurrency

Migration `0043_ops_correlation_itsm` adds nine tables (`collector_state`, `collector_transition`, `correlation_incident`,
`correlation_member`, `notification_channel`, `notification_policy`, `notification_delivery`, `itsm_connection`, `itsm_ticket`)
with check constraints, unique keys for dedup and idempotency, and indexes for active incidents, due retries and external
references. Additive; downgrade refuses while any table holds rows. Races covered by tests: two sweeps, two correlators, two
delivery workers, two ticket workers, lease expiry and stale-owner writes, duplicate enqueue, operator retry versus automatic
retry, broker outage at dispatch, worker crash after the provider accepted a create.

## Known limits

* Collector assignment is read as of correlation time, not as of the alarm.
* The E2E seeds alarms and an aged heartbeat through `backend/scripts/e2e_ops_driver.py` because the telemetry pipeline would
  need five real minutes; alarm creation itself is covered by the telemetry tests.
* Unauthenticated `PATCH`/`POST /power/protection-devices/...` (merged in #102) answer 428 before 401 because the `If-Match`
  dependency is declared before the permission dependency; no data is exposed. Not changed here.
