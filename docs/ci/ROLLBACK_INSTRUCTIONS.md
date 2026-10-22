# Rollback Instructions — Risk-Based CI

**Repository:** `AhmedMahmoud2222/DCIM`
**Branch:** `ci/risk-based-ci-optimization` (Draft PR; do not merge)
**Delivery:** one isolated Draft PR with verified signed commits

## 1. What was changed

| File | Change |
|------|--------|
| `.github/workflows/ci.yml` | Restructured into Fast CI / Component CI / Full CI; added `fast-ci`, `component-edge-collector`, `component-database-contracts`, `component-opencode`, `full-ci`; `backend` now risk-aware (fast/full depth); `concurrency.cancel-in-progress` added; dependency caching added. |
| `.github/scripts/ci_policy.py` | New: deterministic risk-classification policy. |
| `.github/scripts/ci_router.py` | New: change detection + fail-closed routing. |
| `tests/ci/` | New: fail-closed regression tests (policy, router, scenarios, false-green negatives). |
| `docs/ci/` | New: ADR, baseline report, routing matrix, benchmark, rollback instructions. |

No changes were made to branch protection, rulesets, production secrets, or deployment
workflows (`deploy.yml`, `deployment-validation.yml`, `deploy-tools-test.yml`).

## 2. How to roll back

The rollback restores the previous single-job-per-component `ci.yml` (all jobs ran on
every PR) and removes the new policy/router scripts and tests.

### Step A — Check out the delivery branch

```bash
git fetch origin
git checkout ci/risk-based-ci-optimization
git log --oneline -3            # confirm you are on the Draft PR branch
```

All commits on this branch are signed; verify before operating:

```bash
git verify-commit HEAD
# expect: Good "git" signature ... with ED25519 key
```

### Step B — Restore the previous workflow

Option 1 (recommended): revert the workflow file to its state on `origin/main`.

```bash
git show origin/main:.github/workflows/ci.yml > .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -S -m "ci: revert risk-based workflow to previous ci.yml"
```

Option 2: revert the entire PR branch to `origin/main` (abandon the optimization).

```bash
git reset --hard origin/main
git push origin +ci/risk-based-ci-optimization   # only if you own the branch
```

### Step C — Remove the new scripts and tests (optional, completes rollback)

```bash
git rm -r .github/scripts/ci_policy.py .github/scripts/ci_router.py tests/ci
git commit -S -m "ci: remove risk-based policy/router and tests"
```

### Step D — Verify

```bash
# YAML parses and all external actions use full commit SHAs
python - <<'PY'
import re, yaml
from pathlib import Path
data = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
assert list(data['jobs'])  # jobs present
bad = []
def walk(n):
    if isinstance(n, dict):
        if 'uses' in n and isinstance(n['uses'], str) and not n['uses'].startswith('./'):
            if not re.fullmatch(r'[^@\s]+@[0-9a-f]{40}', n['uses']):
                bad.append(n['uses'])
        for v in n.values(): walk(v)
    elif isinstance(n, list):
        for v in n: walk(v)
walk(data)
assert not bad, bad
print('rollback OK: workflow valid, actions pinned to full SHAs')
PY
```

### Step E — Required checks still present

Confirm `backend` and `frontend` jobs still exist in `.github/workflows/ci.yml` (they are
the required status checks and must not be removed).

## 3. Forward (re-apply) after rollback

Re-apply from the Draft PR branch:

```bash
git checkout ci/risk-based-ci-optimization
# the branch contains the full implementation; re-open / re-submit the Draft PR
```

Do not merge or deploy. Request an independent reviewer to inspect the exact published HEAD
and post findings on the same PR before any merge consideration.

## 4. What rollback does NOT affect

* Branch protection / rulesets — untouched by this work.
* Production secrets — never referenced.
* Deployment workflows (`deploy.yml`, `deployment-validation.yml`, `deploy-tools-test.yml`)
  — never modified.
* Merges / deployments — none performed as part of this work.
