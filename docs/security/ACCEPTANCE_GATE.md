# Trusted acceptance gate for PR #211

This change implements P0 code and P1 preparation. It does not merge, deploy,
enable agent trust, modify repository settings or authorize production promotion.
Staging, deployment infrastructure and release automation are deferred to a
separate phase. They are not prerequisites for collaboration or acceptance tests.

## Trust boundary and evidence flow

Three new workflows run only on `refs/heads/main`, checking out
`github.workflow_sha`, never the PR merge/head ref. Their scripts also verify the
workflow ref, current signed main SHA, API workflow identity and exact source bytes
of the workflow, policy and adapter. Candidate patches are JSON data only.
No `pull_request_target`, candidate installation, executable PR artifact, custom
AI dispatcher or blanket write permissions are introduced.

1. The owner posts the exact `/acceptance-review` command on an enrolled same-repo
   PR. Only owner numeric ID/login/type may initiate this first version. There is
   no wildcard bot authorization or automatic cross-agent trust activation.
2. Trusted preparation fetches exact PR/head/base, complete patches and prior
   findings. Missing/binary/truncated patches reject rather than invite a guessed
   acceptance. The protected registry identifies the implementer; unknown tasks
   reject. Claude and Codex IDs/App IDs are pinned from observed GitHub metadata.
3. The pinned official Claude action returns schema-constrained `structured_output`
   with exact binding, reviewer, ACCEPT/BLOCK, findings and explicit resolved IDs.
   Ordinary Claude comments and review-job success are never acceptance evidence.
4. A trusted SessionStart hook uses the native App token before the action revokes
   it. It posts a fixed identity/run/binding witness and validates API-returned
   user ID/login/type and App ID/slug. The witness contains **no verdict**. A local
   exclusive-create record prevents duplicate posts after ambiguous failures.
5. Native agent mode persists its token in `.git/config`; the startup hook removes
   it before any network work/model turn. An exclusive PreToolUse guard permits
   only Read of the prepared context file and the native StructuredOutput result
   transport. It denies git configuration, credentials, process environments,
   arbitrary files, shell, write, network and MCP tools. Static deny rules add
   defense in depth. `--allowedTools` alone is not claimed as a sandbox.
6. A fresh sealing job independently validates JSON, native execution/check suite,
   App witness, session, current binding and output schema. It posts a same-PR
   finding record and uploads one immutable `evidence.json`. The model never
   receives the sealing/acceptance write token. Provider keys are scoped solely
   to the native action. The sealed record itself is still untrusted until its
   source run and artifact integrity are re-authenticated by the gate.

The official action behavior was checked at commit
`925afb53776cf1a2b738c6bdbed37b4d4000ef13`: `src/modes/detector.ts` selects agent
mode for a nonempty prompt; agent mode does not create a tracking comment.
`src/entrypoints/run.ts` supplies the native token to GITHUB_TOKEN/GH_TOKEN before
SDK execution, and settings support SessionStart/PreToolUse hooks. The deterministic
hook therefore avoids relying on a nonexistent progress comment or enabling tag
mode's candidate checkout/write tools. Live execution of this new combination
remains an activation test, not an already observed result.

## Owner attestation and independence

`acceptance-owner.yml` is an owner-only main dispatch with exact PR/head/base
inputs. It requires a real owner approval from the run's environment-approval
audit for `acceptance-owner`. An absent or unprotected environment cannot create
that API record, so merely dispatching the workflow does not attest anything.
This task does not dispatch it or provision the environment on the owner's behalf.

The owner can be the PR author for this **separate authorization**, but never
counts as the independent reviewer. Claude must differ from both implementer and
PR author by provider name and numeric account/App identities. Current registry
enrolls only PR #211 (implementer Codex). Expansion requires reviewed trusted
configuration; no identity is inferred from PR prose, branch names or signatures.

Every acceptance currently requires the owner attestation. This intentionally
does not activate routine autonomous acceptance; a later approved risk policy and
operating controller must determine which eligible tasks may omit a human gate.

## Gate, complete evidence and check forgery

`acceptance-gate.yml` runs on relevant workflow completion, a bounded schedule or
main-only dispatch. It inspects enrolled open PRs, with a three-job parallelism cap
and timeouts. Its only write permission is checks-write; it holds no provider or
deployment secret. It publishes `DCIM independent acceptance` under GitHub Actions
App ID 15368. BLOCK is a failing check, not neutral/skipped/success. A successful
workflow run alone does not mean its decision was ACCEPT.

