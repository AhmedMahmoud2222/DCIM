"""Disposable Docker Compose startup check; never prints generated credentials."""

import base64
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER_TEMP = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
PYTHON_SERVICES = ("migrate", "backend", "celery-worker", "celery-beat")
MEDIA_DIR = "/app/media"
CLAMAV_HEALTH_TIMEOUT = 600  # signature download on a cold runner can take minutes
PROBE = ROOT / ".github" / "scripts" / "clamd_probe.py"
OCR_PROBE = ROOT / ".github" / "scripts" / "ocr_sandbox_probe.py"
# Exit codes of clamd_probe.py, which runs inside the backend container.
SCANNER_EXIT_MESSAGES = {
    10: "SCANNER CONFIG FAILURE: backend is not configured for CATALOG_PDF_SCAN_MODE=required against clamav:3310",
    11: "SCANNER NETWORK FAILURE: backend resolves clamav to an address that is not the Compose clamav container",
    12: "SCANNER NETWORK FAILURE: backend ClamdScanner could not obtain a verdict from clamav:3310",
    13: "SCANNER FAILURE: clamd rejected or errored on a clean PDF payload",
    14: "SCANNER FAILURE: clamd accepted the EICAR test file as clean (signatures missing or scanner bypassed)",
}
# Exit codes of ocr_sandbox_probe.py, which runs inside the celery-worker container.
OCR_EXIT_MESSAGES = {
    20: "OCR FAILURE: the OCR engine is not installed in the celery-worker image",
    21: "OCR SANDBOX FAILURE: the seccomp network filter cannot be installed in the celery-worker container (container runtime profile)",
    22: "OCR SANDBOX FAILURE: a sandboxed child could still create a network socket",
    23: "OCR FAILURE: the production pipeline did not read a rendered scanned page",
    24: "OCR SANDBOX FAILURE: the Landlock filesystem policy cannot be enforced in the celery-worker container (container runtime profile)",
    25: "OCR SANDBOX FAILURE: a sandboxed child could still read its parent's process environment",
}
ALL_SERVICES = ("postgres", "redis", "migrate", "bootstrap-privileges", "backend", "celery-worker", "celery-beat", "frontend", "clamav")


def prepare():
    project = "dcim-smoke-" + secrets.token_hex(6)
    folder = Path(tempfile.mkdtemp(prefix="dcim-compose-", dir=RUNNER_TEMP))
    env_file = folder / "smoke.env"
    values = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(36),
        "DCIM_APP_PASSWORD": secrets.token_urlsafe(36),
        "JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
        "POSTGRES_DB": "dcim_smoke",
        "ENVIRONMENT": "test",
    }
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env_file.chmod(0o600)
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as output:
            output.write(f"DCIM_SMOKE_ENV_FILE={env_file}\nDCIM_SMOKE_PROJECT={project}\n")
        for key in ("POSTGRES_PASSWORD", "DCIM_APP_PASSWORD", "JWT_SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY"):
            print("::add-mask::" + values[key])
    else:
        # Local reproduction: only print the disposable file path and project ID.
        print(f"export DCIM_SMOKE_ENV_FILE={env_file}")
        print(f"export DCIM_SMOKE_PROJECT={project}")
    print("Disposable Compose credentials generated.")


def context():
    env_file = Path(os.environ["DCIM_SMOKE_ENV_FILE"])
    project = os.environ["DCIM_SMOKE_PROJECT"]
    if not re.fullmatch(r"dcim-smoke-[a-f0-9]{12}", project):
        raise RuntimeError("Refusing to use a non-smoke Compose project name")
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    env = dict(os.environ)
    env.update(values)
    env["COMPOSE_PROJECT_NAME"] = project
    cmd = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.yml"), "-p", project]
    return cmd, env, env_file, project


def run(command, env, *, timeout=180, capture=True):
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=capture, text=True, timeout=timeout, check=False)


