# Issue #100 implementation start — datasheet review/apply workflow

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

Reuse the existing secure PDF storage, extraction/OCR sandbox, job, provenance and review APIs. Do not rebuild them.

Initial slices:
1. Catalog-admin frontend for upload/extraction job state/retry.
2. Candidate review UI with provenance, confidence, flags and model attribution.
3. Explicit accepted-candidate -> draft-revision application service/API.
4. Optimistic-concurrency, audit and immutable published/retired guards.
5. End-to-end browser and adversarial authorization tests.

Applying a candidate must remain a deliberate human action. No automatic write from extraction output to the catalog.
