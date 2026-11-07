# Issue #107 M2: read-only assistant architecture and implementation contract

**Document version:** 0.1.2 (2026-10-20; section 13.1 and section 7b added 2026-11-07). **Everything below is PROPOSED. Nothing is implemented.** Existing-behavior claims cite `main@92be692`; see the [main document](ISSUE_107_AI_OPERATIONS.md).

## 1. Principles

1. The assistant is a read-only client of existing services. It holds no database role of its own with wider reach than the calling user.
2. An AI-specific permission gates the feature only. It never grants access to an underlying record.
3. Authorization and scope filtering happen before retrieval, aggregation and ranking, never after.
4. The model sees a typed evidence bundle, never raw tables, SQL, a shell, Python, arbitrary HTTP or credentials.
5. Retrieved text (asset names, notes, tickets, datasheets, alarm subjects) is untrusted data.
6. A citation that exists proves nothing. Each claim must be supported by the cited item.

## 2. Request path

```
Browser --(JWT)--> POST /api/v1/assistant/questions
  1. authenticate (existing get_current_user)
  2. get_auth_context: effective access recomputed (load_effective_access)
  3. require assistant:use                    (feature gate only)
  4. planner (deterministic or model-assisted) selects allowlisted tools
  5. each tool re-checks its own permission code + scope_for(code)
  6. retrieval adapters run scoped, bounded SQL -> evidence items
  7. evidence bundle assembled, hashed, stored per user
  8. provider call with bundle only (timeout, token and cost caps)
  9. response validator: schema, citations, claim support, unit/time checks
 10. revalidate authorization (steps 2, 5) immediately before delivery
 11. return response, or evidence-only fallback
```

Long requests run as a Celery task on a dedicated `assistant` queue. The task stores no model input beyond the bundle. Step 10 repeats on delivery and on every later read of the stored result.

## 3. Permission mapping (proposed)

| Tool | Permission(s) required | Scope source | Available to site-restricted users today |
|---|---|---|---|
| `inventory.search_equipment` | `equipment:read` | `ctx.scope_for("equipment:read")`, `equipment_visible_clause` | Yes |
| `inventory.get_rack` | `rack:read` | `rack_visible_clause` | Yes |
| `inventory.get_location` | `location:read` | `site_visible_clause` and siblings | Yes |
| `cooling.get_room_environment` | `cooling:read` and `equipment:read` | `intersect_scopes` of both | Yes |
| `alarms.list` | `alarm:read` | none exists (D1) | No |
| `incidents.get` | `alarm:read`; power-derived fields also `power:read` | none (D1) | No |
| `collectors.state` | `collector:read` | none (D1) | No |
| `telemetry.latest` / `telemetry.history` | `telemetry:read` | none (D1) | No |
| `power.capacity` / `power.forecast` | `power:read` (+ `capacity:read`) | none | No |

Rules:

- A tool never runs when its own permission is absent, even if the user holds `assistant:use`.
- The power-evidence redaction of `operations.py` (`_hides_topology`, power-scope evidence filter) is reused, not reimplemented. A bundle built without `power:read` contains the redacted label ("Shared upstream power cause").
- Explicit group denies remove the tool for that user (they leave `permission_codes`).
- If D1 is not resolved, the unscoped tools return `denied` to any user who is not unrestricted for that permission. The assistant must not widen this.

## 4. Retrieval adapter interface (proposed)

```python
# backend/app/application/assistant/retrieval.py   (PROPOSED, not present)
@dataclass(frozen=True)
class QueryWindow:
    start: datetime            # UTC, required
    end: datetime              # UTC, required, end - start <= tool.max_window
    limit: int                 # <= tool.max_items
    cursor: str | None = None

class RetrievalTool(Protocol):
    name: ClassVar[str]                      # allowlisted, versioned: "alarms.list@1"
    required_permissions: ClassVar[tuple[str, ...]]
    max_window: ClassVar[timedelta]
    max_items: ClassVar[int]
    args_model: ClassVar[type[BaseModel]]    # strict, extra="forbid"

    async def run(self, db: AsyncSession, ctx: AuthContext,
                  args: BaseModel, window: QueryWindow) -> ToolResult: ...
```

