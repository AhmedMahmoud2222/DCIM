# CFD verification and validation policy

Document version: 0.1.0
Status: PROPOSED. The numeric gates are project gates proposed for review. They are not industry standards and they are not
validated accuracy claims. No benchmark has been run and no measured comparison exists.

## 1. Vocabulary

- Verification: the code solves the equations correctly (analytic cases, mesh convergence, conservation).
- Validation: the model predicts the real room within stated error (measured versus predicted).
- A converged run is not validated. A validated run is not engineering-accepted. The four states in the service document stay separate.

## 2. Benchmark plan

| Level | Case | Known behaviour or reference | Purpose |
|---|---|---|---|
| V1 | Pure conduction through a wall slab with fixed face temperatures | linear profile | thermal boundary handling |
| V2 | Plane channel or pipe flow at low Reynolds number | analytic profile / friction | momentum boundary, wall treatment |
| V3 | Differentially heated cavity (Boussinesq regime, published reference data) | published Nusselt number and temperature profile | buoyant flow, compare Boussinesq and variable-density formulations |
| V4 | Heated plume above a point or area source in a closed box | scaling of plume rise and centreline temperature | buoyancy with a volumetric source |
| V5 | Empty room with supply and return, no heat | mass balance, residence behaviour | boundary compatibility rules |
| V6 | Single rack with prescribed airflow and heat in a box | exhaust temperature rise equals heat divided by (density x specific heat x flow) | rack source model and energy balance |
| V7 | Rack row with cold/hot aisles, representative of the target rooms | self-consistency, mass and energy balance, mesh study | representative case |
| V8 | Cooling unit off case on V7 | new equilibrium, no change to inventory | steady-failure behaviour; labelled "equilibrium, not a time estimate" |
| V9 | Real-room case with measured data | measured versus predicted | validation, only when data meets section 6 |

Reference values for V3 and V4 must be cited from primary literature in M3, with the citation stored beside the case.

## 3. Mesh refinement

Three meshes with a refinement ratio of at least 1.3 in each direction (cell size ratio between successive levels). For
critical inlet probes, hotspot probes and integral quantities (total heat removed, supply flow) compute the observed order
and a Richardson/GCI-style estimate. A mesh result is accepted only if the three solutions are in the asymptotic
or monotone range; if the sequence oscillates, the finest result is not accepted and the report says so. The estimate is
a numerical discretization uncertainty indicator, not a probabilistic confidence interval.

## 4. Conservation checks

Definitions. All fluxes are summed over contract patches from the field output.

- The mass criterion is chosen by the boundary topology declared in the contract, never by the case label or by the
  computed result. A domain is **ventilated** if any patch has role `supply`, `return`, `leakage` or `opening`, or any
  `flow_rate`/`pressure` momentum type. Otherwise it is **sealed** (all patches are walls, `no_slip`, no through-flow).
  A domain classed as sealed that has a non-wall patch with non-zero flux is rejected as `rejected_balance`.
- Ventilated mass imbalance = |sum of signed mass flow over all patches| / sum of absolute inflow over all patches. If the
  sum of absolute inflow is below 1e-3 kg/s the case is `not_evaluable` and the run is rejected, because a ratio over a
  near-zero denominator gives no information. This rejection applies to ventilated domains only.
- Sealed mass criterion. Boundary through-flow is zero by construction, so the ratio above is not defined. Instead:
  (a) the absolute net boundary mass flow, summed over all patches, is at most 1e-9 kg/s (numerical zero) and no patch
  carries flux above that magnitude; (b) the total fluid mass in the domain (volume integral of density) drifts by at most
  0.1 % over the averaging window relative to its initial value (a variable-density formulation is not allowed to create or
  lose mass); (c) the internal continuity residual, the volume-integrated absolute divergence of the mass flux normalised
  by the total fluid mass per second of the characteristic buoyancy time scale, is below the stated floor. A correct
  sealed solution passes. Non-conservation is still detected: any leak flux, mass drift or continuity residual above these
  bounds fails with `rejected_balance`. The sealed pressure level is fixed by an internal pressure reference (input
  contract section 7, rule 1).
