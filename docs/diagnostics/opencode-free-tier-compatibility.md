# OpenCode free-tier compatibility: native CLI smoke (`read: ask`)

Status: diagnostic note, 2026-10-23. No production component is changed by the
document this note accompanies. Scope: the native CLI smoke diagnostic
(`.github/scripts/opencode_native_smoke.py`,
`.github/workflows/opencode-native-smoke.yml`) and observations about the
catalog/proxy surface that are recorded here only.

## 1. Symptom

GitHub Actions run
[39972207857](https://github.com/AhmedMahmoud2222/DCIM/actions/runs/39972207857)
(workflow_dispatch, `main`, commit `a2672d5`) failed with:

```
status failed reason=provider_error error_class=forbidden error_status_code=403
exit 0
```

That run used the merged #196 configuration, which set `read: 'deny'` in
`opencode_native_smoke.py`. It was a **diagnostic false negative**, not evidence
that production inference is unavailable.

Upstream reference: <https://github.com/anomalyco/opencode/issues/50627>

## 2. Root cause (controlled evidence)

Same pinned runtime (`opencode-ai 1.19.3`, the checked-out lockfile), same fixed
model (`opencode/glm-5.3-flash-free`), same fixed prompt, isolated `HOME`/XDG and
an empty workdir. Only the `read` permission key differs. The probe uses a
synthetic, non-credential key, so it cannot succeed; what matters is the failure
layer:

| `permission.read` | Gateway response | Meaning |
| --- | --- | --- |
| `deny` | HTTP 403 (`forbidden`) | request envelope rejected at the policy layer, before auth |
| `ask` | HTTP 401 (`auth_error`) | envelope accepted, authentication reached |
| `allow` | HTTP 401 (`auth_error`) | envelope accepted, authentication reached |

`read: 'deny'` removes the `read` tool from the outbound tool list. The free tier
rejects that changed envelope with a pre-auth 403 even when the credential and
network path are valid. `read` itself is unrelated to inference; denying it is a
policy choice that the free tier does not accept.

## 3. Headless semantics of `read: ask` (1.19.3)

Verified with the real pinned binary and a scripted OpenAI-compatible stub (only
model replies are scripted; the CLI, permission engine and event stream are the
genuine runtime). A canary file lives in the smoke workdir and the scripted model
asks to read it. Every request body is recorded.

| Variant | Events | Canary in any request | Wall time | Exit |
| --- | --- | --- | --- | --- |
| `read: allow` (control) | `tool_use(read,completed)` then text | **yes** | ~4.8 s | 0 |
| `read: ask`, stdin `/dev/null` | `tool_use(read,error)`, `step_finish(reason=tool-calls)` | no | ~4.1 s | 0 |
| `read: ask`, stdin open pipe | `tool_use(read,error)`, `step_finish(reason=tool-calls)` | no | ~4.1 s | 0 |
| `read: deny` | tool is not offered at all | no | ~3.9 s | 0 |

Conclusions for the diagnostic:

- `ask` is **not** silently auto-approved in headless mode: the read tool call
  ends in `error`, and the canary content never appears in a follow-up request.
- It does **not** hang. Both a closed stdin and an open-but-idle pipe finish in
  seconds. The runner additionally passes `stdin=subprocess.DEVNULL` and bounds
  the run with a timeout.
- The CLI exits `0` even when the permission layer rejects the tool, so a
  transcript-level guard is mandatory. The runner's `evaluate()` rejects **any**
  `tool_use` event as `unexpected_tool_use`, regardless of tool or status, and a
  last `step_finish(reason=tool-calls)` is also not an accepted termination.

## 4. Correction

Diagnostic-only change:

- `PERMISSIONS['read'] = 'ask'`; every other tool of the pinned runtime remains
  `deny` (`edit`, `bash`, `webfetch`, `external_directory`, `task`, `todowrite`).
- Credential-free **preflight**: a step before the secret-bearing step sends the
  identical fixed envelope with a non-credential placeholder key. It passes only
  when the gateway answers 401; a 403 fails the job with
  `reason=config_envelope_rejected` *before* `OPENCODE_API_KEY` is mapped. The
  preflight never reads the environment key (`resolve_key('preflight', ...)`
  always returns the constant placeholder). A passing preflight records
  `mode=preflight`, `status=ok`, `reason=credential_rejected_as_expected`,
  `error_class=auth_error`, `error_status_code=401`; the CLI's own exit code is
  nonzero (auth rejected), which is expected and is not a probe failure.
- The live step keeps the same fixed model/prompt, isolated home/workdir,
  closed artifact field list and protected `opencode-agent` environment.

Defence in depth: config-level denial of all non-read tools; headless `ask` never
executes a read; transcript-level rejection of any tool use; bounded timeout;
sanitized artifact (`status`, `reason`, `error_class`, `error_status_code`,
counts and elapsed time only).

## 5. Reconciliation with the production agent policy

The production agent configuration (`opencode_sandbox.py`, `agent_config()`) uses

```json
"read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"}
```

inside a **sanitized worktree** (untracked files, symlinks, untrusted agent
configuration and credential-like files are dropped), with
`external_directory: deny`, a separate OS user and a loopback-only model proxy.
Read availability there is a reviewed, intentional part of the sandbox.

The diagnostic runs no repository source at all: empty workdir, isolated
`HOME`/XDG, no `.git`. Its `read: ask` keeps the outbound envelope compatible
with the free tier without executing reads (section 3). Therefore:

- a 403 produced by the diagnostic's own `read: deny` must **never** be read as
  "production inference is unavailable";
- the corrected diagnostic's 403, if one ever appears, means the envelope or
  credential policy was rejected — not that the model is down.

## 6. Catalog endpoint and model-proxy compatibility notes (not changed here)

Recorded for follow-up; no code in this repository section was modified.

**Catalog.** `.github/scripts/opencode_catalog.py` tries
`https://opencode.ai/inference/openai/v1/models` first, then the legacy
`https://opencode.ai/zen/v1/models`. In the pinned 1.19.3 binary the built-in
provider definitions are:

- provider `opencode` → `https://opencode.ai/inference/openai/v1`
- provider `opencode-go` → `https://opencode.ai/inference/go/openai/v1`

The literal `/zen/v1` does not occur in the 1.19.3 binary. The legacy fallback
(and the proxy's matching GET allow-list entry) is stale relative to the pinned
runtime: a retired route can return 403/404 that is easy to misstate as a model
or account problem. Recommended: treat the primary inference endpoint as the
only authoritative catalog route; remove or explicitly mark the legacy fallback
in a separate reviewed change.

**Model proxy.** `.github/scripts/opencode_proxy.py` forwards only
`POST /inference/openai/v1/chat/completions`, and `GET` only
`/inference/openai/v1/models` (plus the stale `/zen/v1/models`). It binds one
registry-approved `-free` model per instance and rejects a mismatched body
`model`. Compatibility risks:

1. **Path/version drift**: if upstream moves or versions the inference path,
   the proxy returns its own 403 `endpoint_not_allowed`. That is a different
   layer from an upstream 403 and must be classified separately.
2. **Envelope sensitivity**: upstream free-tier policy can reject a request
   envelope that the proxy considers valid (this `read: deny` case is the
   example). Proxy acceptance does not imply upstream acceptance.
3. **Registry gating**: `opencode/glm-5.3-flash-free` is still marked
   `terms_verified: false` / `repo_code_approved: false` in
   `.github/opencode/model-registry.json`. Inference compatibility in a
   diagnostic does not authorize production routing; the flags remain the
   gate.
4. **Diagnostic vs production path**: the native smoke talks to the gateway
   directly with the standard provider integration, while production talks
   through the loopback proxy. A success/failure in one path does not transfer
   automatically to the other.

## 7. Recommended controlled next experiment

No merge, no model activation, no real-key dispatch from this change set.

1. Owner dispatches the corrected native smoke **once** on trusted `main`, with
   the real key confined to the protected step. Expected: preflight
   `status=ok`/`error_class=auth_error`/401, then live `status=ok` with
   `step_finish(reason=stop)`.
2. If the live step is `ok`: the earlier 403 was the diagnostic envelope
   artifact. Any remaining disagreement with the direct diagnostic
   (`model_health_diagnostic.py`) is then a client-construction/header
   difference on that diagnostic, not an inference outage.
3. If the live step is still 403 with `read: ask`: stop treating it as a model
   availability signal. Compare the artifact's `error_class`/
   `error_status_code` with the preflight result and investigate key entitlement
   or runner egress in a separate reviewed change.
4. Separately, and outside this diagnostic, confirm the catalog route and the
   proxy path against a live 401/200 response before changing either; keep the
   registry flags as the production gate.

## 8. Explicit non-goals

This note and the accompanying diagnostic change do **not** modify the
production model registry or health flags, the reviewer capability matrix, the
sandbox/proxy/publisher, branch protection, the direct model-health diagnostic,
or any other workflow. No production model is activated.