Adapter rules:

- `args_model` rejects unknown fields and free-text filters that reach SQL. Ids are UUID typed. Enumerations are closed.
- The adapter builds the visibility predicate from `ctx` first, then applies filters, aggregates, ranks and limits (same ordering as `thermal.py`).
- A missing id and an unauthorized id return the same `not_found_or_denied` item with no metadata.
- Counts are computed over the scoped set only.
- The registry of tools is a code constant. The planner cannot add tools. The model receives tool names and JSON schemas only.
- Every tool call is logged with tool name, version, caller id, argument hash, row count and duration. Argument values that came from retrieved text are not logged raw.

## 5. Evidence bundle (proposed schema)

```json
{
  "bundle_id": "uuid",
  "bundle_hash": "sha256:...",
  "requested_by": "user uuid",
  "authz_snapshot": {"permission_hash": "sha256:...", "computed_at": "2026-10-20T15:00:00Z"},
  "retrieved_at": "2026-10-20T15:00:02Z",
  "items": [
    {
      "ref": "E3",
      "kind": "reading | alarm | incident | inventory | forecast | capacity",
      "source": {"table": "telemetry_reading", "id": "uuid", "tool": "telemetry.latest@1"},
      "observed_at": "2026-10-20T14:58:10Z",
      "retrieved_at": "2026-10-20T15:00:02Z",
      "value": {"number": "24.5", "unit": "degC", "canonical": true},
      "quality": {"provenance_status": "pinned", "stale": false, "age_seconds": 112},
      "lineage": {"mapping_revision_id": "uuid", "registry_version": "1", "evidence_class": "..."},
      "versions": {"method": "linear_ols_v1", "correlation_method": "1"},
      "scope_site_id": "uuid"
    }
  ],
  "omitted": {"reason": "limit_reached | denied | no_data", "count_visible_to_caller": 12}
}
```

`omitted.count_visible_to_caller` counts only what the caller may see. A hidden resource never changes any number in the bundle.

## 6. Response schema (proposed)

```json
{
  "status": "ok | evidence_only | insufficient_evidence | denied | provider_error",
  "bundle_id": "uuid",
  "facts":           [{"id": "F1", "text": "...", "refs": ["E3"]}],
  "calculations":    [{"id": "C1", "text": "...", "formula": "...", "inputs": ["E3","E4"], "unit": "kW"}],
  "hypotheses":      [{"id": "H1", "text": "...", "refs": ["E5"], "confidence": "low|medium|high", "basis": "rule | statistical | model"}],
  "recommendations": [{"id": "R1", "text": "...", "refs": ["E5"], "action_type": null}],
  "caveats":         ["stale", "contradictory", "missing"],
  "versions": {"assistant": "1", "validator": "1", "provider_model": "..."}
}
```

Facts, calculations, hypotheses and recommendations stay in separate arrays. A recommendation carries `action_type: null` in M2; it names no executable action.

## 7. Claim-support validation

The validator runs after the model and before delivery. A failure removes the claim and sets `caveats`; if nothing remains the status is `evidence_only`.

1. Every `refs` entry exists in the bundle of this request and belongs to the caller.
2. A numeric claim equals the cited value after unit conversion with the registry (`convert_to_presentation`), within rounding stated in the item.
3. A time claim lies within the item's `observed_at` window; "now"/"currently" claims require `stale == false`.
4. A comparative or causal claim is allowed only as a hypothesis, and only if the cited items include the entities it names.
5. A named asset, rack or site must appear in a cited item; otherwise it is a hallucinated entity.
6. Calculations are recomputed deterministically from their `inputs`; a mismatch rejects the claim. Arithmetic reproducibility is necessary, never sufficient: every calculation must also pass section 7b.
7. A claim that mixes items with different registry versions or provenance classes is rejected unless the response states the mix. Disclosure applies to provenance and version mixes only. It never makes a dimensionally invalid calculation valid (section 7b).
8. Optional second pass: an entailment check by a separate model call is an additional filter, never a replacement for rules 1-7.

