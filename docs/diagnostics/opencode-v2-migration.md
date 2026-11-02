# OpenCode v2 migration: runtime, provider compatibility and fail-fast

Status: 2026-10-28. Scope: `.github/workflows/opencode.yml` agent path and the native smoke diagnostic.
No real-key request was made while producing this document; every measurement below used the genuine
`@opencode/cli` 2.1.4 binary against a loopback scripted provider and a synthetic key.

## 1. Selected runtime

| | v1 (before) | v2 (now) |
| --- | --- | --- |
| npm package | `opencode-ai` 1.19.3 | `@opencode/cli` **2.1.4** |
| binary in the lockfile install | `node_modules/opencode-linux-x64/bin/opencode` | `node_modules/@opencode/cli-linux-x64/bin/opencode` |
| `--version` output | `1.19.3` | `opencode v2.1.4` |

2.1.4 is the release named in OpenCode's own guidance (issue 59387) and is five days older than `latest` (2.2.0,
published 2026-10-27, deliberately not taken). The lockfile pins all 12 platform packages with sha512 integrity and
`os`/`cpu` selectors; `npm ci --ignore-scripts` installs from it and the workflows assert the exact version string
before any credential is mapped. The linux-x64 tarball was checked against the lockfile integrity.

## 2. What changed in the CLI contract (measured)

* `opencode run --standalone --format json --model M -- PROMPT`. There is **no** `--pure` and **no** `--dir`; the
  work tree is the process cwd and `$PWD` (Bun resolves relative paths from `$PWD` when set, so the guard sets it
  explicitly under `env -i`). `--standalone` avoids the shared background service.
* `OPENCODE_PURE` is gone. The guard sets `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_CONFIG_PROJECT_DISABLE`,
  `OPENCODE_DISABLE_CLAUDE_CODE`, `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`, `OPENCODE_DISABLE_MODELS_FETCH`,
  `OPENCODE_DISABLE_AUTOUPDATE`.
* The legacy config keys (`provider.opencode.options.baseURL`, `enabled_providers`, `small_model`, `share`,
  `permission`) are still accepted (marked deprecated in the published schema). The v2-native `providers` object has
  no `baseURL`, so the loopback proxy still depends on the legacy provider keys; the e2e tests pin that behaviour.
* Tool names changed (`bash` is `shell`; `edit` covers write/patch) and v2 adds `execute` (Code Mode), `subagent`,
  `skill`, `question`.

### Security-relevant finding: v2 headless is allow-by-default

With no permission config, a headless v2 run **completed** `write`, `shell` and `execute` calls. The v1-style
`permission` keys do not cover `execute`. The agent config therefore also carries a native `permissions` list that
opens with `{action:'*', resource:'*', effect:'deny'}` (last match wins), allows only what each mode needs, and
denies `execute`, `subagent`, `skill`, `question`, `webfetch`, `websearch` and `external_directory` last. Verified
against the real binary for implement, review and probe modes (`tests/deployment/test_opencode_e2e.py`): `.env` and
out-of-tree reads are refused, review cannot edit, probe is offered no tools, the v2-only tools are not offered.

## 3. Event stream (guard v4)

* A reply without tool calls is a lone `text` event (no `step_*`); after tool calls the closing reply is a bare
  `text` after the last `step_finish(tool-calls)`. Measured stable over repeated runs. The guard accepts exactly
  that, last and non-empty; text before/between steps or after a `stop` step is rejected.
* Truncation: `step_finish(length)` then an `error` event, exit 1. 401: an `error` event, exit 1.
* **429, 5xx, cut and empty streams: v2 retries internally and forever** with only repeated `step_start` events and no
  `error` event until it is killed. This reproduces the reported symptom (probe waits for the 120 s limit).

## 4. Provider compatibility finding

Root cause of "wait until timeout": confirmed and fixed (section 5): the client never gives up on a 429.

Root cause of the **429 itself: not determined, and cannot be from here.** What the evidence supports:

* OpenCode's published terms: free-tier chat models "require OpenCode"; other harnesses receive HTTP 403, even with a
  Console key (`opencode.ai/v2/docs/console/{models,harnesses}`). v1 "no longer supports free models" (issue 59387),
  so a v1 client being refused is consistent with the history; a v1 refusal would more plausibly be a 403 than 429.
* The sandboxed client is the genuine OpenCode CLI; the proxy is a loopback relay that substitutes the credential.
  Before this change it forwarded only `content-type`, `accept`, `user-agent` and `x-opencode-request-id`, dropping
  every identity header the real v2 client sends (`x-opencode-client: cli`, `x-opencode-project`,
  `x-opencode-session[-id]`, `x-session-id`, `x-session-affinity`; user agent `opencode/latest/2.1.4/cli`).
  The proxy now relays those headers **unchanged and only when well formed** (`x-opencode-client` only as the literal
  `cli`); it never creates, completes or rewrites one. This is not impersonation: it stops stripping what the real
  client said about itself.
* Whether the provider accepts a credential-substituting relay in front of the genuine client, and whether account
  quota or model availability is the actual 429 cause, is **unverifiable without a real request**. The status code
  alone does not distinguish rate limit from exhausted quota, so no such category is claimed.

If the one manual probe is still refused (403 with identity headers relayed, or 429), the correct conclusion is an
architectural incompatibility between "free tier requires the OpenCode client" and "credential isolated behind a
proxy". Do not weaken the sandbox. Owner-approved alternatives to evaluate: a paid or Go-plan model (the harnesses
page allows other clients with those), or running the unmodified client with the key under a different isolation
(for example a dedicated disposable runner with no repository secrets), which is a separate security decision.

## 5. Fail-fast (proxy to guard)

The proxy appends `provider_rejection` records (status-derived category, count, `terminal`) to `OPENCODE_PROXY_LOG`
and never reads or logs response bodies or headers. The guard polls that file while the agent runs, kills the agent
account when a record is terminal, and reports the sanitized category as the failure reason (it never overturns a
success).

| HTTP status | category | terminal after |
| --- | --- | --- |
| 401 | `provider_auth_rejected` | 1 |
| 402 | `provider_quota_exhausted` | 1 |
| 403 | `provider_http_403` | 1 |
| 429 | `provider_http_429` | 2 |
| 5xx, failed upstream connection | `provider_unavailable` | 3 |
| other 4xx | `provider_http_<status>` | 1 |

Not distinguished (insufficient response information): `provider_rate_limited` vs quota on 429, and
`provider_client_unsupported` vs another policy refusal on 403.
Residual: a stream cut mid-response, or an empty stream, still retries until the hard timeout.

## 6. Owner instruction for the single production probe

After merge, run one `/oc probe`. Expected: `PROBE_OK` acknowledged within the 120 s limit, or a failure within
seconds naming `provider_*`. A `provider_http_403` or repeated `provider_http_429` with identity headers relayed
means the incompatibility in section 4; stop and decide on an alternative rather than retrying.