- Benchmark mapping. Sealed (sealed criterion): V3 and V4. V1 is solid conduction with no fluid domain: the mass criterion
  is not applicable and only the energy criterion (wall heat flux balance) is applied. Ventilated (ventilated criterion):
  V2 when run with inlet and outlet patches (a periodic channel is treated as sealed in the flow direction and uses the
  sealed criterion plus the prescribed mass flux), V5, V6, V7 and V8, and V9 for real rooms. The M3 report states which
  criterion each case used.
- Energy imbalance = |Q_sources + H_in - H_out - Q_walls| / max(Q_sources, Q_floor), with H the enthalpy flux referenced
  to the same datum on all patches and Q_floor a fixed small power (default 100 W) that prevents division by near zero.
  With Q_sources below Q_floor the absolute residual in watts must be below 1 W instead.
- Both imbalances are evaluated on the averaging window, not the last iteration.

## 5. Proposed gates and review of the starting targets

| Gate | Proposed start | Assessment |
|---|---|---|
| Mass imbalance | at most 0.5 % (ventilated cases); sealed cases use the sealed criterion in section 4 | Keep for steady ventilated runs with converged pressure. It is a numerical check, not a measurement claim. Tighten to 0.1 % for ventilated V2, V5 and V6. Sealed V3 and V4 are judged by the sealed criterion, not by this ratio |
| Energy imbalance | at most 1 % | Keep. Wall and leakage paths must be counted. With a dominant sensible load a 1 % error on heat is a small number of watts, so the absolute floor in section 4 applies at low load |
| Mesh sensitivity at critical inlet probes | at most 0.5 degC between the two finest meshes | Keep as the primary gate for inlet probes. Add: at most 1.0 degC at hotspot and exhaust probes, and at most 2 % on total heat removed. The 0.5 degC value is close to common air-temperature sensor accuracy, so it is a strict numerical requirement |
| Holdout MAE | at most 2 degC | Keep as a project gate only after the sensor-accuracy rule below. If combined measurement uncertainty exceeds 1 degC, the comparison cannot discriminate and the verdict is `insufficient_data` |
| Absolute bias (mean signed error) | at most 1 degC | Keep. A bias above this value signals a systematic boundary or airflow error |
| Maximum absolute error | at most 5 degC | Keep as the all-sensor ceiling. Propose a separate ceiling of 3 degC on rack-inlet probes, because inlet temperature is the operational question. Owner decision |
| Minimum coverage | see below | New gate |

None of these numbers has a validation basis in this repository. The owner may revise them. Any change creates a new
policy version (`vv/2`), and runs record the policy version they were judged under.

### Error metrics (reported for every validation)

For each eligible holdout sensor i with prediction p_i, measurement m_i: error e_i = p_i - m_i.
Bias = mean(e_i). MAE = mean(|e_i|). RMSE = sqrt(mean(e_i^2)). Max error = max(|e_i|). Inlet error and hotspot error are
reported separately for sensors with role `rack_inlet` and for the sensors flagged as hotspots by the measured data.
Coverage = eligible holdout sensors / eligible sensors in the room, plus the fraction of rack inlets within a stated distance
of an eligible rack-inlet temperature probe (not of any sensor).

### Sensor accuracy

Each sensor has an accuracy `a_i` (K) from a datasheet or calibration certificate. This field does not exist in the data model
today (see the readiness matrix). Combined comparison uncertainty u_i = sqrt(a_i^2 + s_i^2 + g_i^2) where s_i is the standard
deviation of the sensor over the window and g_i is the temperature sensitivity to position error (position error bound times
the local predicted gradient). A pass requires each aggregate gate to be met, and the gate to be larger than the median u_i.
Sensors with unknown accuracy are not eligible for validation.