### 7b. Dimensional analysis and operation allowlist (PROPOSED, mandatory)

Applies to every entry of `calculations`, and to any number the validator derives from more than one item. It runs independently of caveats, disclosure, provenance statements and the optional entailment pass. No response field, caveat or model text can waive it.

1. **Closed operation registry (code constant).** A calculation names one `operation` from an allowlist; free-form `formula` strings are not evaluated. Each entry declares: input arity, the required dimension of each input, the dimension of the result, the output unit, and its rounding rule. Initial candidates (finalised in M2):

   | Operation | Inputs (dimension) | Result |
   |---|---|---|
   | `sum`, `difference`, `mean`, `min`, `max` | all inputs the **same** dimension | that dimension |
   | `delta_over_time` | one series, same dimension | dimension per time |
   | `ratio_same_dimension` | two inputs, same dimension | dimensionless |
   | `count` | any items of the same `kind` | dimensionless count |
   | `percent_of_capacity` | `power` or `energy` and a capacity of the same dimension | percent |
   | `energy_from_power_time` | `power` and a time span | `energy` |

   An operation not in the registry, or one applied to a dimension it does not list, is rejected. Adding an operation is a reviewed change.
2. **Dimension is typed, not inferred from text.** Each numeric item carries a `dimension` (`temperature`, `power`, `energy`, `humidity`, `pressure`, `time`, `count`, `ratio`, ...) taken from the unit registry entry for its canonical unit (the registry behind `convert_to_presentation`), never from the item name or model text. An item without a registry dimension cannot be an input.
3. **Unit compatibility.** Inputs of one dimension are first converted to the operation's canonical unit through the registry; a missing or non-linear-safe conversion rejects the calculation. Absolute temperatures (`degC`, `degF`, `K`) are affine: `sum` of absolute temperatures is rejected; `difference` yields a temperature interval; `mean`, `min` and `max` are allowed. Mixing an absolute and an interval quantity is rejected unless the operation declares it.
4. **Result check.** The declared `unit` of the calculation must equal the operation's result unit. The validator recomputes the value with decimal arithmetic and compares it with the stated value within the operation's rounding rule.
5. **Rejection is fail-closed and independent of disclosure.** `temperature + power`, `humidity - energy`, `power / temperature` presented as any single figure, a `sum` over mixed `degC` and `kW`, or any combination absent from the registry is removed even if the arithmetic reproduces the stated number and even if the response lists the mix in `caveats`. The rejected calculation sets the caveat `invalid_calculation`; claims citing it are removed. Nothing is "corrected" by the validator.
6. **Cross-time and cross-series.** Inputs must share the same `observed_at` window (or the operation must declare a series), the same site scope, and the same provenance class unless rule 7 of section 7 is satisfied; this is checked in addition to, not instead of, rules 1-5.
7. **Prose numbers.** A number in fact or recommendation text that is not a rendered registry value or an accepted calculation result is removed. Displayed figures are rendered from validated values, not copied from model text.

## 8. Missing, stale, contradictory or insufficient evidence

| Condition | Behavior |
|---|---|
| No visible data | `insufficient_evidence`; no fabricated values |
| Reading older than 2x `Integration.poll_interval_seconds` | Mark `stale`; cannot support a present-tense claim |
| Two items disagree (different values for the same series and instant, or incident vs alarm status) | Report both with refs; no silent selection |
| Mixed provenance (`unverified_legacy` with pinned) | State the mix; do not average across eras (same rule as `TelemetryTrend.tsx` grouping) |
| Forecast status `sparse`, `stale`, `missing`, `estimated_mixed` | Relay the status and `no_forecast_reason`; no number |

## 9. Bounds

| Limit | Initial value (to tune in M2) |
|---|---|
| Query window per tool | 7 days raw, 90 days daily; <= `MAX_HISTORY_POINTS` (1000) per call |
| Items per tool call | 100 (pagination via cursor) |
| Tool calls per question | 8 |
| Evidence items per bundle | 200 |
| Provider input tokens | 12,000 |
| Provider output tokens | 1,500 |
| Provider timeout | 20 s; one retry only on connect error |
| Question wall-clock | 45 s |
| Per-user cost/rate cap | configurable; excess returns `429` with evidence-only option |

