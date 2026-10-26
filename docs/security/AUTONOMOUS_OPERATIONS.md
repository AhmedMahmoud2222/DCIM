# Autonomous operations: foundation and activation blockers

Status: **partial implementation, not activated, not approved for merge or deployment**.
Audit date: 2026-10-26. Implementation branch: `security/trusted-agent-cross-trigger`.

## Live baseline

The branch and `main` both pointed to `51f2981247bb8cb4a2aa9c830f036da1db6a9411`
before implementation. Main and PR refs were fetched. No matching open foundation
PR existed. Existing workflows are `ci.yml`, `claude.yml`, `claude-code-review.yml`,
`deploy.yml`, `deployment-validation.yml`, `deploy-tools-test.yml`, `opencode.yml`,
`opencode-security-validation.yml`, `model-health-diagnostic.yml`, and
`opencode-native-smoke.yml`. Only `opencode.yml` changes in this foundation.

During review, main advanced to `dc9962237a47f741361ccd914a413ac295dd079f`
(PR #210). Its proxy/streaming regression files do not overlap the foundation's
changes. It was fetched and integrated without rewriting the earlier signed
commits; validation was repeated against that combined tree.

Open PR #207 independently adds a Claude command gate. This change does not copy,
replace or silently activate that work. PR #210 changes the OpenCode streaming
proxy; that implementation is also left separate. No preparation patch was applied.

GitHub ruleset `23651422` (Default Policy) is active on the default branch. It
requires backend/frontend checks, strict up-to-date checks, resolved review threads,
linear history, and blocks deletion/non-fast-forward pushes. It requires **zero**
approving reviews, does **not** require signatures, and contains always-bypass
entries for the owner and six integrations. These are observed settings, not safe
autonomous merge guarantees. Classic branch-protection reads returned 403. No
repository settings, bypass actors, credentials or environments were changed.

The connected installation list identifies the connector's accessible installation,
not every installed GitHub App. The repository installation API was inaccessible.
App permissions, installation ownership and readiness therefore remain unverified.
Codex identity was independently observed in GitHub API comments on PR #188:
numeric user ID `199175422`, login `chatgpt-codex-connector[bot]`, App slug
`chatgpt-codex-connector`. The App ID `1144995` also appears in API metadata. This
does not establish provider capability or authorization to initiate other agents.
Claude, Copilot and the actual `OPENCODE_APP_SLUG` identity must still be verified.

## Implemented controls

`agent_authority.py` and the OpenCode gate add opt-in authorization for bot-authored
commands on an **existing same-repository open PR**. Humans retain their numeric
allowlist and association checks. Exact `/oc` and `/opencode` grammar is unchanged;
quoted, embedded, uppercase and prefix lookalikes remain rejected. Bot requests
also require:

1. An owner-managed full identity tuple: numeric user ID, exact login, numeric App
   ID and App slug. All must match GitHub event/API metadata, including
   `performed_via_github_app`; missing attribution fails closed.
2. A prior authorized human `/oc` or `/opencode` command in the same conversation.
   A mention aimed at another provider grants no OpenCode authority. The bot must
   explicitly identify that command in its penultimate line `DCIM-Root: <comment id>`.
   Review-only roots cannot authorize implementation. Text claiming ownership grants
   no authority. Trusted bot task text remains potentially prompt-injected: the
   sandbox, protected publisher and independent acceptance boundaries still apply.
3. A final line `DCIM-Head: <full current HEAD SHA>` and an unedited command present
   in current paginated API history. Compare ID, body, creation/update timestamps,
   user ID/login/type and App ID/slug; ignore reaction counts and incidental metadata.
   Creation/update timestamps must agree on both sides. Captured Claude REST
   metadata is tested alongside a synthetic webhook projection; an actual live
   webhook/REST round-trip remains unverified while this workflow is unmerged.
   Providers must create a **new standalone command comment**; editing a progress
   or tracking comment is intentionally unsupported. The captured edited Claude
   review comment is a negative fixture, not an eligible command. Capability
   probes must prove that a provider can create fresh commands before enabling it.
   A live reaction round-trip on owner comment `6424782080` increased the reaction
   count from 1 to 2 without changing either timestamp (recorded fixture). This
   verifies the observed REST behavior, not live bot authorization or a claim race.
4. An atomic GitHub Actions `eyes` reaction on the command. Only HTTP 201 starts
   execution; HTTP 200 (already exists), timeouts and ambiguous responses deny it.
5. One of four durable reaction slots on the human root (`rocket`, `hooray`,
   `heart`, `+1`). These slots belong to the workflow identity. Concurrent requests
   compete for the same slots, and deleting bot comments does not replenish them.
   Exhaustion escalates to an owner command rather than resetting automatically.

The gate receives only the job-scoped GitHub token with issues-write for these
claims, contents-read and PR-read. No provider secret or App private key is in that
job. Generation still has no GitHub write credentials, uses the existing isolated
filesystem and model proxy, and cannot publish. Publishing still runs separately
with its short-lived App token, verified GitHub signature, protected paths and
exact-SHA update checks. The new policy/authorization scripts and their tests are
also protected from agent publication. Release verification now rejects commits
unless `verification.verified` is boolean true and `verification.reason` is `valid`.

The four slots bound this OpenCode ingress. They do not magically enforce other
providers' private integrations; do not enable unrestricted bot inputs there.
Deleting root reactions is privileged recovery, never an automatic retry strategy.
If generation fails after a claim, the slot remains consumed. A new owner command
is the auditable recovery action. Do not delete another actor's reactions in order
to replay a request. Existing human requests retain their previous provider behavior;
this patch does not claim to deduplicate human workflow reruns.

`orchestration_policy.py` supplies an executable, tested policy library for the
requested states, observed-capability fallback, exact HEAD/base invalidation,
same-PR independent review evidence, signed commit-range bindings, required gates,
idempotent events, revision preconditions, a hash-linked audit history, three
CI attempts (remediation state entry itself does not consume another attempt) and
an absolute one-hour execution deadline. The hash chain
detects inconsistency; it is **not** a signature or tamper-proof storage system.

**There is no deployed controller consuming this library.** Its facts are a trusted
collector interface, not a JSON authorization API. Never construct them from an
agent's comment, verdict prose, PR body or uploaded artifact. The collector must
fetch complete commit/check/review evidence from authenticated APIs, bind the
commit range to current base/HEAD, authenticate review provider and implementer,
verify all unresolved findings and dependencies, and atomically persist state by
revision compare-and-swap before issuing any action. Same-PR review comments are
required for traceability but cannot constitute acceptance evidence alone.
Independent acceptance and automatic merge eligibility are separate: a high-risk
change may be independently accepted without qualifying for automatic merge.
Risk considers both sides of renames and rejects symlinks/unknown file kinds as
high risk. The collector must prove complete file metadata, not just supply names.
`BLOCKED` and `FAILED` are terminal records: recovery requires a new, linked task
identity and new human authorization; it never silently renews the old task budget.
Low-risk documentation is the only planned auto-merge class; all other scope
requires protected owner approval. Deployment transitions always reject pending
a validated adapter. The library does not merge or trigger any provider.

## Configuration and activation

Keep `DCIM_TRUSTED_AGENT_IDENTITIES` unset until identities, installation scopes
and provider behavior have been verified. Its strict JSON schema is:

```json
[{"id":123,"login":"verified-agent[bot]","app_id":456,"app_slug":"verified-agent"}]
```

The numbers above are placeholders, not configured identities. There is no
wildcard or default trust. Numeric IDs must come from GitHub, and permissions must
be read from the actual installation. `OPENCODE_ALLOWED_ACTOR_IDS` continues to
govern the human root. A workflow must be on the default branch to receive native
issue-comment events; a Draft PR cannot prove live triggering. Initial foundation
changes are high risk: do not merge before exact-HEAD independent acceptance,
security/mutation CI, and owner review of the activation configuration.

Native Claude reviewed the initial signed HEAD on PR #211, comment `6421352765`,
and returned **NOT ACCEPTED**. Its correctness findings prompted remediation of
comment comparison, explicit OpenCode root binding, retry counting, changed-HEAD
state handling, acceptance/risk separation, file-kind/rename risk, terminal recovery
semantics and non-JSON evidence errors. A new HEAD requires renewed review. Claude
reported that its current tools cannot publish a formal review or App-bound
acceptance check. Comment-only review is not a substitute for that missing gate.

Before an autonomous controller may be activated:

- Resolve #207's separate Claude authority model and native action bot restrictions.
- Verify every installed App's identity, short-lived token capability, least-privilege
  permissions and event subscriptions. Keep workflow/administration/merge bypass
  permissions out of implementation agents. Do not broaden GITHUB_TOKEN globally.
- Prove each provider's native development/review triggers and failure signals in
  a bounded same-PR experiment. Codex/Copilot support, subscription, quota, bot
  initiator restrictions and review eligibility are provider-side settings; DCIM
  cannot infer them or configure them by adding a workflow or mentioning a bot.
- Implement the authenticated evidence collector, atomic durable state store,
  task-to-existing-PR mapping, native dispatch adapters, watchdog and independent
  acceptance check. Feed fallback only recent successful capability observations;
  missing credentials and previous successful text are not readiness probes.
- Add the independent acceptance/security gate as an App-bound required check,
  enforce signatures and current independent approval, audit all bypass actors,
  and protect orchestration/security changes with owner approval. Do not use a
  bypass token or force-merge. Reinspect settings before each activation.
- Validate remediation and fallback end-to-end with bounded retries. A new HEAD or
  base invalidates prior review, CI and merge eligibility. An implementation agent
  must never accept its own change, even through another model session.

## Deployment architecture and precise blockers

`deploy.yml` remains validation-only. It does not start services. The existing
Compose runner builds on the destination and has code/config rollback, but cannot
establish immutable artifact promotion or safe reversal of database migrations.
Neither a verified staging host nor deployment credentials, production environment
protection, operational equipment isolation, DCIM test integrations, canary target,
backup restore proof or rollback procedures were supplied/verified in this task.
Local Docker availability is not a verified staging target.

Stage A must build backend/frontend images once from an exact verified release
SHA, run full tests/migrations/security/dependency checks, produce immutable image
digests, SBOMs and signed provenance, and verify those attestations before use.
Do not rebuild on staging or production. Record release SHA, digest, workflow/run
attempt and test results together; reject mismatched or stale evidence.

Stage B needs a real isolated staging target, host identity/OIDC audience, protected
environment and narrowly scoped short-lived credentials. Use the same verified
artifact, lock concurrent deployments, back up the database and validate
expand/contract migrations before switching traffic. Check database/Redis/API
readiness, browser workflows and DCIM integrations against a synthetic equipment
boundary, then monitor health over a configured window. Failure must restore the
previous artifact/config only if schema compatibility is proven; otherwise safe-stop
and escalate. Do not automatically downgrade a migrated production database.

Stage C must reuse the staging-verified digest through a protected production
environment, use a validated canary/traffic switch where supported, and retain the
previous compatible release plus recovery evidence. Runtime health failure must
roll back or safe-stop with an audited alert. Protect approval consumption against
replay and revalidate exact release identity immediately before promotion.
**Production auto-promotion remains disabled.** Neither comments nor a supplied
`production_enabled=true` value can activate the current policy library.

No agent is authorized to operate live power, cooling or equipment control systems.
Deployment credentials and integration tests must be technically isolated from
such systems before activation. No merge, deployment, rollback of a real target,
App installation or production notification is represented as completed here.

## Validation and records

Relevant commands are `python3 -m unittest discover -s tests/deployment -p
'test_*.py'`, `python3 -m unittest discover -s tests/ci -p 'test_*.py'`, and
`python3 tests/deployment/check_workflows.py`. New negative cases cover identity
spoofing, App mismatches, absent human authority, stale HEAD/events, recursion,
duplicate claims, budget exhaustion, ambiguous claims, stale review/CI,
self-acceptance, invalid signatures, retry limits and disabled deployment.

The existing privileged mutation suite must run on its disposable GitHub-hosted
runner; skips or missing kernel capabilities are not acceptance. The R-8 mutant
was updated to continue removing the human ID check after the new bot branch.
Independent local review found that regression and missing commit-range binding;
both were remediated. Final test counts, signed SHA, Draft PR and review request
are reported in the PR/chat, without claiming an all-green result when blocked.
