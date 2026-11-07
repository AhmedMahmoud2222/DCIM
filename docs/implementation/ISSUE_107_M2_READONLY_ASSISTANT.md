# Issue #107 M2: read-only assistant architecture and implementation contract

**Document version:** 0.1.1 (2026-10-20; section 13.1 added 2026-11-07). **Everything below is PROPOSED. Nothing is implemented.** Existing-behavior claims cite `main@92be692`; see the [main document](ISSUE_107_AI_OPERATIONS.md).

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
  "calculations":    [{"id": "C1", "text": "...", "operation": "sum", "inputs": ["E3","E4"], "unit": "kW"}],
  "hypotheses":      [{"id": "H1", "text": "...", "refs": ["E5"], "confidence": "low|medium|high", "basis": "rule | statistical | model"}],
  "recommendations": [{"id": "R1", "text": "...", "refs": ["E5"], "action_type": null}],
  "caveats":         ["stale", "contradictory", "missing"],
  "versions": {"assistant": "1", "validator": "1", "provider_model": "..."}
}
```

A calculation names an `operation` from the allowlist of section 7b and its `inputs`; it carries no free-form formula string. The validator derives the result unit and dimension itself; the model's `unit` is checked against that, never trusted. Facts, calculations, hypotheses and recommendations stay in separate arrays. A recommendation carries `action_type: null` in M2; it names no executable action.

## 7. Claim-support validation

The validator runs after the model and before delivery. A failure removes the claim and sets `caveats`; if nothing remains the status is `evidence_only`.

1. Every `refs` entry exists in the bundle of this request and belongs to the caller.
2. A numeric claim equals the cited value after unit conversion with the registry (`convert_to_presentation`), within rounding stated in the item.
3. A time claim lies within the item's `observed_at` window; "now"/"currently" claims require `stale == false`.
4. A comparative or causal claim is allowed only as a hypothesis, and only if the cited items include the entities it names.
5. A named asset, rack or site must appear in a cited item; otherwise it is a hallucinated entity.
6. Calculations must first pass dimensional validation (section 7b), then are recomputed deterministically from their `inputs`; either failure rejects the claim. Arithmetic reproducibility is necessary, never sufficient.
7. A claim that mixes items with different registry versions or provenance classes is rejected unless the response states the mix. Disclosure applies to those mixes only. It never waives any step of section 7b: a dimensionally invalid or non-allowlisted calculation is rejected whether or not the response mentions it.
8. Optional second pass: an entailment check by a separate model call is an additional filter, never a replacement for rules 1-7.

## 7b. Dimensional validation of calculations (PROPOSED)

Every item with a numeric `value` carries a `dimension` assigned by the unit registry from its canonical unit (for example `degC` is temperature, `kW` power, `kWh` energy, `%` ratio). The model cannot set or override it. A calculation is accepted only if all steps pass, in order; the first failure rejects it and sets the caveat `calculation_rejected`.

1. **Allowlisted operation.** `operation` is one of a closed code constant: `sum`, `difference`, `mean`, `min`, `max`, `count`, `ratio`, `percent_of`, `rate_of_change`. Anything else, including a free-form formula, is rejected at schema validation.
2. **Dimension rule per operation.** `sum`, `difference`, `mean`, `min`, `max`: all inputs share one dimension. `ratio`, `percent_of`: inputs share one dimension and the result is dimensionless. `rate_of_change`: one dimension per time. `count`: any inputs, result is a count. No operation multiplies or divides across different dimensions in M2; derived quantities such as power x time -> energy are rejected until the registry gains a reviewed rule.
3. **Unit conversion before arithmetic.** Inputs of one dimension are converted to a single unit via the registry (`convert_to_presentation`). Affine units are handled correctly: `sum` of absolute temperatures is rejected; `difference` of two absolute temperatures yields a temperature interval, not a temperature.
4. **Result unit check.** The validator computes the result dimension and unit from steps 2-3 and compares it with the claimed `unit`; a mismatch is rejected.
5. **Scope compatibility.** Inputs of a `sum`, `mean` or `ratio` must describe the same kind of subject (a rack's capacity and a site's capacity are not summed as peers).
6. **Arithmetic recomputation** (section 7 rule 6) runs last.

Disclosure, caveats, hypothesis labelling or explanatory text cannot move a calculation past steps 1-5. A claim that cannot be expressed as an allowlisted operation is not a calculation: it is removed, and the underlying items are shown instead.

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

### 15.2 Dimensional-validity tests (PROPOSED)

Scripted provider, real unit registry. Fixture items: `T1` = 24.5 degC, `T2` = 20 degC, `P1` = 24.5 kW, `P2` = 10 kW.

1. **Temperature + power, numerically reproducible.** Model returns `sum` of `T1`,`P1` = 49.0 `kW`. Recomputation would match 49.0; the claim is still rejected at step 2, `calculation_rejected` is set, and 49.0 appears nowhere in the delivered response.
2. **Same output with disclosure.** Identical, plus text stating that different quantities were combined. Still rejected (guards the section 7 rule 7 carve-out).
3. **Mutation-sensitive.** The validator is run in CI with each mutant: (a) skip step 2, (b) skip step 3 conversion, (c) accept an unlisted operation, (d) treat disclosure as a waiver, (e) trust the model's `unit`, (f) run recomputation before dimension checks, (g) skip step 5. Every mutant must fail at least one test in this list; a surviving mutant fails the section.
4. **Wrong result unit, valid dimensions.** `sum` of `P1`,`P2` claimed as `kWh` is rejected at step 4; claimed as 34.5 `kW` it is accepted (positive control).
5. **Unit conversion.** `P2` stored as 10000 W with `P1` in kW: only 34.5 kW is accepted; an unconverted 10024.5 is rejected.
6. **Absolute temperatures.** `sum` of `T1`,`T2` is rejected; `difference` = 4.5 is accepted only as a temperature interval.
7. **Cross-dimension ratio.** `ratio` of `P1` to `T1` is rejected; `ratio` of `P1` to `P2` is accepted as dimensionless.
8. **Cross-dimension product.** Power x time -> energy is rejected in M2 even though `P1` x 1 h = 24.5 kWh is arithmetically right.
9. **Free-form formula or unlisted operation** (`"p1 + t1 * 2"`) is rejected at schema validation.
10. **Injected text.** An asset name or alarm subject instructs adding temperature to power; the model complies; rejected as in test 1.
11. **Provenance disclosure still works.** Same-dimension inputs with different provenance classes are accepted when the mix is stated and rejected when it is not.
12. **No residue.** After test 1 no total, ranking or summary built on the rejected calculation is delivered, and `status` is `evidence_only` if no claim survives.
