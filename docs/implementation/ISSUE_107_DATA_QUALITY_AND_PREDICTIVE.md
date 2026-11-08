# Issue #107 M3/M4: data quality, anomaly detection, predictive maintenance and capacity

**Document version:** 0.1 (2026-10-20). **PROPOSED design; nothing here is implemented.** Existing-behavior claims cite `main@92be692`. CFD is not required for any telemetry-only detector in this document.

## 1. Two separate finding types

| | Data-quality finding | Operational anomaly |
|---|---|---|
| Question | Can the reading be trusted? | Is the monitored system behaving abnormally, given trusted data? |
| Examples | Missing, stale, duplicate, skewed, wrong unit, flatline | Temperature rising out of pattern, load step change |
| Effect | Blocks or down-weights anomaly detection on that series; shown to operators | Raises an anomaly with evidence |
| Never | Reported as a physical event | Raised from series that currently have a blocking quality finding |

A series with a blocking data-quality finding produces no anomaly. The UI shows the quality finding instead.

## 2. Deterministic data-quality checks (M3 first slice)

Each check is versioned (`dq.<check>@<n>`), pure over a window of stored readings, and reproducible from the same rows.

| Check | Input (existing source) | Rule | Configurable |
|---|---|---|---|
| Missing/stale reading | `TelemetryReading.occurred_at`, `Integration.poll_interval_seconds` | No reading for `k x interval` (default k=2 stale, k=5 missing) | k, per metric override |
| Collector outage | `CollectorState`, `CollectorTransition` (`collector_health_sweep`) | Collector `offline` or `stale` over the window; readings in the gap are marked "not collected", not "zero" | Reuses 90 s / 300 s thresholds |
| Duplicates | `telemetry_ledger.note_duplicate_suppressed` counts, `(collector_id, dedup_key)` | Duplicate-suppression rate above threshold per collector and hour | Rate threshold |
| Out-of-order | `occurred_at` vs `received_at` | Share of readings arriving later than `late_after`; reading older than replay horizon is `ReplayHorizonExceeded` | `late_after` |
| Clock skew | `received_at - occurred_at` | Negative lag beyond `CLOCK_SKEW_SECONDS` (60 s) or persistent positive lag | Skew tolerance |
| Unknown/incompatible unit | `registry.validate_metric_unit`, mapping contract holds (`register_contract_hold`) | Open contract hold or unit outside `allowed_source_units` | none (registry-defined) |
| Invalid provenance | `ReadingProvenanceStatus` | `unverified_legacy`, `unverified_registry` or missing mapping revision for a series expected to be pinned | Expected status per integration |
| Range | `MetricSemantics.validate_value` | Value outside canonical range or non-finite | Registry version |
| Rate of change | consecutive readings | `abs(dv/dt)` above metric limit (physical plausibility, e.g. temperature) | Per metric, versioned |
| Flatline | window of readings | Zero variance across `n` samples where the sensor normally varies (excluding `state` kinds) | `n`, minimum variance |

Output record (PROPOSED): `series_key`, `check`, `check_version`, `window_start`, `window_end`, `evidence_refs[]`, `severity` (`blocking | warning | info`), `status` (`open | resolved | suppressed`), `suppressed_by` (maintenance window or operator), `created_at`. Findings are derived and idempotent: the same window and version produce the same finding key.

Operator feedback: confirm, dismiss with reason code, or suppress for a period. Feedback is data for evaluation; it never edits thresholds automatically.

## 3. Operational anomaly detection

Order of introduction:

1. **Rule baselines**: static thresholds already in `alarm_service` stay authoritative for alarms. Anomaly rules add rate and deviation from a rolling per-series baseline, still deterministic.
2. **Robust statistical detectors** (median/MAD or seasonal-hour baseline) over canonical-unit series, versioned (`anom.<name>@<n>`).
3. **Learned models** only after the evaluation protocol below passes.

Common requirements:

- Inputs are canonical-unit readings with provenance. Mixed provenance eras are never combined into one baseline (same grouping rule as `frontend/src/features/telemetry/TelemetryTrend.tsx`).
- Every anomaly stores: detector name and version, parameter set hash, evidence window, baseline window, evidence refs (reading ids or aggregate ids), score, and the exclusions applied.
- **Exclusions**: collector outage periods, open blocking quality findings, maintenance windows and operating-mode flags (source not yet available, decision 7).
- Thresholds live in configuration with an audit trail; changing one creates a new parameter-set hash. Detections are reproducible by rerunning detector version and parameters over the stored window.
- An anomaly is a hypothesis with evidence, not an alarm. It becomes an alarm only through an existing alarm rule path.

