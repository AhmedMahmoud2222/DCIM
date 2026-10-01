# DCIM01 security audit, delta v1.3

Adds to `DCIM01_SECURITY_AUDIT_REPORT_v1.2.md`. Covers PR #70 (catalog datasheet PDFs), PR #71 (decommission timestamp), findings #72 to #75, and the migration lineage. Exploit-relevant detail is left out on purpose. Regression tests carry it, generated at runtime, with no sample files.

Baseline: `main` `f94f220`. Final heads: #70 `b9ee791`, #71 `ff19127`. No merge or deployment has taken place.

## 9. New attack surface (PR #70)

An authenticated administrator can upload a PDF up to 25 MB. The server parses it, scans it, stores it by content hash, and serves it back as a download. Parsing untrusted PDFs is the new exposure. The server never renders or executes PDF content.

## 10. Verified protections

| Area | What holds | Evidence |
|---|---|---|
| Parser isolation | pypdf runs in a child interpreter: 15 s CPU, 1 GB address space, 20 s wall clock, sockets and DNS disabled, 25 MB and 100-page caps | resource probes: millions of objects, cyclic and deep page trees, stream and object-stream bombs end in a clean rejection |
| Malware scanning | `required` mode fails closed (HTTP 503) on scanner absent, hung, crashed, error reply or malformed reply. Infected files are rejected and audited. Production refuses to start in any other mode | scanner policy matrix plus real `clamd` tests |
| Storage | key is `{sha256}.pdf`, never a client name. Writes are atomic. 20 hostile filenames tested | hostile API suite |
| Versioning | published and retired revisions are frozen by database triggers. A newer datasheet cannot change a published revision | schema tests, mutation testing |
| Concurrency | per-object advisory locks serialize upload and purge. Independent database sessions, lock-span probes from a second connection | hostile API and concurrency suites |
| Authorization | anonymous, ordinary, restricted, site admin and Administrator tested on every endpoint, with draft-disclosure and IDOR checks | hostile API suite |
| Migration | upgrade, downgrade with linked data, re-upgrade; RESTRICT foreign keys and immutability trigger fire | section 12 |

## 11. Findings

| # | Finding | Severity | Status |
|---|---|---|---|
| 72 | Active-content screening did not cover objects stored in compressed object streams | Medium | Fixed in #70 head (PR #77). 5 of 5 fixtures failed before, all pass after |
| 73 | #59 and #70 both added a `0031` migration on the same parent, producing two heads | Medium (blocks startup) | Lineage corrected; permanent gate in PR #79 |
| 74 | Raw client filename in a malware-rejection audit record; duplicate upload could return a row a purge was deleting; orphan objects never swept; unscanned documents served after a move to `required` | Low to Medium | Fixed in #70 head (PR #78). 4 tests failed before, all pass after |
| 75 | Lifecycle transition had no row lock: duplicate decommission events, and a status/timestamp mismatch under a race | Medium | Fixed in #71 head (PR #76). 2 race tests failed before, pass after |

Issues stay open until the owner confirms the merged state.

## 12. Migration lineage

Final: `0030_bulk_import_attempts` -> `0031_user_groups` (#59) -> `0032_catalog_documents` (#70).

Revision id, `down_revision`, filename, docstrings and references were updated. The migration SQL is unchanged. Checked on a database seeded at 0030 with a user, manufacturer, catalog model and revision, then groups at 0031, then a linked document at 0032: one head; downgrade to 0031 removes only the document tables and permission; downgrade to 0030 removes only the group tables; re-upgrade restores 3 document grants and 4 user/group grants; existing user, group and catalog rows survive every step.

A repository gate (PR #79, `scripts/check_alembic_single_head.py`) fails CI when the graph has other than one head. It fails on the unfixed #59 plus #70 combination and passes on the corrected chain.

## 13. Decommission lifecycle (PR #71)

The column is `TIMESTAMP WITHOUT TIME ZONE`; the fix stores naive UTC. Only the transition endpoint writes the status and timestamp, and the endpoint raised before commit under the original bug, so no stored row carries a bad timestamp and no backfill is needed. A row lock now serializes transitions. A CHECK tying the timestamp to the status needs a migration and is not included.

## 14. Remaining decision: tenant scope (#57)

Catalog documents are global manufacturer and catalog data. They have no site, rack or tenant link, and this report does not add one. Tenant visibility of catalog documents is open until the tenant model is designed. #57 stays open.

## 15. Untested

- ClamAV container start and signature download on a real host (CI Compose smoke covers startup; no signature update was exercised).
- Behavior under sustained concurrent uploads at production volume.
- Browser-side rendering of downloaded datasheets (served as `attachment`, `nosniff`).
- Object storage backends other than local disk.
- `github-advanced-security` fails on #70 in the latest runs. Its log tail shows only Copilot-agent cleanup, so the cause was not confirmed. It passed on #71 and #79.