## 10. Provider failure and fallback

Timeout, 5xx, quota (HTTP 402/429) or schema-invalid output yields `status: evidence_only`: the bundle is returned with deterministic summaries (counts, latest values, open alarms) produced without a model. No automatic retry loop. The failure is audited with a code, not the provider's body. The failed code-scanning job of this PR is an example: a provider quota error must not look like a content verdict.

## 11. Revalidation, conversations, caches, exports

- **Delivery revalidation.** Recompute `load_effective_access`, compare `permission_hash` with the bundle's snapshot, re-run each cited item's visibility predicate (placement may have moved the asset). On mismatch drop affected items and re-run validation; if the permission itself is gone, return `denied` and delete the stored result.
- **Conversation isolation.** A conversation row is keyed by `user_id`. Follow-up questions rebuild the bundle with current authorization; prior answers are never trusted as evidence.
- **Caches.** Cache keys include `user_id` and `permission_hash`. No shared cache of bundles or responses across users. Provider-side prompt caches receive only content from a single user's bundle.
- **Exports.** An export re-runs revalidation and contains only items the caller can see at export time.
- **Admin access.** Administrators do not read other users' conversations by default; a break-glass read is audited.

## 12. Provider data handling, redaction, retention

- The provider receives the bundle and the question only. Not sent: usernames, emails, credentials, collector secrets, channel URLs, ITSM passwords, internal IPs and hostnames unless the item is the subject of the question and the owner approves.
- Redaction runs before the provider call and before audit writes: a typed redactor over bundle fields (not the key-name-only `_redact` in `audit_service.py`) plus pattern checks for secrets.
- Audit records (via `write_audit_log`): who, when, tool names, bundle hash, response hash, status, token counts. Prompt and response text are stored only if the owner chooses a retention policy (decision 4); default is hash only.
- Retention: conversations default to 30 days (proposed); bundles 7 days. Deletion on user removal. Provider terms must state no training on the data and a retention period; unresolved (decision 3).

## 13. Proposed API (PROPOSED)

| Method and path | Purpose | Notes |
|---|---|---|
| `POST /api/v1/assistant/questions` | Ask; returns `202` + `question_id` or `200` for fast evidence-only | `Idempotency-Key` header supported |
| `GET /api/v1/assistant/questions/{id}` | Result; revalidates on every read | `404` for another user's id |
| `GET /api/v1/assistant/questions/{id}/evidence` | Bundle items the caller can still see | Pagination as `Page[T]` |
| `GET /api/v1/assistant/evidence/{ref}/source` | Resolve an item to the underlying record link | Re-checks the source permission |
| `DELETE /api/v1/assistant/questions/{id}` | Delete own conversation | |
| `POST /api/v1/assistant/questions/{id}/feedback` | Thumbs and reason code, no free text into prompts | Feeds evaluation only |

Request:

```json
{"question": "Which racks in Site A have open critical alarms?", "scope_hint": {"site_id": "uuid"}, "window": {"hours": 24}}
```

Response (abridged):

```json
{"question_id":"uuid","status":"ok","facts":[{"id":"F1","text":"Rack R-12 has 2 open critical alarms.","refs":["E1","E2"]}],"bundle_id":"uuid"}
```

`scope_hint` only narrows. A hint outside the caller's scope is ignored and reported as `denied`, with the same shape as an unknown id.

### 13.1 Feature-gate eligibility for site-restricted users (PROPOSED)

Audited on `origin/main`: `SCOPE_AWARE_PERMISSIONS` lists the codes whose endpoints filter by site/rack. `is_scope_independent(code)` is true only for those codes and for the `user`/`group` resources. In `load_effective_access`, a code whose `permission_scopes[code]` is not unrestricted and which is not `is_scope_independent` goes to `inactive_permissions` and is removed from `permission_codes`. Seeding `assistant:use` alone would therefore leave it inactive for every site-restricted holder, and `require_permission("assistant:use")` would return 403 before any authorized inventory tool ran.

