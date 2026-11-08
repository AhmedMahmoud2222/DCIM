# Issue #107 M2: read-only assistant architecture and implementation contract

**Document version:** 0.1.3 (freshness by evidence kind, 2026-11-08; previously 0.1.2: 2026-10-20; sections 7a, 7b, 11 derived evidence, 11a, 13.1 and tests 15.1-15.4 reconciled 2026-11-07). **Everything below is PROPOSED. Nothing is implemented.** Existing-behavior claims cite `main@92be692`; see the [main document](ISSUE_107_AI_OPERATIONS.md).

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
      "freshness": {"class": "cadence | live_read", "source_version": "7"},
      "lineage": {"mapping_revision_id": "uuid", "registry_version": "1", "evidence_class": "...",
                  "derived": false, "contributors": [], "placement_revision": "sha256:..."},
      "versions": {"method": "linear_ols_v1", "correlation_method": "1"},
      "scope_site_id": "uuid",
      "attributes": {"collector.state": "online", "alarm.status": null, "equipment.status": null}
    }
  ],
  "omitted": {"reason": "limit_reached | denied | no_data", "count_visible_to_caller": 12}
}
```

`omitted.count_visible_to_caller` counts only what the caller may see. A hidden resource never changes any number in the bundle.

`attributes` (PROPOSED) holds categorical values copied by the retrieval adapter from the source record into a closed, versioned vocabulary (`predicate_registry`, section 7a). It is written by deterministic code, never by the model. Only registered keys are allowed; an item has a key only if the source record has that field.

`lineage.derived`, `contributors` and `placement_revision` (PROPOSED) support section 11: a derived item (aggregate, total, count, ranking, summary) lists every contributing record id and the `tool@version` plus argument hash that produced it.

`quality.stale` and `age_seconds` are retrieval-time values only; freshness is recomputed at every use (section 11a).

## 6. Response schema (proposed)

```json
{
  "status": "ok | evidence_only | insufficient_evidence | denied | provider_error",
  "bundle_id": "uuid",
  "facts":           [{"id": "F1", "text": "...", "refs": ["E3"],
                       "predicates": [{"subject_ref": "E3", "predicate": "collector.state", "op": "eq", "value": "online"}]}],
  "calculations":    [{"id": "C1", "text": "...", "operation": "sum", "inputs": ["E3","E4"], "unit": "kW"}],
  "hypotheses":      [{"id": "H1", "text": "...", "refs": ["E5"], "confidence": "low|medium|high", "basis": "rule | statistical | model"}],
  "recommendations": [{"id": "R1", "text": "...", "refs": ["E5"], "action_type": null}],
  "caveats":         ["stale", "contradictory", "missing"],
  "versions": {"assistant": "1", "validator": "1", "provider_model": "..."}
}
```

A calculation names an `operation` from the allowlist of section 7b and its `inputs`; it carries no free-form formula string. The validator derives the result unit and dimension itself; the model's `unit` is checked against that, never trusted. Facts, calculations, hypotheses and recommendations stay in separate arrays. A recommendation carries `action_type: null` in M2; it names no executable action.

Every fact must carry at least one typed `predicates` entry (`subject_ref`, `predicate`, `op`, `value`). `text` is presentation only and is never evidence; see section 7a.

## 7. Claim-support validation

The validator runs after the model and before delivery. A failure removes the claim and sets `caveats`; if nothing remains the status is `evidence_only`.

1. Every `refs` entry exists in the bundle of this request and belongs to the caller.
2. A numeric claim equals the cited value after unit conversion with the registry (`convert_to_presentation`), within rounding stated in the item.
3. A time claim lies within the item's `observed_at` window. "now"/"currently" claims require freshness computed at the current evaluation time (section 11a); the stored `quality.stale` is never consulted for this rule.
4. A comparative or causal claim is allowed only as a hypothesis, and only if the cited items include the entities it names.
5. A named asset, rack or site must appear in a cited item; otherwise it is a hallucinated entity.
6. Calculations must first pass dimensional validation (section 7b), then are recomputed deterministically from their `inputs`; either failure rejects the claim. Arithmetic reproducibility is necessary, never sufficient.
7. A claim that mixes items with different registry versions or provenance classes is rejected unless the response states the mix. Disclosure applies to those mixes only. It never waives any step of section 7b: a dimensionally invalid or non-allowlisted calculation is rejected whether or not the response mentions it.
8. Categorical and relational claims are checked deterministically against typed predicates (section 7a). Citation existence alone never satisfies this rule.
9. Optional second pass: an entailment check by a separate model call is an additional filter, never a replacement for rules 1-8, and can only remove claims.

## 7a. Typed categorical predicates (PROPOSED)

The validator, not the model, decides whether a categorical claim is supported.

**Registry.** A code constant `predicate_registry` lists each allowed predicate with: the evidence `kind` it applies to, a closed value enumeration taken from the source model, allowed ops (`eq`, `ne`, `in`), and the staleness rule. Initial entries:

| Predicate | Kind | Values | Staleness |
|---|---|---|---|
| `collector.state` | collector item | the closed set of collector states in the source model (exact set copied from code in M2; not verified here) | present-tense claims need freshness at evaluation time (section 11a) |
| `alarm.status` | alarm item | the alarm lifecycle enumeration | same |
| `alarm.severity` | alarm item | the severity enumeration | none |
| `incident.status` | incident item | the incident lifecycle enumeration | same |
| `equipment.status`, `equipment.type`, `equipment.rack_id`, `equipment.site_id` | inventory item | source enumeration or UUID | present-tense claims need the `live_read` check (section 11a.1b); no polling interval is used |
| `relation.located_in`, `relation.raised_on` | pair of items | a subject item and an object item | `live_read` check on both items (section 11a.1b) |

**Check.** For each predicate in a fact:

1. `subject_ref` must be in this bundle, and its `kind` must match the predicate's kind.
2. The predicate key must exist in the subject's `attributes`, and `value` must be in the registry enumeration. Unknown key or value rejects the claim.
3. `op` is evaluated against `attributes[predicate]`. `eq` passes only on exact equality; a missing attribute never passes. A `ne` claim passes only if the attribute exists and differs.
4. A relationship predicate passes only if both items are in the bundle and the object id is recorded in the subject's source fields (for example the alarm's equipment id equals the cited equipment item's id). Co-citation of two items is not a relationship.
5. A present-tense claim also applies the staleness column and rule 3 above, with freshness evaluated per section 11a.
6. Where the bundle holds several items for the same subject (section 8, contradictory evidence), a predicate is supported only if no cited item for that subject contradicts it; otherwise the claim is removed and `contradictory` is set.

**Free text fails closed.** A fact with no predicates, or whose `text` states a state, status, severity, type, location or relationship that the validator cannot map to a registered predicate, is removed. Displayed fact text is rendered from the validated predicates by a template (the same template that renders the historical wording of section 11a), so it cannot carry an unvalidated categorical statement. Model prose is kept only in hypotheses (rule 4) and recommendations, which are labeled and may not assert current state. If synthesis is off or all facts are removed, the deterministic evidence-only summary is used.

## 7b. Dimensional validation of calculations (PROPOSED)

Every item with a numeric `value` carries a `dimension` assigned by the unit registry from its canonical unit (for example `degC` is temperature, `kW` power, `kWh` energy, `%` ratio). The model cannot set or override it. A calculation is accepted only if all steps pass, in order; the first failure rejects it and sets the caveat `calculation_rejected`.

1. **Allowlisted operation.** `operation` is one of a closed code constant: `sum`, `difference`, `mean`, `min`, `max`, `count`, `ratio`, `percent_of`, `rate_of_change`. Anything else, including a free-form formula, is rejected at schema validation.
2. **Dimension rule per operation.** `sum`, `difference`, `mean`, `min`, `max`: all inputs share one dimension. `ratio`, `percent_of`: inputs share one dimension, none of them is an affine absolute quantity (step 3a), and the result is dimensionless. `rate_of_change`: one dimension per time. `count`: any inputs, result is a count. No operation multiplies or divides across different dimensions in M2; derived quantities such as power x time -> energy are rejected until the registry gains a reviewed rule.
3. **Unit conversion before arithmetic.** Inputs of one dimension are converted to a single unit via the registry (`convert_to_presentation`). Affine units are handled correctly: `sum` of absolute temperatures is rejected; `difference` of two absolute temperatures yields a temperature interval, not a temperature.
3a. **Affine absolute quantities are excluded from ratios.** The registry marks each unit with a `scale`: `ratio` (true zero: kW, W, kWh, %) or `affine` (arbitrary zero: `degC`, `degF`, and any absolute temperature). A quotient of two absolute temperatures depends on the unit chosen (20 degC / 10 degC = 2, but 293.15 K / 283.15 K is about 1.035), so it has no physical meaning. In M2 `ratio` and `percent_of` are rejected when any input is an absolute temperature, whatever its unit, including a value stored in kelvin; there is no silent conversion to kelvin and no exception. A temperature interval (the result of `difference`, dimension `temperature_interval`, unit `delta_degC` or `delta_K`) is a ratio-scale quantity: `ratio` and `percent_of` of two intervals are accepted and are invariant under degC/K. An interval and an absolute temperature are never combined in one operation. `mean`, `min`, `max` and `rate_of_change` of absolute temperatures stay allowed because their results transform consistently with the unit (the last yields an interval per time). A unit with no registry `scale` is rejected (fail closed).
4. **Result unit check.** The validator computes the result dimension and unit from steps 2-3 and compares it with the claimed `unit`; a mismatch is rejected.
5. **Scope compatibility.** Inputs of a `sum`, `mean` or `ratio` must describe the same kind of subject (a rack's capacity and a site's capacity are not summed as peers).
6. **Arithmetic recomputation** (section 7 rule 6) runs last.

Disclosure, caveats, hypothesis labelling or explanatory text cannot move a calculation past steps 1-5 (including 3a). A claim that cannot be expressed as an allowlisted operation is not a calculation: it is removed, and the underlying items are shown instead.

## 8. Missing, stale, contradictory or insufficient evidence

| Condition | Behavior |
|---|---|
| No visible data | `insufficient_evidence`; no fabricated values |
| `cadence` item (telemetry, collector state) older than 2x `Integration.poll_interval_seconds`, measured at the current evaluation time (section 11a.1a) | Mark `stale`; cannot support a present-tense claim |
| `live_read` item (inventory, alarm and incident status; no integration) whose scoped re-read at evaluation time fails, shows a changed `source_version`, or is no longer visible (section 11a.1b) | Not current: re-validate against the re-read values, render historical, or remove; never judged by a polling interval |
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

- **Delivery revalidation.** Recompute `load_effective_access`, compare `permission_hash` with the bundle's snapshot, re-run each cited item's visibility predicate (placement may have moved the asset). If the permission itself is gone, return `denied` and delete the stored result. Item-level dropping applies only to **primary** items (one record, one visibility predicate); derived evidence follows the next rule. Freshness is a separate, independent check (section 11a).
- **Derived evidence is never filtered, only rebuilt or invalidated.** An aggregate, total, count, ranking, average, forecast input or any summary built over several records ("derived item") must record its contributors in `lineage.contributors` (record ids and the `tool@version` plus argument hash that produced it). Dropping a derived item's inaccessible contributors, or checking only the visibility of the entity it is attached to (for example the room), would still leak the hidden contributors through the total or ranking and is forbidden. On delivery, on every later read, on export, on asynchronous completion and before any follow-up reuses a bundle, the system must:
  1. Detect change: the `permission_hash` differs, or any contributor's current placement/scope fails the caller's current predicates, or any contributor no longer exists or moved site/room/rack since `retrieved_at` (compare a per-item `placement_revision`, or re-evaluate the predicate).
  2. On any change, **discard every derived item whose contributor set is not fully visible and unchanged**, and rebuild it by re-running the stored tool plan (tool name, version, validated arguments) through the normal adapter path, which applies the current visibility predicate before aggregation and ranking (principle 3). Rebuilt values replace the old ones; the old values are never shown, cached, exported or sent to the provider.
  3. Claims, calculations, hypotheses and recommendations that cite a rebuilt or discarded item are re-validated against the new values (section 7, including 7a and 7b) or removed. Model-written text is not patched; if synthesis is needed again, it is regenerated from the rebuilt bundle or the response degrades to `evidence_only`.
  4. **Fail closed:** if the rebuild fails, exceeds the bounds of section 9, or the plan cannot be replayed, the derived item and everything that cites it are removed, `caveats` gets `missing`, and the status is `evidence_only` or `insufficient_evidence`. If the revalidation itself errors, return `denied`/an error with no content rather than the stored result.
  5. Rebuilt output must be indistinguishable from output for a caller who never had the old access: no count, rank position, "N more omitted" or gap in totals reveals revoked or moved items (consistent with section 5 `omitted.count_visible_to_caller`).
  6. The stored bundle and response are overwritten or deleted after the rebuild; the audit record keeps hashes and a reason code (`authz_changed`, `placement_changed`), not the discarded values.
- **Conversation isolation.** A conversation row is keyed by `user_id`. Follow-up questions rebuild the bundle with current authorization; prior answers are never trusted as evidence.
- **Caches.** Cache keys include `user_id` and `permission_hash`. No shared cache of bundles or responses across users. Provider-side prompt caches receive only content from a single user's bundle.
- **Exports.** An export re-runs revalidation and contains only items the caller can see at export time.
- **Admin access.** Administrators do not read other users' conversations by default; a break-glass read is audited.

## 11a. Freshness is evaluated at use time (PROPOSED)

`quality.stale` and `age_seconds` in a stored bundle (section 5) describe the moment of retrieval only. They are informational and are never an input to claim validation or delivery.

1. **Evaluation time and freshness class.** Every validation pass takes an explicit `evaluated_at` from the server clock at that moment. Each item carries a `freshness_class` assigned by its retrieval adapter (a code constant per tool, never the model). Currency is decided by class:
   - **a. `cadence` (telemetry readings, collector heartbeat and state, forecasts built from readings).** Age is `evaluated_at - observed_at`; the item is stale when age exceeds 2x the source's current `Integration.poll_interval_seconds` (section 8), read at evaluation time. Only items with an integration may use this class.
   - **b. `live_read` (inventory: equipment, racks, locations, and relations between them; any record with no integration and no sampling cadence).** There is no polling interval and none is consulted. The item stores `source_version` (the row's integer `version` where it has one, otherwise a hash of the cited fields and `updated_at`) and its `placement_revision`. At `evaluated_at` the validator performs a scoped re-read of the record through the normal adapter path (current visibility predicate, principle 3) and compares. Unchanged and still visible: the item is current as of `evaluated_at` (`verified_at`), regardless of how old `retrieved_at` is. Changed: the claim is re-validated against the re-read values (7a), or rewritten historical as in 3 if the change makes the old value false. Not visible any more, deleted, moved out of scope, or not re-readable: handled by section 11 (denied or not_found_or_denied shape, no stale value shown). The re-read is bounded by section 9 and counted against its limits.
   - **c. Alarm and incident status** are `live_read` (state-changing rows), not `cadence`; their `observed_at` is informational.
   - A freshness class the registry does not know is stale (item 6).
2. **When it runs.** Before first delivery, on asynchronous completion (the Celery task re-evaluates after the provider returns, not at enqueue time), on every `GET` of a stored result, on every evidence listing, on export, and whenever a follow-up question reuses any part of a prior bundle or answer. No path may return a stored claim without a fresh evaluation.
3. **Present-tense claims.** A fact whose text asserts a current state ("now", "currently", present-tense state predicates) that cites an item now stale is handled in this order: (a) if the item was fresh at `retrieved_at` and the claim is supported at that time, rewrite it from the validated predicate template as explicitly historical ("As of <observed_at>: ..."), with the caveat `stale`; (b) otherwise remove the claim. Model text is never patched; the template renders it.
4. **Derived evidence.** An aggregate, ranking or calculation is stale if any contributor is stale; it is rebuilt through the adapter (a fresh read) or rendered historical as in 3. A fresh contributor never refreshes a stale one.
5. **Follow-ups.** A follow-up re-runs retrieval for any item it needs to support a present-tense claim; prior bundles may support historical statements only.
6. **Fail closed.** If `evaluated_at` cannot be obtained, the item is stale. For `cadence` items the same applies when the poll interval is unknown, `observed_at` is missing or in the future beyond clock tolerance. For `live_read` items it applies when the re-read fails, times out, or `source_version` is missing, and an absent polling interval is never a reason (it is not an input for that class). Any class-specific re-evaluation error counts as stale. If no claim survives, the status is `evidence_only` or `insufficient_evidence`. Cached responses are keyed additionally by the evaluation outcome and are never served past the earliest item staleness threshold.
7. **Interaction with 11.** Freshness changes do not alter `permission_hash`, so the authorization check alone never detects them; both checks run independently.

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

### 15.1 Derived-evidence revalidation tests (required)

Each runs against the real adapters and DB fixtures (not mocks of the visibility predicate), on delivery, on a later `GET`, on export, and on a follow-up question.

| # | Scenario | Setup | Required result |
|---|---|---|---|
| R1 | Moved asset, room aggregate | Room X summary (total kW, mean temperature) built from equipment A, B, C visible to the user. After retrieval, C moves to a room/site outside the user's scope. | Summary is rebuilt from A and B; totals, mean, count and ranking equal a fresh run; no value that includes C is delivered, exported or sent to the provider. A test asserts the old total is absent from the response, stored bundle, cache and audit payload. |
| R2 | Scope revocation, site | User loses the site grant covering B and C after the bundle is stored. | Derived items are rebuilt under the narrowed scope; if nothing remains, `insufficient_evidence`. No "3 of 5" or other count revealing the removed items. |
| R3 | Ranking | "Top 3 hottest racks": rank 1 moves out of scope. | Ranking recomputed; former rank 4 may now appear; the response never shows a gap, a renumbered-with-hole list or the removed item's value. |
| R4 | Anchor visible, contributor hidden | The room remains visible but one contributing device is no longer visible. | The derived item is not retained just because its anchor entity is visible; it is rebuilt without the device. |
| R5 | Rebuild failure | Force the replay to time out or exceed bounds. | Derived item and every claim citing it are removed; status `evidence_only` or `insufficient_evidence`; no stale value is shown. |
| R6 | Revalidation error | Make the authorization recompute raise. | No stored content is returned (fail closed). |
| R7 | Permission removed | `assistant:use` or the tool permission is revoked. | `denied`; stored result deleted; a later `GET` returns `denied`/`404`. |
| R8 | Moved into scope | An asset moves into the user's scope. | Not required to appear in old answers; a follow-up rebuilds from current authorization and may include it. |
| R9 | Model text with stale number | A scripted provider text states the old total after R1. | Claim fails validation against the rebuilt bundle (section 7 rule 2) and is removed. |
| R10 | Cache and export | Same user keeps a cached bundle across R1/R2; export is requested afterwards. | Cache key mismatch (`permission_hash`/`placement_revision`) forces a rebuild; the export contains only rebuilt items. |
| R11 | Negative control | No authorization or placement change. | Output is identical (same `bundle_hash`) and no rebuild is triggered. |
| R12 | Async completion | Scope is revoked while the Celery task is running. | The completion pass applies the same detection and rebuild; the stale total is never stored or delivered. |

### 15.4 Categorical-claim tests (PROPOSED)

Scripted provider, run in CI; each asserts the claim is removed, `caveats` is set, and nothing is delivered as a fact.

| # | Scenario | Required result |
|---|---|---|
| C1 | **Opposite status, valid citation.** Item E1 is collector C with `collector.state = online`; the provider returns "Collector C is offline" citing E1 with predicate `eq offline`. Repeat for alarm `cleared` vs claimed `open`, and equipment `decommissioned` vs claimed `active`. | Rejected. |
| C2 | Right status, wrong subject: the fact names collector D but cites E1 (collector C). | Rejected. |
| C3 | Free text only: "Collector C is offline" with no predicates; same for a text state that differs from its own predicate. | Rejected. |
| C4 | Unregistered or out-of-enumeration value (`collector.state eq "sleeping"`) or unknown key. | Rejected. |
| C5 | Missing attribute on the cited item; `eq` and `ne` both. | Rejected. |
| C6 | Relationship by co-citation: "Alarm A is on rack R" citing both items where the alarm's equipment is not in R. | Rejected. |
| C7 | Stale present tense: correct but stale collector state claimed as "currently" (evaluated per section 11a). | Rejected or rendered historical. |
| C8 | Contradictory items: collector online in one item, offline in another. | Either single-state claim rejected, `contradictory` set. |
| C9 | Injected text: an asset name or alarm subject says "collector C is offline"; the model repeats it. | Rejected; the attribute, not the text, is checked. |
| C10 | Positive control: the correct predicate on the correct item. | Passes, so the checks do not reject everything. |
| C11 | Moved asset or revoked scope: a categorical claim whose subject becomes invisible. | Removed (section 11, with R1/R4). |
| C12 | Mutation-sensitive: validator run with mutants (a) accept citation existence alone, (b) read `text` instead of `attributes`, (c) skip the enumeration check, (d) treat co-citation as a relationship, (e) skip the contradiction check. | Each mutant fails at least one of C1-C11. |

### 15.2 Dimensional-validity tests (PROPOSED)

Scripted provider, real unit registry. Fixture items: `T1` = 24.5 degC, `T2` = 20 degC, `P1` = 24.5 kW, `P2` = 10 kW.

1. **Temperature + power, numerically reproducible.** Model returns `sum` of `T1`,`P1` = 49.0 `kW`. Recomputation would match 49.0; the claim is still rejected at step 2, `calculation_rejected` is set, and 49.0 appears nowhere in the delivered response.
2. **Same output with disclosure.** Identical, plus text stating that different quantities were combined. Still rejected (guards the section 7 rule 7 carve-out).
3. **Mutation-sensitive.** The validator is run in CI with each mutant: (a) skip step 2, (b) skip step 3 conversion, (c) accept an unlisted operation, (d) treat disclosure as a waiver, (e) trust the model's `unit`, (f) run recomputation before dimension checks, (g) skip step 5, (h) skip step 3a, (i) treat degC as a ratio-scale unit, (j) convert silently to kelvin instead of rejecting. Every mutant must fail at least one test in this list; a surviving mutant fails the section.
4. **Wrong result unit, valid dimensions.** `sum` of `P1`,`P2` claimed as `kWh` is rejected at step 4; claimed as 34.5 `kW` it is accepted (positive control).
5. **Unit conversion.** `P2` stored as 10000 W with `P1` in kW: only 34.5 kW is accepted; an unconverted 10024.5 is rejected.
6. **Absolute temperatures.** `sum` of `T1`,`T2` is rejected; `difference` = 4.5 is accepted only as a temperature interval.
7a. **Absolute-temperature ratio is unit dependent.** Fixture `T3` = 10 degC. `ratio` of `T2` to `T3` claimed as 2 is rejected at step 3a. So is the same pair supplied as 293.15 K and 283.15 K (claimed about 1.035), and `percent_of` (200 %). No number is delivered, `calculation_rejected` is set, and the readings are shown instead. Invariance check: the verdict for any absolute-temperature pair is identical (always rejected) whether expressed in degC, degF or K.
7b. **Temperature intervals.** `difference` of `T2`,`T3` = 10 delta_degC. `ratio` of it to a 5 delta_degC interval is accepted as 2, with the identical result when both are expressed in K (positive control). `ratio` of an interval to an absolute temperature, or `sum` of an interval and an absolute temperature, is rejected.
7c. **Fail closed.** An item whose unit has no registry `scale`, or a temperature unit mapped to `ratio` by mistake, makes `ratio`/`percent_of` rejected. No partial result is delivered, and `status` is `evidence_only` if no claim survives.
7d. **Disclosure and unchanged operations.** A disclosure that Celsius ratios were used does not rescue 7a. `mean` of `T2`,`T3` = 15 degC is still accepted.
7. **Cross-dimension ratio.** `ratio` of `P1` to `T1` is rejected; `ratio` of `P1` to `P2` is accepted as dimensionless.
8. **Cross-dimension product.** Power x time -> energy is rejected in M2 even though `P1` x 1 h = 24.5 kWh is arithmetically right.
9. **Free-form formula or unlisted operation** (`"p1 + t1 * 2"`) is rejected at schema validation.
10. **Injected text.** An asset name or alarm subject instructs adding temperature to power; the model complies; rejected as in test 1.
11. **Provenance disclosure still works.** Same-dimension inputs with different provenance classes are accepted when the mix is stated and rejected when it is not.
12. **No residue.** After test 1 no total, ranking or summary built on the rejected calculation is delivered, and `status` is `evidence_only` if no claim survives.

### 15.3 Freshness tests (PROPOSED)

Controllable clock, permissions and placement unchanged throughout. Fixture: `E3` = 24.5 degC observed at T0, poll interval 60 s (threshold 120 s). Answer F1 "Room A is currently 24.5 degC" cites `E3`.

1. **Time advance, delivery.** Retrieve at T0+60 s (fresh). Advance the clock to T0+300 s before delivery. F1 is not delivered as present tense; it is rewritten as "As of T0: 24.5 degC" with caveat `stale`, or removed. `permission_hash` is identical.
2. **Stored result, later read.** Deliver at T0+60 s, then `GET` at T0+10 min. The response is re-evaluated: historical wording, never the stored "currently".
3. **Async completion.** The task enqueues at T0+100 s, the provider takes 40 s, the clock passes the threshold during the call. The completion pass re-evaluates and F1 is not delivered as present tense.
4. **Export.** An export at T0+1 h contains no present-tense claim on `E3`.
5. **Follow-up reuse.** A follow-up "is it still 24.5?" at T0+10 min triggers a fresh retrieval. If the new reading is unavailable, the answer is `insufficient_evidence` or historical only; the prior bundle never supports "currently".
6. **Derived evidence.** A room average of `E3` (stale) and `E4` (fresh) is rebuilt or rendered historical; it is not delivered as current.
7. **Boundary.** Age exactly at the threshold is fresh, one second past is stale (guards off-by-one).
8. **Poll interval change.** The integration's interval is raised so the item is no longer stale (or lowered so it becomes stale); the outcome follows the interval at evaluation time.
9. **Fail closed.** Clock source unavailable, missing `observed_at`, or `observed_at` in the future: the item is treated as stale.
10. **Injected text.** An asset note says "reading is always current"; the model repeats it; the claim is still downgraded.
11. **Positive control.** At T0+60 s the claim is delivered as present tense with no `stale` caveat.
12. **Mutation-sensitive.** Run the validator with each mutant: (a) trust stored `quality.stale`, (b) evaluate at `retrieved_at` instead of `evaluated_at`, (c) skip re-evaluation on GET, (d) skip it on async completion, (e) skip it on export, (f) reuse a prior bundle for present tense on follow-up, (g) let a fresh contributor refresh a stale aggregate, (h) apply the polling-interval rule to `live_read` items, (i) treat a `live_read` item as current without the re-read, (j) accept a `live_read` item whose re-read shows a changed `source_version` or lost visibility, (k) treat a `cadence` item as `live_read`. Each mutant must fail at least one test in this section; a surviving mutant fails the section.

Inventory without an integration (clock controllable; fixture: `E10` = equipment "PDU-7", `equipment.status = active`, row `version` 7, no integration and no `poll_interval_seconds`; F10 "PDU-7 is currently active" with predicate `equipment.status eq active`):

13. **Positive control, old but current.** `E10` retrieved at T0; evaluate at T0+30 days with the row unchanged and still visible. The scoped re-read matches `source_version` 7; F10 is delivered as present tense with no `stale` caveat, and the absence of a polling interval does not downgrade it. Same result on `GET`, export, async completion and a follow-up (the follow-up re-reads).
14. **Changed row.** The status is changed to `decommissioned` (version 8) after T0. F10 is not delivered as current; it is re-validated against the re-read value and removed (opposite status, 7a), or rendered "As of T0: active" with caveat `stale`. The new status may be stated only through a validated predicate.
15. **Inaccessible inventory.** After T0, the caller's site scope is revoked, or the asset moves to another site (placement change). The re-read returns the `not_found_or_denied` shape; F10 and anything derived from `E10` are removed, with no stale value, no count or "N omitted" revealing it (section 11, tests 15.1 R1 to R4). The row is not reported as merely stale.
16. **Unverifiable.** The re-read errors or times out, or `source_version` is missing on the item. The item is stale (fail closed): no present-tense delivery; historical wording only if it was supported at retrieval, otherwise removed. Status is `evidence_only` or `insufficient_evidence` if nothing survives.
17. **Relation.** F11 "PDU-7 is located in Rack R-12" (`relation.located_in`). The positive control passes when both rows are unchanged and visible. Moving PDU-7 to rack R-13 makes F11 fail, and it is never supported by co-citation.
18. **Class assignment.** An inventory tool that declares `cadence`, or a `cadence` item with no integration, is rejected at registry load (fail closed); an unknown class is stale (mutants (h) and (k)).