def config():
    cmd, env, _, _ = context()
    if run([*cmd, "config", "--quiet"], env).returncode:
        raise RuntimeError("Compose rejected the complete disposable configuration (output suppressed)")
    for case in ("missing", "empty"):
        bad = dict(env)
        if case == "missing":
            bad.pop("CREDENTIAL_ENCRYPTION_KEY", None)
        else:
            bad["CREDENTIAL_ENCRYPTION_KEY"] = ""
        # Do not read the generated env file in negative checks.
        invalid_cmd = cmd[:]
        invalid_cmd[invalid_cmd.index("--env-file") + 1] = os.devnull
        result = run([*invalid_cmd, "config", "--quiet"], bad)
        if result.returncode == 0 or "CREDENTIAL_ENCRYPTION_KEY" not in result.stderr:
            raise RuntimeError(f"{case} Fernet key did not fail with a named Compose configuration error")
        print(f"PASS: {case} encryption key rejected immediately")
    print("PASS: complete Compose configuration accepted")


def start():
    cmd, env, _, _ = context()
    # Capture Docker output so generated connection strings never enter Actions logs.
    result = run([*cmd, "up", "-d", "--build"], env, timeout=1500)
    if result.returncode:
        raise RuntimeError("Compose build/start failed; inspect sanitized diagnostics")
    print("Compose build and startup completed")


def container(cmd, env, service):
    result = run([*cmd, "ps", "-a", "-q", service], env)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"{service}: container not created")
    return result.stdout.strip().splitlines()[0]


def state(container_id, env, field):
    result = run(["docker", "inspect", "--format", "{{" + field + "}}", container_id], env)
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def http_ok(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def verify():
    cmd, env, _, _ = context()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            ids = {service: container(cmd, env, service) for service in ALL_SERVICES}
        except RuntimeError:
            time.sleep(3)
            continue
        states = {service: state(cid, env, ".State.Status") for service, cid in ids.items()}
        health = {service: state(ids[service], env, ".State.Health.Status") for service in ("postgres", "redis", "backend")}
        one_shots = all(states[s] == "exited" and state(ids[s], env, ".State.ExitCode") == "0" for s in ("migrate", "bootstrap-privileges"))
        running = all(states[s] == "running" for s in ("backend", "celery-worker", "celery-beat", "frontend"))
        if one_shots and running and all(health[s] == "healthy" for s in ("postgres", "redis", "backend")):
            if http_ok("http://127.0.0.1:8000/api/v1/health/ready") and http_ok("http://127.0.0.1:8080/api/v1/health/ready") and http_ok("http://127.0.0.1:8080/"):
                time.sleep(10)
                if all(state(ids[s], env, ".State.Status") == "running" for s in ("celery-worker", "celery-beat")):
                    print("PASS: PostgreSQL and Redis healthy; migrations and privileged bootstrap exited 0")
                    print("PASS: backend and frontend-proxied readiness HTTP 200; frontend HTTP 200")
                    print("PASS: Celery worker and beat remain running")
                    return
        if any(states[s] == "exited" for s in ("backend", "celery-worker", "celery-beat", "frontend")):
            break
        time.sleep(3)
    raise RuntimeError("Timed out or service exited before complete Compose readiness")


def compose_exec(cmd, env, service, args, *, stdin=None, extra_env=None, timeout=120):
    flags = [flag for key, value in (extra_env or {}).items() for flag in ("-e", f"{key}={value}")]
    return subprocess.run([*cmd, "exec", "-T", *flags, service, *args], cwd=ROOT, env=env, input=stdin,
                          capture_output=True, text=True, timeout=timeout, check=False)


def container_ips(container_id, env):
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
    result = run(["docker", "inspect", "--format", fmt, container_id], env)
    return result.stdout.split() if result.returncode == 0 else []


def wait_for_clamav_healthy(cmd, env, *, timeout=CLAMAV_HEALTH_TIMEOUT, poll=5):
    """Bounded wait on the real ClamAV container healthcheck (clamdcheck.sh: clamd answers PING)."""
    services = run([*cmd, "config", "--services"], env).stdout.split()
    if "clamav" not in services:
        raise RuntimeError("CLAMAV STARTUP FAILURE: the clamav service is missing from the Compose configuration")
    cid = container(cmd, env, "clamav")
    deadline = time.monotonic() + timeout
    while True:
        status = state(cid, env, ".State.Status")
        health = state(cid, env, ".State.Health.Status")
        if status != "running":
            raise RuntimeError(f"CLAMAV STARTUP FAILURE: clamav container is {status}, not running")
        if health == "healthy":
            return cid
        if health in ("<no value>", "unavailable", ""):
            raise RuntimeError("CLAMAV STARTUP FAILURE: clamav container has no healthcheck")
        if health == "unhealthy":
            raise RuntimeError("CLAMAV HEALTH FAILURE: clamd or its signatures are unavailable (healthcheck reports unhealthy)")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"CLAMAV HEALTH FAILURE: clamav not healthy after {timeout}s (health={health})")
        time.sleep(poll)