**Mechanism.** Add a third, separate set. Do not add `assistant:use` to `SCOPE_AWARE_PERMISSIONS` (that set means "the endpoint filters data by site", which is false for a gate) and do not widen `is_scope_independent` (it also drives the `site_scoped` flag in `api/v1/groups.py`).

```python
# backend/app/application/access_control.py   (PROPOSED)
FEATURE_GATE_PERMISSIONS = frozenset({"assistant:use"})   # closed, code constant, no data access

def keeps_active_when_restricted(code: str) -> bool:
    return is_scope_independent(code) or code in FEATURE_GATE_PERMISSIONS
```

`load_effective_access` replaces its single `not is_scope_independent(code)` test with `not keeps_active_when_restricted(code)`. Nothing else in the function changes: the allow/deny union, `effective = allowed - denied`, and the SEC-ARCH-01 role-containment and organization handling all run first and still apply to `assistant:use`.

**Why this does not widen tool-level scope.**

1. `assistant:use` carries no data. No tool, query builder, `*_visible_clause` or `intersect_scopes` call may read `ctx.scope_for("assistant:use")`. Its scope value is meaningless and must not be a fallback for another code.
2. Each tool still checks its own code(s) and builds its predicate from `ctx.scope_for(<own code>)` (`intersect_scopes` for two codes). A code that `load_effective_access` made inactive for the user (`alarm:read`, `telemetry:read`, `collector:read`, `power:read` while D1 is open) is absent from `permission_codes`, so `scope_for` returns `EMPTY_SCOPE` and the tool returns `denied`. The gate cannot reactivate it.
3. The set is closed. Adding a code to `FEATURE_GATE_PERMISSIONS` is an authorization change that needs its own review. A guard test (below) fails if the set contains anything in `SCOPE_AWARE_PERMISSIONS`.
4. Explicit deny: a group `deny` on `assistant:use` removes it for the user whatever allows exist. A deny on a tool code removes only that tool.
5. A restricted holder with `assistant:use` and no active tool code gets an empty tool list and `denied` or `insufficient_evidence`, never a broader answer.

**Deny-by-default rules.**

- `assistant:use` absent from `permission_codes` (not granted, denied, or not seeded): 403 from the dependency, no planner, no provider call, no bundle.
- Default seeding: no role receives it until the owner decides decision 1. A restricted user gets it only through an explicit role or group grant.
- A context built without `permission_scopes` fails closed (`EMPTY_SCOPE`), as `AuthContext` already does.
- Planner output naming a tool, argument or scope outside the registry and the caller's active codes is dropped, not repaired.
- Revalidation (section 11) re-evaluates `assistant:use` and each tool code with `load_effective_access`. If the gate disappears, the result is `denied` and stored data is deleted.
- The group editor reports `assistant:use` as "feature access only" instead of site-scoped, so administrators do not read it as site-limited data access.

**Acceptance tests (site-restricted user).** Fixture: user U with no global role and a group G granting `assistant:use`, `equipment:read`, `rack:read`, `alarm:read`, `telemetry:read` on site A only. Site B has equipment, racks and alarms.