The collector verifies:

- Current open same-repository PR and exact number/head/base; base is current main.
- The full paginated base-to-head commit range, uniqueness and endpoint binding;
  every commit is independently re-fetched and GitHub verified=true/reason=valid.
- Authenticated CI workflow IDs/paths, exact PR/base/head, latest run/attempt,
  successful required jobs, job-to-check links, App 15368 and check-suite IDs.
  Candidate changes to its own CI workflow reject. Every mutation job in the
  trusted inventory is required, alongside control and aggregate, not just a
  green aggregate name. CI proves execution/results, not semantic correctness of
  changed code/tests; independent review and owner authorization remain separate.
- Latest native review execution, not an older ACCEPT after a failed/new request;
  trusted main source, owner initiator/re-run identity, complete successful jobs,
  one nonexpired artifact, SHA-256 archive digest and exact run/attempt/source/policy.
- Exact structured verdict and no unresolved blocking findings, including prior
  authenticated records. Existing unresolved GitHub threads or change requests
  independently block. Old finding IDs must be explicitly resolved by the new
  independent reviewer. Artifact expiry/policy upgrades require the reviewed
  owner checkpoint mechanism; history is never silently ignored.
- A separate authenticated, owner-approved exact-SHA attestation.

Downloads never forward the GitHub Authorization header to blob storage. Only a
bounded HTTPS storage redirect and one bounded JSON archive member are accepted;
duplicate JSON keys, duplicate artifacts, inconsistent pagination, symlinks and
ambiguous records reject. No downloaded source code is executed.

**Shared App/name binding alone is insufficient.** A different workflow can also
produce a GitHub Actions check. Consumers must run, from trusted main:

```sh
python3 .github/scripts/acceptance_gate.py verify-check --pr 211
```

This validates the external-ID proof, latest gate workflow ID/path/run/attempt,
trusted source revision/bytes, successful native job/check suite, immutable decision
artifact and digest, then re-collects current signatures, CI, review, findings and
owner authorization. A copied same-named green check or details URL cannot grant
acceptance. A new gate execution supersedes an old decision. A new HEAD/base,
failed rerun, dismissed/blocked review or changed evidence invalidates it.

For native GitHub merge protection, a shared-App status requirement still cannot
express all of that provenance. Required-workflow enforcement, where available,
or a separately scoped issuer is needed before relying on status-only native
merging. Automatic merge remains disabled; the future controller must verify the
proof directly and must never use a protection bypass.

## P1 audit and initial bootstrap

The re-read of ruleset 23651422 found six always-bypass actors: five integrations
(842251, 946600, 1144995, 1236702, 4229212) and owner 285010264. Only Codex
1144995, Claude 1236702 and owner identity are verified here; other App names and
installation permissions are not guessed. The ruleset still requires zero
approvals and has no required-signatures rule. Existing backend/frontend checks,
thread resolution, strict checks and history protections are preserved.

`.github/acceptance/protection-plan.json` prepares signature enforcement, current
non-author approval and acceptance/security checks. It applies nothing. Each
bypass requires owner justification/removal; no bypass is used. A formal non-author
approver is needed for the proposed approval count, independently of the author's
owner attestation.

The new workflows are absent from main until reviewed installation. They cannot
certify their own feature-branch source; main-only checks deliberately refuse that
shortcut. With the current no-merge instruction, initial bootstrap remains blocked
on independent acceptance and explicit owner-controlled installation/authorization.
Do not claim a live acceptance run, dispatch from the feature branch, weaken the
guard or turn previous Claude prose into approval to break this bootstrap boundary.

## Operations and recovery

After approved installation and environment setup, request native structured
review, then have the owner dispatch and approve the exact-SHA attestation. Gate
reevaluation happens on workflow completion/schedule; no per-job manual triggering
is needed. A BLOCK record includes its reason. Repair missing evidence and request
a fresh review when binding or policy changes. Do not delete a denied claim or
fall back to an older accepted artifact. Historical migration requires an explicit
owner-signed main checkpoint preserving open findings (see the checkpoint README).

All three workflow control files, policy/checkpoints, adapter scripts and regression
tests are protected from OpenCode publication. Signed commits and the existing
separate publisher/sandbox remain in force. No action merges, approves a formal
PR review, creates another PR, deploys, provisions credentials or operates live
power/cooling equipment. Live provider proofs remain required before activation;
the collaboration controller is documented in `AGENT_COLLABORATION.md`.
