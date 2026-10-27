# Collaboration controller: Draft implementation, inactive

This scope excludes automatic merging and all deployment work. Neither the
workflow variable nor the protected environment nor the state branch is created
by this change. All providers and the controller remain disabled. Passing unit
tests does not establish real agent-to-agent execution.

The reconciler in `orchestration_controller.py` runs from verified current main
only. It uses the existing native OpenCode App in a deterministic trusted runner;
no model receives its journal/comment token. It does not impersonate Claude or
Codex. The App's actual numeric actor ID, App ID and slug must be authenticated
before filling `issuer`. The OpenCode identity remains unknown rather than guessed.
A native comment can awaken another integration; a GITHUB_TOKEN comment cannot be
substituted because GitHub intentionally suppresses most recursive workflow events.

## Durable identity, claims and recovery

An owner initializes the dedicated `automation/collaboration-state` branch with a
GitHub-verified signed commit and `.github/agents/state.json` containing exactly
`{"version":1,"revision":0,"tasks":{}}`. The controller never creates or resets the
branch. The branch must be limited to the reviewed controller issuer and owner;
repository rules must prevent other same-App publishers modifying this journal.
The runtime hard-pins this branch and path and refuses using the default branch.
No bypass was used: this controller has never been activated. An App listed as an
always-bypass actor is not an approved journal writer until the owner independently
verifies appropriately scoped, effective state-branch rules. A protected=true flag
alone cannot prove absence of a bypass. State updates must satisfy normal signed
commit and actor rules; they must not depend on bypassing a PR requirement.
The controller verifies signed state commits and expected author identity before
reading or dispatching. GitHub Contents API SHA compare-and-swap rejects concurrent
writers; a rejected or ambiguous write never authorizes delivery. Each successful
state write is independently fetched and must report verification true/valid.
The workflow globally serializes reconciliations, and scheduled scans compensate
for GitHub concurrency groups retaining only one pending callback. Every reconciliation
scans fresh owner commands on enrolled PRs as well as journaled tasks, so even an
initiating event displaced before its first write can be recovered. Discovery
preserves the initiating timestamp and never renews an existing root's budget.

Use an existing, fresh owner `/oc review ...` or `/opencode review ...` comment
ending with `DCIM-Head: FULL_HEAD_SHA` as the root. Start one task with a separate
fresh owner `/collaborate ROOT_COMMENT_ID` comment on the same existing PR. The
root remains compatible with the unchanged OpenCode authorization parser and may
itself start native OpenCode review; do not represent this as a Claude handoff.
Only owner-enrolled PRs are eligible. No PR is created. Task identity is stable
across repeated initiating events. Edited roots, changed HEAD/base, deleted roots
and stale authorization block execution. Reaction counters do not invalidate a
root. A new root creates a new explicitly authorized task, not a silent retry-budget
reset of an existing task.

Every native delivery has a deterministic nonce and full exact command, saved
before POST. On interruption the next reconciler searches complete API history
for exactly that command and authenticates its actual App/actor and timestamps.
One match recovers delivery; duplicate matches fail closed. The final reserved
budget slot is recovered before checking whether another delivery is allowed. No match escalates:
there is no automatic resend after an ambiguous write. Owner recovery requires
inspection of the recorded execution and consumed delivery; never delete journal
entries or clear claims to retry. Failed authenticated provider results permit
bounded fallback; pending/quota-limited executions expire at the task deadline.
Maximum task lifetime is one hour, three attempts per provider and four total
deliveries. History is append-only within a task with chained audit digests. At
100 tasks archival requires owner-controlled review; no automatic pruning occurs.

## Native integration evidence contract and remaining adapter work

Provider selection requires a current, successful native capability execution,
not a secret's existence, a comment's prose or an integration's brand name. Each
provider remains disabled until an independent proof adapter produces immutable
`dcim-agent-result-attempt-N` artifact from its approved default-branch native
workflow. The configuration pins run ID, workflow ID/path and required jobs. The
controller authenticates repository, run attempt, current main SHA, workflow file,
job/check links and Actions App 15368, then the artifact's SHA-256 and source run.
The structured result contract is:

```json
{"provider":"claude","mode":"review","result":"SUCCESS","task_id":"pr-211-root-123","delivery_id":"DETERMINISTIC_NONCE","binding":{"repository":"AhmedMahmoud2222/DCIM","pr":211,"head":"FULL_SHA","base":"FULL_SHA"},"provider_comment_id":456,"source_sha":"TRUSTED_MAIN_SHA","run_id":789,"attempt":1,"policy_digest":"CANONICAL_COLLABORATION_POLICY_SHA256"}
```

OpenCode may instead reference `provider_review_id` (exactly one reference is
allowed). That native PR review must target the exact HEAD on the same PR with
COMMENTED state and authenticated App metadata. Missing App metadata blocks the
adapter; provider names in text cannot replace it. All result artifacts additionally
bind current trusted main, actual run ID/attempt and the collaboration policy digest.

The provider result comment must be fresh, on the same PR, and authenticated by
pinned actor ID/login and App ID/slug. It is execution evidence only. A result
cannot create acceptance authority. The trusted acceptance `verify_check`
collector independently requires complete current signatures, CI/security,
independent structured review, unresolved-finding resolution and initial owner
attestation. Only that collector can produce controller state ACCEPTED.

The new `orchestration_opencode_result.py` adapter is wired into native OpenCode's
separate trusted review publisher, gated by the inactive collaboration variable.
It reauthenticates the original fresh delegation, human root and exact HEAD/base,
fetches the actual native COMMENT review and verifies its publisher run binding.
Because review REST may omit App metadata, the native publisher then posts a
fixed, fresh execution witness using its actual short-lived App token. The
adapter verifies that response's actor and App IDs before sealing the artifact.
It records `native_review_id` alongside the authenticated `provider_comment_id`;
it never interprets review prose as acceptance. Ordinary owner reviews are
unaffected. This adapter has unit coverage but has not run live.

Claude progress comments do not emit the full sealed delivery/result contract.
They must not be wrapped by copying prose into an artifact. A trusted Claude
adapter must bind the native command event and execution before sealing.
The controller currently stops at BLOCKED/ESCALATED where such proof is absent.
Implementation/remediation adapters are intentionally refused by the command
builder until their authority and publication protections have been proven;
current native dispatch is review-only. Thus this is an implemented controller
skeleton with durable execution and tested recovery, **not a completed autonomous
remediation system**. The pinned official Claude action defaults to rejecting bot
triggers; its existing workflow does not enable an authenticated bot adapter.
Editing a Claude progress comment cannot generate a fresh OpenCode command.
Codex/Copilot native assignment availability and permissions
remain to be verified. Copilot has no invented mention-command adapter.

## Activation boundary

These new trusted workflows cannot run securely from PR #211 to certify their own
feature-branch implementation. Initial installation needs independent non-author
acceptance and explicit owner review; PR #211 remains Draft and unmerged. This
change does not set the activation variable, register provider identities, create
state/environment resources, request new credentials or change repository rules.
Native Claude → OpenCode, OpenCode → Claude, Codex and Copilot handoffs remain
unverified until real initiating events, native App identities, run IDs, exact
SHA/result and replay/recovery evidence are captured. The initial owner-authorized
root never permits changing orchestration/security files, merging or deploying.