### Minimum spatial coverage and sample size (proposed)

- At least 8 eligible holdout sensors, and at least 2 per cooling zone served.
- At least 30 % of rack inlets in the modelled region have an **eligible rack-inlet temperature probe** (role `rack_inlet`,
  metric `temperature_c`, known accuracy, acceptable provenance status) within 1 m, with `elevation_mm` compatible with the
  inlet height and a position-error bound (rack-mounted sensors flagged `position_exact=false` included) well below the 1 m
  distance, or the report restricts its claim to the covered racks. Exhaust, return-air, supply-air, humidity, airflow and
  ambient sensors never count towards this fraction, and a probe on the opposite face of a rack does not cover its inlet.
  Proposed, not owner-approved.
- At least 4 sensors in the calibration set (unless no calibration is used).
- Fewer than that: verdict `insufficient_data`; the run may stay converged and accepted for exploration with the label
  "not measurement validated". It cannot carry the word "validated".

### Calibration versus holdout

The split is made before the run, deterministically, stratified by role and zone, using a stored seed approved by the owner
(for example a hash of sensor id and dataset id). Calibration sensors may tune only declared calibration parameters (for
example leakage fraction, supply flow within its uncertainty) with bounds from the input contract. Holdout sensors are never
used in tuning. If the holdout is consulted after tuning, it is no longer a holdout and the dataset is closed.

### Frozen windows and eligibility

The window is chosen from measured data before any prediction is computed and stored with a hash. Eligibility rules:

1. Raw readings only (not daily aggregates).
2. Provenance status in `pinned`, `inferred` or `operator_resolved` (G7 statuses). `unverified_legacy` and
   `unverified_registry` are ineligible.
3. Canonical unit; no `invalid` state; at least 90 % of the expected samples in the window present, using the poll interval.
4. Stationary: absolute slope of the sensor median below 0.5 K/h over the window, and the room load within 5 % of its mean.
5. The cooling units and rack loads were in the same state for the whole window (from operating status and the thermal load
   completeness logic). Windows crossing a state change are excluded.
6. Window length at least 15 minutes and at least 10 samples per sensor (values to be revisited with real poll intervals).
7. Sensor position known with an error bound; rack-centre approximations (`position_exact = false`) are ineligible for inlet
   comparison unless the bound is stated and included in g_i.

### Sensitivity analysis

Required runs: turbulence model (SST versus a k-epsilon variant), buoyancy treatment (variable density versus Boussinesq),
supply flow and supply temperature within their uncertainty, leakage fraction within its bound, rack airflow within its
bound. The report shows the range of key outputs across these runs as a sensitivity range. It is not a probabilistic
confidence interval and is not labelled one. A probabilistic interval requires a stated sampling design and
distributions and is out of scope until a method is accepted.

## 6. Numerical convergence criteria (summary, details in M3)

A run is `converged` only if: residuals dropped by at least three orders of magnitude or reached a stated floor for every
equation; monitored quantities (critical probes, exhaust temperature, supply flow, total heat removed) have a relative drift
below a policy tolerance over the averaging window; mass and energy imbalances pass; no bounded-field clipping dominates (the
share of clipped cells stays below a stated fraction); temperatures are within the physical bounds of the contract.
Exit code zero and low residuals alone are never sufficient.

## 7. Real-room validation reporting

Each validation report states: input document hash, solver and image digest, mesh level used, window, split, eligible and
excluded sensors with reasons, the metrics above with the numbers, coverage, sensitivity ranges, all assumptions, all gates
that failed, and the sentence "Project gates are proposed internal criteria, not an industry standard or an accuracy
guarantee". If no real data meets section 5, the report says "no measured comparison available".

## 8. What blocks validation today

Sensor accuracy and calibration fields are absent; sensor x/y can be approximated from rack footprints; z is optional; room
and rack heights, cooling geometry and containment surfaces are missing; independent acceptance of #105 is not recorded.
These are listed with their routes in the main document (C.2).