## 4. Evaluation protocol (all detectors, required before M3 acceptance)

1. **Dataset.** Fixed replay set exported from a representative database, with an id and hash. Synthetic injections (flatline, step, drift, dropout, clock skew) are labeled separately from real events and never mixed into real-event scores.
2. **Labels.** Operator-confirmed events and maintenance windows, recorded with who and when. If labels do not exist, the report says so and gives no precision claim.
3. **Time-separated splits.** Tune on earlier periods, report on a later untouched period. No random split of a time series.
4. **Metrics.** Per detector and per metric: precision, recall, false positives per series-week, missed events, detection delay (median, p95), alert volume per operator-day.
5. **Operating modes.** Report separately for maintenance, failover and commissioning periods; state whether they were excluded.
6. **Baseline comparison.** Every model must beat the best deterministic detector on the same data, or the deterministic one ships.
7. **Reproducibility.** Report names repository SHA, detector version, parameter hash, dataset hash and random seeds.

Documentation checks and unit tests do not establish detection quality. Only this protocol on real data does.

## 5. Alarm correlation (M4)

`correlation_service.correlate` (`METHOD_VERSION = "1"`, 24 h lookback) remains the authoritative rules engine. Changes ship as a new method version with its own evaluation against labeled incidents (probable-cause accuracy, over-merge and under-merge rate). The assistant presents `rationale` and `evidence` from the stored incident and does not recompute causality.

## 6. Capacity-risk forecasting (M4)

Vocabulary the system must keep apart (`power_capacity.CapacityFigures` and `power_forecast.Forecast` already separate several):

| Term | Meaning | Source |
|---|---|---|
| Measured demand | Observed load from telemetry, with `load_basis = measured` | `power_history` snapshots |
| Estimated demand | Load from nameplate or allocation | `load_basis` other than measured |
| Rated capacity | Equipment or breaker rating | `PowerCapacity` |
| Calculated availability | Rated minus allocation, with A/B redundancy rules | `power_capacity` |
| Forecast | Projected demand with method, window, uncertainty | `power_forecast` or successors |

Requirements for any forecast beyond `linear_ols_v1`:

- Output includes method id and version, input window, measured-vs-estimated share, coverage, point estimate and an uncertainty interval with its stated coverage level (for example 80%). The current `confidence` string is not an interval.
- Insufficient data returns a refusal status and reason: keep `sparse`, `stale`, `missing`, `estimated_mixed`; add `non_stationary` when a changepoint test fires inside the window.
- Evaluate by rolling-origin backtests: error of predicted exhaustion date and of headroom at 7/30/90 days, against `linear_ols_v1` as baseline.
- Drift monitoring: compare recent forecast errors with the backtest distribution weekly; a sustained shift sets the forecast to `degraded` and surfaces it. Retraining or recalibration is a versioned, reviewed change.
- Never present a forecast as measured fact; the assistant labels it a calculation or hypothesis with its interval.

## 7. Predictive maintenance (M4+, gated)

The repository holds no equipment failure or repair history and little component-level health telemetry (PSU, fan, disk, UPS battery). Therefore:

- **Minimum data before any model.** Per failure mode and equipment class: at least 30 independent confirmed failures (or the number a statistician agrees gives an interval narrow enough to act on), at least 12 months of continuous health telemetry covering them, repair records with timestamps and root cause, and maintenance events to exclude. With less, report descriptive statistics only.
- **Labeling.** A label is a confirmed failure with failure time, mode and confirmation source (work order, vendor RMA, operator). Right-censored units (still working) are kept for survival methods. Labels come from maintenance records, not from alarms alone, and a labeling guide is versioned.
- **Claims.** The system does not report a "probability of failure" unless the model is calibrated on held-out time-separated data (reliability curve, Brier score, calibration slope) and the report shows the interval. Until then, outputs are labeled *risk indicators* (rank-ordered or rule-based, with evidence) and carry no probability.
- **Uncertainty.** Every output carries an interval or a rank with the number of comparable units; "insufficient data" is a first-class result with the missing requirement named.
- **Drift.** Monitor input distribution shift, calibration over time and alert-to-failure lead time; degrade or disable on thresholds agreed with the owner.
- **Explainability.** Each risk indicator lists the contributing readings or events by reference.
- **Human use.** Output is advisory. It cannot trigger any action without the M5 path.
