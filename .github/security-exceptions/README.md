# Frontend audit exception: GHSA-fc5q-39xc-vjpf (picomatch)

**SECURITY EXCEPTION NOT APPROVED OR ACTIVE.**

This directory holds an *inactive* mechanism (Issue #364). The enforced CI gate is unchanged:
`npm audit --audit-level=high` in the `frontend-suite` job still fails while the advisory is
reported. No workflow calls `.github/scripts/frontend_audit_gate.py`, and
`frontend-audit-picomatch.json` records `"approved": false`.

## Files

| File | Role |
|---|---|
| `.github/scripts/frontend_audit_gate.py` | Fail-closed wrapper. Stdlib only. `--mode strict` (default) or `--mode exception`. |
| `.github/security-exceptions/frontend-audit-picomatch.json` | The owner's approval record. Shipped unapproved. |
| `tests/ci/test_frontend_audit_gate.py` | Positive and negative tests, run by the existing `ci-policy` job. |
| `tests/ci/fixtures/frontend_audit_gate/` | Real `npm audit --json` output and registry data used as fixtures. |

## Threat model and trust boundaries

**Asset:** the integrity of the frontend CI security gate.

**Residual vulnerability:** picomatch <= 4.0.7 has a regular-expression denial of service when it
is given a crafted extglob pattern. Impact is availability only. In this repository the affected
packages are development dependencies (`npm audit --omit=dev` reports 0), the container ships only
the built `dist/`, the dev server is bound to localhost, and the only glob in application code is a
static `import.meta.glob`. These are findings from Issue #364, not guarantees: the wrapper
re-checks the ones it can.

**Trusted inputs:** this repository's committed files at the reviewed commit (the script, the
approval record, the lockfile) and the code review that protects them.

**Untrusted or fallible inputs, all treated as failures when wrong:** `npm audit` output, npm
registry responses, the GitHub advisory API, the system clock, the file tree under
`frontend/src` and `frontend/e2e`.

**Out of scope:** an actor who can change this script, the configuration and the workflow in one
unreviewed commit can disable any gate. Protection is code review (`AGENTS.md`) and the tests
below, which fail if the exception becomes active without the tests being changed on purpose.

## Failure modes (all exit 1)

| Condition | Result |
|---|---|
| Any production dependency vulnerability, or `npm audit --omit=dev` fails | fail |
| Truncated, malformed or empty `npm audit` output; `error` payload; unknown report version | fail |
| Missing `metadata`/`vulnerabilities`, unknown severity, counts that do not add up | fail |
| A `via` reference that is not in the report; an advisory with inconsistent metadata | fail |
| A node whose root advisory cannot be established, or that is rated above all its advisories | fail |
| npm exit status other than 0 or 1; exit 0 with high findings; exit 1 without any | fail |
| Any high/critical advisory other than GHSA-fc5q-39xc-vjpf anywhere in a chain | fail |
| Configuration absent, unreadable, not approved, malformed, wrong advisory, extra/missing keys | fail |
| Expiry missing, malformed, not UTC-`Z`, at or before the current time, or lifetime over 45 days | fail |
| npm registry or advisory API unreachable, invalid or inconsistent | fail |
| A picomatch release newer than 4.0.7; a patched version or changed range in the advisory; advisory withdrawn | fail |
| Application source references picomatch, minimatch, tinyglobby or fdir, or uses a non-literal `import.meta.glob` | fail |
| Source directory missing, symlinked or unreadable | fail |
| Any unexpected exception in the wrapper | fail (never converted to success) |

The advisory id is a constant in the script. Configuration cannot name another advisory, a package, a
severity or a family of packages.

## Compensating controls (to be restated by the owner when deciding)

Dev-only dependency graph; zero production vulnerabilities enforced on every run; static globs
only, enforced by the source guard; localhost-only dev server; hard expiry; automatic failure when
upstream publishes a fix; independent review of every change to this mechanism.

## Source guard: limitations

The guard is a textual scan, not an AST parse. It flags quoted module specifiers equal to a
prohibited module even in comments (a false failure, never a false pass). It cannot see a module
name assembled at runtime; review and the zero-production-vulnerability gate cover that.

## Activation procedure (NOT performed; requires separate written owner authorization)

Do these in one reviewed PR, only after the owner has accepted the risk in writing on Issue #364:

1. Edit `frontend-audit-picomatch.json`: `approved: true`, `approved_by`, `approval_reference`
   (link to the owner's written authorization), `approved_on`, and `expires_utc` (the owner sets the
   date; the proposed 2026-12-18 is **not approved**; maximum 45 days after `approved_on`).
2. In `.github/workflows/ci.yml`, in `frontend-suite`, replace the single step
   `run: npm audit --audit-level=high` with
   `run: python3 .github/scripts/frontend_audit_gate.py --mode exception`
   (the step needs `GITHUB_TOKEN` in its environment for advisory API rate limits). Keep the
   non-blocking full-report step and every other step.
3. Update `tests/ci/test_frontend_audit_gate.py::ActivationBoundaryTests`, which currently asserts
   the exception is inactive, to assert the activated state. Updating it is the deliberate signal that
   activation was reviewed.
4. Obtain independent review of the exact head. Remove the exception, and switch back to the plain
   audit step, as soon as picomatch publishes a fix or the expiry passes.
