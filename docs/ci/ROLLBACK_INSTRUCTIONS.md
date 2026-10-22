# Rollback of the risk-based CI

The rollback restores the `ci.yml` of the commit before this change. The original jobs
are inside the routed workflow without change, so the rollback does not lose a test.

1. Find the commit before the merge of PR #191 on `main` (`<before>`).
2. Restore the workflow and remove the routing files:

   ```bash
   git switch -c ci/rollback-risk-routing origin/main
   git show <before>:.github/workflows/ci.yml > .github/workflows/ci.yml
   git rm .github/scripts/ci_policy.py .github/scripts/ci_router.py .github/scripts/ci_gate.py
   git rm -r tests/ci docs/ci/CI_ARCHITECTURE_ADR.md docs/ci/ROUTING_MATRIX.md
   git commit -S -m "ci: roll back risk-based routing"
   ```

3. Open a PR. After the rollback, the original jobs `backend` and `frontend` give the
   required checks again. The ruleset needs no change, because the check names stay
   `backend` and `frontend` before and after the rollback.

A fast rollback without a PR revert is also possible: change the route job to always
write the full route. Then every worker runs on every PR, and the gates stay.