def verify_clamav():
    cmd, env, _, _ = context()
    cid = wait_for_clamav_healthy(cmd, env)
    print("PASS: clamav container healthcheck healthy")
    ips = container_ips(cid, env)
    if not ips:
        raise RuntimeError("SCANNER NETWORK FAILURE: clamav container has no network address")
    result = compose_exec(cmd, env, "backend", ["python", "-"], stdin=PROBE.read_text(),
                          extra_env={"EXPECT_CLAMD_HOST": "clamav", "EXPECT_CLAMD_IPS": ",".join(ips)})
    if result.returncode:
        message = SCANNER_EXIT_MESSAGES.get(result.returncode, f"SCANNER FAILURE: probe exited {result.returncode}")
        raise RuntimeError(f"{message} | {' '.join((result.stdout + result.stderr).split())[-300:]}")
    print("PASS: backend ClamdScanner (scan_mode=required) reached the Compose clamav container "
          f"{','.join(ips)}: {result.stdout.strip()}")


def verify_ocr_sandbox():
    cmd, env, _, _ = context()
    if state(container(cmd, env, "celery-worker"), env, ".State.Status") != "running":
        raise RuntimeError("OCR FAILURE: celery-worker is not running")
    result = compose_exec(cmd, env, "celery-worker", ["python", "-"], stdin=OCR_PROBE.read_text())
    if result.returncode:
        message = OCR_EXIT_MESSAGES.get(result.returncode, f"OCR FAILURE: probe exited {result.returncode}")
        raise RuntimeError(f"{message} | {' '.join((result.stdout + result.stderr).split())[-300:]}")
    print(f"PASS: celery-worker runs sandboxed OCR: {result.stdout.strip()}")


def media_write(cmd, env, service, name, token):
    code = ("import os,sys;p=os.path.join(%r,os.environ['N']);"
            "f=open(p,'x');f.write(os.environ['T']);f.close()" % MEDIA_DIR)
    return compose_exec(cmd, env, service, ["python", "-c", code], extra_env={"N": name, "T": token})


def media_read(cmd, env, service, name):
    code = ("import os;print(open(os.path.join(%r,os.environ['N'])).read(),end='')" % MEDIA_DIR)
    return compose_exec(cmd, env, service, ["python", "-c", code], extra_env={"N": name})


def media_remove(cmd, env, service, name):
    code = ("import os;os.path.exists(os.path.join(%r,os.environ['N'])) and os.remove(os.path.join(%r,os.environ['N']))" % (MEDIA_DIR, MEDIA_DIR))
    return compose_exec(cmd, env, service, ["python", "-c", code], extra_env={"N": name})