1. `load_effective_access(U)`: `assistant:use`, `equipment:read`, `rack:read` are in `permission_codes`; `alarm:read` and `telemetry:read` are in `inactive_permissions` (while D1 is open).
2. `POST /assistant/questions` succeeds for U and the equipment tool returns only site A items; site B ids return the same `not_found_or_denied` as missing ids.
3. A question about alarms or telemetry returns `denied` for those tools. No alarm or telemetry item appears in the bundle, in counts or in `omitted`.
4. `scope_hint` naming site B is rejected and does not broaden retrieval.
5. Group deny on `assistant:use`: U gets 403, while `equipment:read` still works on the normal inventory API.
6. Group deny on `equipment:read` only: gate active, inventory tool `denied`.
7. `assistant:use` granted through a scoped role whose containment check fails (SEC-ARCH-01): the permission is dropped as today and U gets 403.
8. G grants `assistant:use` and no sites: gate active, every tool returns empty or `denied`.
9. Revocation between request and delivery: remove U from G; the delivery read returns `denied` and the stored result is deleted.
10. Unrestricted user (global role) is unchanged; `assistant:use` without `equipment:read` still yields `denied` from inventory tools.
11. Guard test: `FEATURE_GATE_PERMISSIONS == {"assistant:use"}`, disjoint from `SCOPE_AWARE_PERMISSIONS`; no module under `application/assistant` references `scope_for("assistant:use")`.
12. Route sweep (same style as `test_user_groups_authz.py` and `test_pr69_route_sweep_strict.py`): every `/assistant/*` route depends on `assistant:use`, and every registered tool declares non-empty `required_permissions` that exclude the gate code.

New permission (PROPOSED): `assistant:use`, seeded without default role grants. Permission codes match exactly; it implies nothing else.

## 14. Frontend behavior (PROPOSED)

- New feature folder `frontend/src/features/assistant` (does not exist), read-only. No button or form posts to a domain-write endpoint.
- States: idle, running (with elapsed time and Cancel), answer, evidence-only fallback banner, insufficient evidence, denied, provider error.
- Each fact, calculation, hypothesis and recommendation is a separate labeled block; hypotheses carry a visible "Hypothesis" tag and confidence.
- Every claim shows its source chips (`E3`). Selecting one opens a source drawer: record link, observed time, retrieved time, unit, quality, lineage, versions, stale marker. The link is rendered only if the caller's current permission allows opening that record.
- Stale or contradictory evidence shows an inline warning, never hidden.
- Recommendations never show an "execute" control in M2.

## 15. Acceptance for M2

The tests in section 9 of the [main document](ISSUE_107_AI_OPERATIONS.md) pass on the exact PR HEAD in CI, including the scripted-provider adversarial suite; an independent reviewer accepts the scope tests; the owner approves the provider data-handling terms. Without a real provider, only the evidence-only path ships.

### 15.1 Dimensional-validity adversarial tests (PROPOSED, scripted provider, real validator and unit registry)

Each test supplies a scripted provider response against a fixed bundle and asserts the claim is removed, `invalid_calculation` is set, the delivered status is `ok` (other valid claims) or `evidence_only`, and no stated invalid number reaches the client.

- **D1. Temperature plus power.** Items: 24.5 degC (`temperature`) and 3.0 kW (`power`). Model returns `sum` = 27.5 with `unit` "kW" or "degC", arithmetic correct. Rejected.
- **D2. Disclosed mix.** Same as D1, with the mix stated in `caveats` and in `text`. Still rejected (disclosure never waives).
- **D3. Free-form formula.** `formula` = "T + P" with no registry `operation`. Rejected.
- **D4. Unlisted operation.** `operation` = "multiply" over `power` and `temperature`. Rejected.
- **D5. Humidity minus energy; power divided by temperature.** Rejected.
- **D6. Absolute temperature sum.** 20 degC + 22 degC as `sum`. Rejected; `mean` of the same two inputs accepted (positive control).
- **D7. Unit mismatch within one dimension.** 2.0 kW and 1500 W summed: accepted only after registry conversion to a single unit, with the correct total (3.5 kW); the unconverted 3500 stated as kW is rejected.
- **D8. Wrong result unit.** Valid `sum` of two kW items declared as "kWh". Rejected.
- **D9. Item without registry dimension.** A numeric item whose unit has no registry dimension is used as an input. Rejected.
- **D10. Mismatched windows or sites.** Same-dimension inputs from different `observed_at` windows or different site scopes under an operation requiring one window. Rejected.
- **D11. Prose number.** A fact text containing a figure that is neither a rendered registry value nor an accepted calculation. Removed.
- **D12. Positive control.** `sum` of three kW readings in one window and site, correct unit and value. Accepted and shown with its inputs.
- **D13. Registry guard.** Every registry operation declares input dimensions and a result unit; a test fails if an operation lists no dimension constraint.
