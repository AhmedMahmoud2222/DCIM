# DCIM Platform

An in-house Data Center Infrastructure Management (DCIM) platform with a FastAPI/PostgreSQL backend, React/TypeScript frontend, and separately packaged Edge Collector. Source code covers physical infrastructure, catalog and equipment models, power and monitoring capabilities; the exact implemented and accepted scope must be determined from the checked-out commit and its evidence.

Use the guides below for development, operations and technical contracts. Original phase and review records are organized under [history](docs/history/README.md); release evidence and authorization requirements are explained in the [release guide](docs/RELEASE_AND_DEPLOYMENT.md).

## Choose your starting point

| I need to… | Start here |
| --- | --- |
| Understand the project and where documentation lives | [Documentation index](docs/DOCUMENTATION_INDEX.md) |
| Assess delivered features, limits and integration history | [Project status](docs/PROJECT_STATUS.md) — dated baseline; compare with live source |
| Understand components and source-of-truth boundaries | [Architecture overview](docs/ARCHITECTURE_OVERVIEW.md) |
| Set up development or run tests | [Development and testing](docs/DEVELOPMENT_AND_TESTING.md) |
| Operate or troubleshoot the application | [Operations](docs/OPERATIONS.md) |
| Understand release, staging and production approval gates | [Release and deployment guide](docs/RELEASE_AND_DEPLOYMENT.md) |
| Find original architectural decisions, findings and closure evidence | [Historical evidence guide](docs/HISTORY_AND_EVIDENCE.md) |
| Review proposed features and release sequencing | [Product roadmap](docs/PRODUCT_ROADMAP.md) |

## Repository layout

| Directory | Purpose |
| --- | --- |
| `backend/` | API, domain/application services, PostgreSQL migrations, workers and tests |
| `frontend/` | Web UI, frontend tests and browser E2E |
| `edge_collector/` | Edge polling/transport and independent tests |
| `docs/` | Current navigation, operating guidance, contracts, architecture, historical evidence pointers |
| `.github/` | CI, release/deployment workflows and validation scripts |
| `docs/history/` | Original dated architecture, phase and review evidence, organized by delivery stream |
| `.claude/skills/archify/` | Bundled tool/reference documentation; separate from DCIM project reports |

## Work with the code safely

For local prerequisites, dedicated development/test database creation, PostgreSQL privilege separation, migrations, environment variables, worker startup, test commands and Edge/browser suites, follow [Development and testing](docs/DEVELOPMENT_AND_TESTING.md). Do not treat sample credentials, checked-in examples or historical test results as production configuration.

For runtime checks, backup/restore, telemetry freshness, credential encryption, incident response and Compose validation, follow [Operations](docs/OPERATIONS.md). The [release guide](docs/RELEASE_AND_DEPLOYMENT.md) distinguishes configuration validation, actual staging execution and production authorization.

**Safety boundaries:** No production deployment is authorized by this documentation change. In particular, **migration 0057 must not be executed in production** without representative staging qualification and separate written owner **and** DBA approval; follow [its rollout runbook](docs/FK_INDEX_ROLLOUT_0057.md) and [Issue #214](https://github.com/AhmedMahmoud2222/DCIM/issues/214). Security findings, required CI/reviews and feature-specific controls remain authoritative in their respective documents and GitHub records.

When reviewing a PR, follow [AGENTS.md](AGENTS.md). A documentation-only summary must never be used as a substitute for exact-head code review, tests, or formal approval.
