# ADR-0013: CFD solver selection for issue #106

Document version: 0.1.0
Status: PROPOSED. Not accepted. Primary-source verification and image pinning are outstanding (see "Verification status").

## Context

Issue #106 needs a steady-state, single-room, air-cooled thermal and airflow prediction that runs inside the existing
deployment model (FastAPI, PostgreSQL, Redis/Celery, no uncontrolled external service). A heat map from #105 is an
interpolation of sensors and does not model airflow. The solver output must be reproducible, benchmarkable and clearly
separated from any claim of validation.

## Candidates

| Criterion | OpenFOAM (preferred) | Code_Saturne | Commercial packages (for example Ansys Fluent/Icepak, Simcenter) |
|---|---|---|---|
| Thermal and airflow physics | Steady and transient buoyant RANS with variable-density or Boussinesq buoyancy, porous zones, volumetric heat sources, wall functions, many turbulence models | Buoyant flow with Boussinesq and variable density, porous media, volumetric sources, RANS and LES | Mature electronics and data-hall workflows, built-in fan/perforated-tile/rack objects |
| Reproducibility and benchmarking | Text case files under version control, deterministic on identical decomposition and build, scriptable | Text/XML setup, scriptable | Binary or GUI-driven projects; scripting varies by vendor |
| Deployment and isolation | Runs headless in an OCI image as a non-root process; no license server | Headless in an image; fewer prebuilt images | License servers, activation and vendor runtimes complicate offline, isolated workers |
| Licensing | GPL-3.0-or-later for the code. Running it as a separate process behind an API does not link it into the DCIM application. Distribution of the image requires GPL compliance | GPL-2.0-or-later | Proprietary, per-seat or per-core cost, usage terms limit automation |
| Maintenance and support | Two active distributions with different conventions (ESI-OpenCFD "openfoam.com" and OpenFOAM Foundation "openfoam.org"); large community; commercial support from several vendors | Smaller community, institutional maintainer | Vendor support, vendor release cadence |

Licensing statements above come from general project knowledge. The licence texts and the exact distribution licence files must be
read from the pinned release before acceptance (see "Verification status").

## Decision (proposed)

Use OpenFOAM, as one distribution and one release, in an isolated container. Code_Saturne is the credible open-source
alternative if the OpenFOAM verification cases in the V&V policy fail or the licensing review rejects GPL-3.0 packaging.
A commercial package is out of scope for the first increments because of licence servers and automation limits; it can serve
as an independent cross-check later and is not a dependency.

### Distribution and version pinning contract

The contract fixes the shape of the pin. The values are not known to this document and must not be guessed.

| Field | Rule |
|---|---|
| Distribution | Exactly one of ESI-OpenCFD or Foundation. Mixing dictionary names, solver names or utility options between them is prohibited. This ADR recommends deciding by running the benchmark set in the V&V policy on both and picking the one whose steady buoyant solver and case syntax the project can pin, with the owner confirming |
| Release | One release tag, recorded with its release date and the primary release-notes URL |
| Container | OCI image referenced only by digest (`image@sha256:...`), built or mirrored into the project registry, with the build recipe and base-image digest stored. Tags are never used at runtime |
| Executable contract | A fixed list of permitted executables (for example mesh generation, `checkMesh`, one solver binary, a post-processing utility) addressed by absolute path, each with a fixed argument grammar. No shell, no user-supplied arguments, no `codeStream`/`#codeStream`, `#calc`, `#includeEtc` outside the image tree, `libs`/`functions` entries outside an allow-list |
| Case syntax | Server-owned templates for the pinned distribution only. The template version, distribution and release are stored in the run's input contract |
| Build reproducibility | Same image digest, same case hash, same decomposition and thread count must reproduce the same field values to the stated tolerance in the V&V policy. Differences across hardware are tolerated only within that tolerance |

### Physics choices (proposed)

1. Scope: single room, air-cooled, steady-state, buoyant RANS, one fluid (air), no humidity transport, no condensation,
   no radiation in the first increment (declare it; add a radiation sensitivity case later), no chilled-water plant.
2. Buoyancy: variable-density, ideal-gas air with the pressure formulation that separates hydrostatic pressure from the
   dynamic part (the `p_rgh` family of formulations in the pinned distribution). Boussinesq remains a verification mode.
   Reason: the temperature spread in a data hall can reach 15 to 25 K between supply and exhaust and larger near an exhaust
   face. The Boussinesq approximation is conventionally accepted when relative density variation is small, often quoted as
   a few percent to about ten percent of the reference. At a reference of 295 K, a 20 K difference is about 7 percent
   in density. That sits inside the range where the approximation is arguable, so it cannot be assumed safe for hotspot
   questions. The variable-density formulation costs little more and removes the assumption. Boussinesq is kept to reproduce
   textbook cases that are defined under it. A benchmark compares both on one rack case and the difference is reported.
