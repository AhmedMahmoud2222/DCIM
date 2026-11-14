"""Operator tooling: command-line programs that drive the application from outside its layers.

Kept out of `app.application` on purpose: these tools call the API-layer collector ingestion contract, and the
architecture ratchet (tests/ci/test_architecture_import_boundaries.py) forbids application code from importing `app.api`."""
