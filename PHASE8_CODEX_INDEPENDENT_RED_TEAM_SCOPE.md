# Phase 8 Codex Independent Red-Team Scope

Audited repository: `AhmedMahmoud2222/DCIM`.

Target commit: `fc5fd57e59f71836828decb66dcdfb669f44b47c`; parent/base: `99fce6555d1d75152b09a61780c6a8e837395ee4`.

This independent audit reviewed the exact one-commit Phase 8 diff, its five required design documents, architecture/RBAC conventions, migration 0008, integration and collector APIs, HMAC authentication, secrets handling, discovery/reconciliation, drivers, and the Phase 8 tests. It specifically attempted to falsify claims about site assignment, payload bounding, collector authorization, audit/outbox, N+1 avoidance, idempotency, and replay resistance.

Evidence labels used in the companion report:

- **EXECUTED — VERIFIED**: command or repository comparison completed in this audit environment.
- **STATIC ANALYSIS**: conclusion from the exact audited source and migration.
- **NOT EXECUTED — ENVIRONMENT LIMITATION**: a required runtime dependency was unavailable; no pass result is implied.

The audit is implementation-neutral: no application, test, migration, frontend, configuration, Docker, or CI source was changed. The only intended permanent additions are this scope document and `PHASE8_CODEX_INDEPENDENT_RED_TEAM.md`.
