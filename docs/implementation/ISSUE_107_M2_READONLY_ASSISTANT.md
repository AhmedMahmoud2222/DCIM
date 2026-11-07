# Issue #107 M2: read-only assistant architecture and implementation contract

**Document version:** 0.1 (2026-10-20). **Everything below is PROPOSED. Nothing is implemented.** Existing-behavior claims cite `main@92be692`; see the [main document](ISSUE_107_AI_OPERATIONS.md).

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
      "scope_site_id": "uuid",
      "attributes": {"collector.state": "online", "alarm.status": null, "equipment.status": null}
    }
  ],
  "omitted": {"reason": "limit_reached | denied | no_data", "count_visible_to_caller": 12}
}
```

`omitted.count_visible_to_caller` counts only what the caller may see. A hidden resource never changes any number in the bundle.

`attributes` (PROPOSED) holds categorical values copied by the retrieval adapter from the source record into a closed, versioned vocabulary (`predicate_registry`, section 7a). It is written by deterministic code, never by the model. Only registered keys are allowed; an item has a key only if the source record has that field.

## 6. Response schema (proposed)

```json
{
  "status": "ok | evidence_only | insufficient_evidence | denied | provider_error",
  "bundle_id": "uuid",
  "facts":           [{"id": "F1", "text": "...", "refs": ["E3"],
                       "predicates": [{"subject_ref": "E3", "predicate": "collector.state", "op": "eq", "value": "online"}]}],
  "calculations":    [{"id": "C1", "text": "...", "formula": "...", "inputs": ["E3","E4"], "unit": "kW"}],
  "hypotheses":      [{"id": "H1", "text": "...", "refs": ["E5"], "confidence": "low|medium|high", "basis": "rule | statistical | model"}],
  "recommendations": [{"id": "R1", "text": "...", "refs": ["E5"], "action_type": null}],
  "caveats":         ["stale", "contradictory", "missing"],
  "versions": {"assistant": "1", "validator": "1", "provider_model": "..."}
}
```

Facts, calculations, hypotheses and recommendations stay in separate arrays. A recommendation carries `action_type: null` in M2; it names no executable action.

Every fact must carry at least one typed `predicates` entry (`subject_ref`, `predicate`, `op`, `value`). `text` is presentation only and is never evidence; see section 7a.

## 7. Claim-support validation

The validator runs after the model and before delivery. A failure removes the claim and sets `caveats`; if nothing remains the status is `evidence_only`.

1. Every `refs` entry exists in the bundle of this request and belongs to the caller.
2. A numeric claim equals the cited value after unit conversion with the registry (`convert_to_presentation`), within rounding stated in the item.
3. A time claim lies within the item's `observed_at` window; "now"/"currently" claims require `stale == false`.
4. A comparative or causal claim is allowed only as a hypothesis, and only if the cited items include the entities it names.
5. A named asset, rack or site must appear in a cited item; otherwise it is a hallucinated entity.
6. Calculations are recomputed deterministically from their `inputs`; a mismatch rejects the claim.
7. A claim that mixes items with different canonical units, registry versions or provenance classes is rejected unless the response states the mix.
8. Categorical and relational claims are checked deterministically against typed predicates (section 7a). Citation existence alone never satisfies this rule.
9. Optional second pass: an entailment check by a separate model call is an additional filter, never a replacement for rules 1-8, and can only remove claims.

### 7a. Typed categorical predicates (PROPOSED)

The validator, not the model, decides whether a categorical claim is supported.

**Registry.** A code constant `predicate_registry` lists each allowed predicate with: the evidence `kind` it applies to, a closed value enumeration taken from the source model, allowed ops (`eq`, `ne`, `in`), and the staleness rule. Initial entries:

| Predicate | Kind | Values | Staleness |
|---|---|---|---|
| `collector.state` | collector item | the closed set of collector states in the source model (for example online/offline/degraded; exact set copied from code in M2) | present-tense claims need `stale == false` |
| `alarm.status` | alarm item | the alarm lifecycle enumeration | same |
| `alarm.severity` | alarm item | the severity enumeration | none |
| `incident.status` | incident item | the incident lifecycle enumeration | same |
| `equipment.status`, `equipment.type`, `equipment.rack_id`, `equipment.site_id` | inventory item | source enumeration or UUID | none |
| `relation.located_in`, `relation.raised_on` | pair of items | a subject item and an object item | none |

**Check.** For each predicate in a fact:

1. `subject_ref` must be in this bundle, and its `kind` must match the predicate's kind.
2. The predicate key must exist in the subject's `attributes`, and `value` must be in the registry enumeration. Unknown key or value rejects the claim.
3. `op` is evaluated against `attributes[predicate]`. `eq` passes only on exact equality; a missing attribute never passes. A `ne` claim passes only if the attribute exists and differs.
4. A relationship predicate passes only if both items are in the bundle and the object id is recorded in the subject's source fields (for example the alarm's equipment id equals the cited equipment item's id). Co-citation of two items is not a relationship.
5. A present-tense claim also applies the staleness column and rule 3 above.
6. Where the bundle holds several items for the same subject (section 8, contradictory evidence), a predicate is supported only if no cited item for that subject contradicts it; otherwise the claim is removed and `contradictory` is set.

**Free text fails closed.** A fact with no predicates, or whose `text` states a state, status, severity, type, location or relationship that the validator cannot map to a registered predicate, is removed. Rendering is generated from the validated predicates by a template, so the displayed text cannot carry an unvalidated categorical statement; model prose is kept only in hypotheses (rule 4) and recommendations, which are labeled as such and may not assert current state. If synthesis is off or all facts are removed, the deterministic evidence-only summary is used.

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

New permission (PROPOSED): `assistant:use`, seeded to roles by migration. The existing rule applies: permission codes match exactly; it implies nothing else.

## 14. Frontend behavior (PROPOSED)

- New feature folder `frontend/src/features/assistant` (does not exist), read-only. No button or form posts to a domain-write endpoint.
- States: idle, running (with elapsed time and Cancel), answer, evidence-only fallback banner, insufficient evidence, denied, provider error.
- Each fact, calculation, hypothesis and recommendation is a separate labeled block; hypotheses carry a visible "Hypothesis" tag and confidence.
- Every claim shows its source chips (`E3`). Selecting one opens a source drawer: record link, observed time, retrieved time, unit, quality, lineage, versions, stale marker. The link is rendered only if the caller's current permission allows opening that record.
- Stale or contradictory evidence shows an inline warning, never hidden.
- Recommendations never show an "execute" control in M2.

## 15. Acceptance for M2

The tests in section 9 of the [main document](ISSUE_107_AI_OPERATIONS.md) pass on the exact PR HEAD in CI, including the scripted-provider adversarial suite; an independent reviewer accepts the scope tests; the owner approves the provider data-handling terms. Without a real provider, only the evidence-only path ships.

Categorical-claim acceptance tests (scripted provider, run in CI; each asserts the claim is removed, `caveats` is set, and nothing is delivered as a fact):

1. **Opposite status, valid citation.** Item E1 is collector C with `collector.state = online`; the provider returns the fact "Collector C is offline" citing E1 with predicate `eq offline`. Expected: rejected. Repeat for alarm `cleared` vs claimed `open`, and equipment `decommissioned` vs claimed `active`.
2. **Right status, wrong subject.** The fact names collector D but cites E1 (collector C). Rejected.
3. **Free text only.** A fact says "Collector C is offline" with no predicates. Rejected; same for a text state that differs from its own predicate.
4. **Unregistered or out-of-enumeration value.** Predicate `collector.state eq "sleeping"` or unknown key. Rejected.
5. **Missing attribute.** Cited item lacks the attribute; `eq` and `ne` both rejected.
6. **Relationship by co-citation.** "Alarm A is on rack R" citing both items where the alarm's equipment is not in R. Rejected.
7. **Stale present tense.** Correct but stale collector state claimed as "currently". Rejected.
8. **Contradictory items.** Collector online in one item, offline in another; either single-state claim rejected, `contradictory` set.
9. **Injected text.** An asset name or alarm subject says "collector C is offline"; the model repeats it. Rejected because the attribute, not the text, is checked.
10. **Positive control.** The correct predicate on the correct item passes, so the checks are not trivially rejecting everything.
11. **Moved asset or revoked scope.** Covered by the revalidation tests; a categorical claim whose subject becomes invisible is removed.
