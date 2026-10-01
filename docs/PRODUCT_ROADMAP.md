# DCIM01 Consolidated Product Roadmap
Version: proposed 1.0, 2026-09-29. Baseline: main at a1ba578cb5ae5d5fea0c9e56dc7c863e47ca6577.

This roadmap combines the original kickoff vision, MVP, Phase 10 extensions, Phase 11 assurance and later feature requests. Proposed release labels R1–R5 are distinct from historical phase numbering. Historical Phase 3/4/13 definitions have not been fully recovered. A merged feature is not necessarily independently accepted or deployed.

## Delivered source baseline
| Area | Scope | Evidence |
|---|---|---|
| Foundation | FastAPI, PostgreSQL, authentication, RBAC, audit, outbox, Alembic, CI | [Phase 1](../PHASE1_IMPLEMENTATION.md) |
| Operational DCIM | Locations, rooms, racks, equipment, 2D floor plans, rack elevations, power topology, alarms, dashboard | [Project status](PROJECT_STATUS.md) |
| Monitoring MVP | Edge Collector, SNMP v2c, ICMP/REST, discovery, metric mappings, telemetry and alarm lifecycle | [MVP plan](../DCIM_MVP_V0_1_PLAN.md) |
| Phase 10A | Versioned manufacturer/model catalog, graphics and marker editor, draft/publish/retire | PRs #16, #20, #21 merged |
| Phase 10B | Equipment instantiation, immutable port/inlet snapshots, cabling, rack faceplates | PR #22 merged |
| Phase 10C | Live telemetry overlays and bounded power/network failure-impact simulation | PR #23 merged |
| Phase 11 | Security remediation, CI hardening, deployment governance and validation | Incremental; consult live PRs and [security plan](../PHASE11_SECURITY_REMEDIATION_PLAN.md) |

## R1 — Controlled staging and acceptance
- Reconcile exact-head technical acceptance, required checks and merge status for [SEC-07 PR #49](https://github.com/AhmedMahmoud2222/DCIM/pull/49) and [Excel import PR #50](https://github.com/AhmedMahmoud2222/DCIM/pull/50). Keep SEC-07 compound-failure pool-recovery caveat as follow-up.
- Confirm [accessibility PR #31](https://github.com/AhmedMahmoud2222/DCIM/pull/31) status and outstanding manual NVDA/VoiceOver, zoom and keyboard checks.
- Audit database units and physical-value storage. Architecture approval is required before implementation or migration; audit findings are not automatically staging blockers unless a concrete risk is demonstrated.
- With separate owner authorization, execute [staging VM plan](STAGING_VM_DEPLOYMENT_PLAN.md): isolated Ubuntu VM, private HTTPS/VPN, staging-only secrets, synthetic data, exact-main CI, backup/restore and migration/rollback rehearsal.
- Record deployment SHA, check runs, smoke results, risks and sign-off. **Production deployment remains disabled.**

## R2 — Operational completeness and data integrity
- Accept Excel bulk import: templates, preview, validation, transactional behavior, audit, XLSX formula and ZIP protections.
- After approval, standardize dimensional units, canonical telemetry registry, raw-reading preservation, conversion boundaries, precision and backward-compatible migrations.
- Improve asset lifecycle, cabling validation, alarm workflows, reporting, accessibility and media persistence.
- Run isolated real-device interoperability and recovery tests.

## R3 — Advanced infrastructure intelligence
- LLDP/CDP and approved SNMPv3 network discovery; ports, patch panels and cable traceability.
- UPS/PDU/breaker models, A/B redundancy, electrical capacity and dependency analysis.
- Rack, power and cooling capacity forecasts, utilization trends and reports.
- Event correlation, ServiceNow/ITSM integration, expanded tested vendor/device profiles and change-impact simulation.

## R4 — Spatial digital twin and thermal analysis
- Calibrated 2D/3D room/rack geometry, equipment state and connectivity overlays.
- Validated CAD/Visio import, grids, rack detection and manual correction.
- Environmental and cooling telemetry, heat maps and airflow visualization.
- CFD with documented boundary conditions, measured-sensor calibration and uncertainty reporting.

## R5 — AI-assisted and controlled autonomous operations
- Permission-aware natural-language assistant grounded in DCIM records.
- Data-quality monitoring, anomaly detection, predictive maintenance and capacity forecasts.
- Alarm correlation, probable-cause analysis and recommended incident runbooks.
- Human-approved remediation with simulation, rollback, audit, least privilege and kill switch; self-healing only after independent safety validation.

## Cross-cutting release requirements
Security and secret rotation; data integrity and historical telemetry preservation; idempotent ingestion and concurrency tests; WCAG-oriented accessibility plus manual checks; exact-commit CI and independent review; vendor interoperability evidence; environment-specific release authorization.

## Governance
Historical phase documents remain unchanged. [Project status](PROJECT_STATUS.md) records actual implementation, PR and CI evidence. The README and [documentation index](DOCUMENTATION_INDEX.md) link here. Future work requires owner-approved scope, dependencies, tests and acceptance criteria. Do not mark a proposed feature delivered or production-validated without evidence.