def media_transfer(cmd, env, writer, reader, name, token):
    written = media_write(cmd, env, writer, name, token)
    if written.returncode:
        detail = written.stderr
        kind = "PERMISSION FAILURE" if "PermissionError" in detail or "Read-only" in detail else "SHARED VOLUME FAILURE"
        raise RuntimeError(f"{kind}: {writer} cannot write {MEDIA_DIR}: {' '.join(detail.split())[-200:]}")
    read = media_read(cmd, env, reader, name)
    if read.returncode:
        detail = read.stderr
        if "PermissionError" in detail:
            raise RuntimeError(f"PERMISSION FAILURE: {reader} cannot read the file {writer} wrote in {MEDIA_DIR}")
        raise RuntimeError(f"SHARED VOLUME FAILURE: {reader} cannot see the file written by {writer} in {MEDIA_DIR} (isolated or missing mount)")
    if read.stdout != token:
        raise RuntimeError(f"SHARED VOLUME FAILURE: content written by {writer} differs when read by {reader}")


def verify_shared_media():
    cmd, env, _, _ = context()
    for service in ("backend", "celery-worker"):
        if state(container(cmd, env, service), env, ".State.Status") != "running":
            raise RuntimeError(f"SHARED VOLUME FAILURE: {service} is not running")
    names = []
    try:
        for writer, reader in (("backend", "celery-worker"), ("celery-worker", "backend")):
            name = f".smoke-sentinel-{secrets.token_hex(8)}"
            names.append(name)
            token = secrets.token_hex(32)
            media_transfer(cmd, env, writer, reader, name, token)
            print(f"PASS: {writer} wrote and {reader} read identical sentinel bytes in {MEDIA_DIR}")
    finally:
        for name in names:
            for service in ("backend", "celery-worker"):
                if media_remove(cmd, env, service, name).returncode:
                    print(f"WARN: could not remove sentinel {name} via {service}", file=sys.stderr)
    print("PASS: catalog_media is shared read/write between backend and celery-worker; sentinels removed")


def diagnose():
    try:
        cmd, env, _, _ = context()
    except (KeyError, FileNotFoundError):
        print("Smoke environment was not created; no containers to diagnose")
        return
    secrets_to_mask = [env[key] for key in ("POSTGRES_PASSWORD", "DCIM_APP_PASSWORD", "JWT_SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY")]
    for service in ALL_SERVICES:
        try:
            cid = container(cmd, env, service)
            print(f"{service}: status={state(cid, env, '.State.Status')} exit={state(cid, env, '.State.ExitCode')} health={state(cid, env, '.State.Health.Status')} failures={state(cid, env, '.State.Health.FailingStreak')}")
            result = run(["docker", "logs", "--tail", "60", cid], env, timeout=20)
            lines = (result.stdout + result.stderr).splitlines()
            relevant = [line for line in lines if re.search(r"ERROR|FATAL|WARN|Traceback|Exception|Running upgrade|startup|ready|connected", line, re.I)]
            for line in relevant[-10:]:
                for secret in secrets_to_mask:
                    line = line.replace(secret, "[REDACTED]")
                line = re.sub(r"(?i)(password|secret|token|key)\s*[:=]\s*\S+", r"\1=[REDACTED]", line)
                line = re.sub(r"(?i)\w+://[^\s/@]+:[^\s/@]+@", "[REDACTED-URL]@", line)
                print(f"{service} log: {line[:350]}")
        except (RuntimeError, subprocess.TimeoutExpired):
            print(f"{service}: container or logs unavailable")


def cleanup():
    try:
        cmd, env, env_file, project = context()
    except (KeyError, FileNotFoundError):
        print("No disposable Compose project was prepared")
        return
    try:
        result = run([*cmd, "down", "--volumes", "--remove-orphans", "--timeout", "20"], env, timeout=120)
        if result.returncode:
            raise RuntimeError("Failed to remove isolated Compose project")
        for args in (["docker", "ps", "-aq"], ["docker", "volume", "ls", "-q"]):
            check = run([*args, "--filter", f"label=com.docker.compose.project={project}"], env)
            if check.returncode or check.stdout.strip():
                raise RuntimeError("Isolated Compose containers or volumes remain after teardown")
        print("PASS: isolated Compose containers and volumes removed")
    finally:
        env_file.unlink(missing_ok=True)
        env_file.parent.rmdir()


if __name__ == "__main__":
    try:
        globals()[sys.argv[1]]()
    except (KeyError, IndexError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