3. Turbulence: k-omega SST as the reference model with wall functions chosen by the local first-cell y+; realizable k-epsilon
   or RNG k-epsilon as a sensitivity alternative. Reason: SST is robust for separated and recirculating flows near rack faces.
   The turbulence-model sensitivity case in the V&V policy decides whether this choice stays. No claim of model superiority is
   made without that evidence.
4. Rack representation (needs engineering review before M3): volumetric heat source spread over the rack volume, airflow
   through the rack as a porous/resistance zone or a fan-driven face, with the volumetric flow supplied as an input with
   provenance. The exhaust temperature rise is an output and is compared to heat/(rho cp flow). It is not imposed.
   Airflow is never derived from a temperature difference.
5. Cooling supply and return: explicit surfaces with fixed supply temperature and one flow-setting boundary per
   mass-conserving group; return and leakage surfaces use pressure-type boundaries. See the input contract for the
   over-constraint rules. Rated capacity is a post-run limit check, never a boundary condition.
6. Walls and leakage: declared adiabatic by default; any conduction coefficient or leakage area is an explicit input with
   provenance, otherwise the run states the assumption in its report.
7. Air properties: declared constants for molecular viscosity and heat capacity (or temperature-dependent laws from the pinned
   thermophysical library), reference pressure from the site setting. Values are stored in the input contract.

### What a cooling-failure case means

A steady-state run with a unit switched off predicts a new equilibrium temperature field with the stated boundary conditions,
if that equilibrium exists and the solver converges to it. It does not predict time to overheating, ride-through, thermal
mass effects or transient hotspot growth. Time-to-threshold needs a separately verified transient model with thermal mass of
air, racks, building and equipment, and its own validation. The report for such a case carries the label
"steady-state equilibrium, not a time estimate".

### Geometry generation and meshing (proposed, checked in M3)

1. The geometry is generated from the input contract by server-owned code into a closed watertight surface set: room shell,
   rack boxes (or porous-zone cells), cooling-unit surfaces, obstacles, containment surfaces. Every surface patch is named
   from the contract identifiers.
2. Mesh: block-mesh plus snapping/refinement from the pinned distribution, with three refinement levels for the study. A
   run is rejected if any cell-count, memory or time cap is hit.
3. Mesh quality gates (all recorded; thresholds are starting values that M3 must justify with the benchmark meshes):
   `checkMesh` passes with zero failed checks; maximum non-orthogonality at most 70 degrees and average at most 30; maximum
   skewness at most 4; aspect ratio at most 100 outside boundary layers; no negative volumes; a single connected fluid
   region; all contract patches present and non-empty with the contract's areas within 1 percent; first-cell y+ range
   reported for walls and rack faces.
4. Convergence and conservation: a run is only "converged" when all hold in the stated averaging window: monitored
   quantities (critical inlet probe temperatures, exhaust temperature, supply mass flow, total heat removed) vary by less than
   the policy tolerance; residual drop of at least three orders of magnitude, or an absolute floor, per equation; mass and
   energy balances inside the V&V limits. Exit code zero and low residuals alone never qualify a run.
5. Reproducible case generation: case directory built only from the immutable input document, template version and pinned
   image digest. The hash of the case tree is stored.
6. Artifact provenance: the input document hash, template hash, image digest, executable list with versions, host
   resource limits, start and end times, per-attempt logs and result files with SHA-256 are stored with the run.
7. Conditions that reject a run (no result is shown as a prediction): input contract incomplete or incompatible; geometry not
   closed or not single-region; mesh gate failure; solver divergence, NaN or temperature outside the physical bounds in the
   policy; mass or energy imbalance above the limit; any monitored quantity still drifting; wall-clock, memory or disk
   limit reached; a cancelled or timed-out attempt; a result artifact checksum mismatch; a boundary patch missing in the
   output; pinned digest or template mismatch.

## Verification status

| Item | Status in this increment |
|---|---|
| Primary documentation for the pinned release | NOT VERIFIED. The documentation hosts (`openfoam.org`, `openfoam.com`, `code-saturne.org`, `doc.cfd.direct`) are not reachable from the authoring container (DNS failure). Web search returned secondary pages only. A search snippet suggests ESI-OpenCFD announced v2606 in June 2026 and a Foundation release numbered 14 in July 2026; neither is confirmed here |
| Licence text of the pinned release | NOT VERIFIED |
| Exact solver name and dictionary syntax of the buoyant steady solver in the chosen distribution | NOT VERIFIED. Names differ between distributions and have changed between Foundation versions; this is the reason a single distribution is pinned |
| Container image digest | NOT RECORDED. No image was pulled or built in this increment |
| Benchmark results | none |

These items block acceptance of this ADR as a final decision. M3 starts with a verification note that cites the primary
documentation URLs and release notes of the chosen release and records the digest.

## Consequences

- One distribution means the project owns one set of templates. Switching later is a new ADR and a new template set.
- GPL packaging obligations apply to any redistributed image. The DCIM application code does not link the solver.
- The first increment excludes humidity, radiation, transient behaviour and plant modelling. Reports must state this.
- If the verification cases fail on OpenFOAM, the fallback is Code_Saturne under a new ADR.
