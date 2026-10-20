# PostgreSQL test environment for G7 provenance

- Use a dedicated PostgreSQL 16 instance (not the shared dev port 5432).
- Provision roles/databases `dcim_app` and `dcim_test` per `backend/tests/conftest.py`.
- Run: `pytest tests/unit/test_g7_reading_provenance.py tests/api/test_g7_readings_provenance_api.py` with `DATABASE_URL` pointing at the provisioned instance.
- Exact-HEAD CI runs these against a fresh PostgreSQL 16 service container.
