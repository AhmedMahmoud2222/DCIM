"""Check the resolved Compose/backend Settings contract without starting containers.

Run from an environment with backend dependencies and Docker Compose installed:
    python backend/scripts/check_compose_settings.py
Use --compose-file to test a previous configuration. All values used here are
ephemeral test fixtures; rendered configuration and keys are never printed.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("migrate", "backend", "celery-worker", "celery-beat")


def check(compose_file: Path, compose_command: list[str]) -> None:
    # Do not inherit CI secrets, COMPOSE_FILE overrides or a developer's .env.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT") if key in os.environ}
    env.update(
        POSTGRES_PASSWORD=secrets.token_urlsafe(32),
        DCIM_APP_PASSWORD=secrets.token_urlsafe(32),
        POSTGRES_DB="dcim_config_check",
        JWT_SECRET_KEY=secrets.token_urlsafe(48),
        CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
        ENVIRONMENT="test",
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env") as empty_env:
        command = [
            *compose_command, "--project-directory", str(ROOT), "--env-file", empty_env.name,
            "-f", str(compose_file.resolve()), "config", "--format", "json",
        ]

        def render(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(command, env=values, capture_output=True, text=True, timeout=60, check=False)

        result = render(env)
        if result.returncode:
            raise RuntimeError("Compose config failed with all required test variables supplied (output suppressed).")
        services = json.loads(result.stdout)["services"]
        required = {name.upper() for name, field in Settings.model_fields.items() if field.is_required()}
        omissions = []
        for name in SERVICES:
            values = services[name].get("environment", {})
            missing = sorted(key for key in required if not values.get(key))
            if missing:
                omissions.append(f"{name}: {', '.join(missing)}")
                continue
            if values["CREDENTIAL_ENCRYPTION_KEY"] != env["CREDENTIAL_ENCRYPTION_KEY"]:
                raise RuntimeError(f"{name}: encryption key must come from the shared environment variable.")
            # Required-field presence is checked above so ambient Settings fallbacks
            # cannot hide omissions. Exercise both Settings and the actual key codec.
            validation = subprocess.run(
                [sys.executable, "-c", "from app.core.config import Settings; "
                 "from cryptography.fernet import Fernet; "
                 "Fernet(Settings(_env_file=None).credential_encryption_key.encode('ascii'))"],
                env={"PATH": env.get("PATH", ""), "PYTHONPATH": str(ROOT / "backend"),
                     **{key: str(value) for key, value in values.items() if value is not None}},
                capture_output=True, text=True, timeout=60, check=False,
            )
            if validation.returncode:
                raise RuntimeError(f"{name}: resolved environment fails Settings/Fernet validation (output suppressed).")
        if omissions:
            raise RuntimeError("Compose omits required backend settings: " + "; ".join(omissions))
        print(f"PASS: all {len(required)} required Settings fields supplied to all four Python services; shared Fernet key valid")

        for label in ("missing", "empty"):
            invalid_env = dict(env)
            if label == "missing":
                del invalid_env["CREDENTIAL_ENCRYPTION_KEY"]
            else:
                invalid_env["CREDENTIAL_ENCRYPTION_KEY"] = ""
            result = render(invalid_env)
            if result.returncode == 0 or "CREDENTIAL_ENCRYPTION_KEY" not in result.stderr:
                raise RuntimeError(f"Compose must reject a {label} encryption key with a named configuration error.")
            print(f"PASS: {label} CREDENTIAL_ENCRYPTION_KEY rejected by Compose interpolation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=ROOT / "docker-compose.yml")
    parser.add_argument("--compose-command", nargs="+", default=["docker", "compose"])
    args = parser.parse_args()
    check(args.compose_file, args.compose_command)
