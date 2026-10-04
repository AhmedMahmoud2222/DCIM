# Issue #103 implementation start — correlation, ITSM, notifications and collector-offline events

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

Initial slices:
1. Deterministic collector offline/online transition event.
2. Event/alarm correlation model that preserves every source event.
3. Probable-cause evidence model.
4. Notification provider abstraction with retry/dedupe/audit.
5. ServiceNow/ITSM adapter with idempotent create/update and least-privilege credentials.
6. Failure-isolation, secret-redaction and provider-outage tests.

External incident systems must never become authoritative for DCIM inventory/topology facts.
