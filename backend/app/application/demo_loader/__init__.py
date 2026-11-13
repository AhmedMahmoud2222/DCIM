"""Local-demonstration data loader (telemetry simulator, environmental sensors, floor-plan calibration).

Everything in this package is for a local demonstration database only. It is never imported by the API or the
workers, only run explicitly through ``python -m app.application.demo_loader`` with a confirmation flag, and it
refuses to run when ``ENVIRONMENT=production``.
"""

LOADER_VERSION = "1.0.0"
