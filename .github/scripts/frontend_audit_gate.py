#!/usr/bin/env python3
"""Fail-closed frontend dependency audit gate with an optional, INACTIVE picomatch exception.

SECURITY EXCEPTION NOT APPROVED OR ACTIVE. No workflow calls this file; the enforced CI gate is
still `npm audit --audit-level=high`, which fails while GHSA-fc5q-39xc-vjpf (picomatch <= 4.0.7,
no patched release) is reported. Threat model, failure modes and the activation procedure are in
.github/security-exceptions/README.md.

--mode strict (default)  Fails on any high/critical advisory and on any production vulnerability.
                         Never reads the approval record, so it cannot grant an exception.
--mode exception         Also tolerates high findings caused EXCLUSIVELY by the advisory id
                         hard-coded below, and only while every check holds: valid unexpired
                         owner record, zero production vulnerabilities, no other high/critical
                         advisory in any chain, no upstream fix (npm registry and GitHub advisory
                         API), and no use of the vulnerable glob tooling in frontend/src or e2e.

Every ambiguity fails closed. Exit status: 0 only when the policy is satisfied, 1 otherwise,
including unexpected exceptions (argparse usage errors exit 2, also nonzero).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

from frontend_audit_report import (  # noqa: E402 - sibling module in the same directory
    EXCEPTION_ADVISORY,
    EXCEPTION_PACKAGE,
    EXCEPTION_RANGE,
    Analysis,
    AuditReport,
    GateError,
    analyze,
    clause,
    is_int,
    parse_audit,
)

# --------------------------------------------------------------------------- constants

LAST_VULNERABLE = (4, 0, 7)
ADVISORY_API_URL = "https://api.github.com/advisories/" + EXCEPTION_ADVISORY


CONFIG_SCHEMA_VERSION = 1
CONFIG_KEYS = (
    "schema_version",
    "advisory",
    "approved",
    "approved_by",
    "approval_reference",
    "approved_on",
    "expires_utc",
)
MAX_LIFETIME_DAYS = 45
APPROVAL_REFERENCE_RE = re.compile(
    r"^https://github\.com/AhmedMahmoud2222/DCIM/(?:issues|pull)/[0-9]+#"
    r"(?:issuecomment|pullrequestreview|issue)-[0-9]+$"
)
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
EXPIRY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

PROHIBITED_MODULES = ("picomatch", "minimatch", "tinyglobby", "fdir")
SCAN_DIRS = ("src", "e2e")
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf", ".mp4", ".webm", ".zip", ".gz"}
)

NPM_TIMEOUT_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 20
DEFAULT_CONFIG_RELATIVE = ".github/security-exceptions/frontend-audit-picomatch.json"


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[[Sequence[str], Path], CommandResult]
AdvisoryFetcher = Callable[[], object]


# --------------------------------------------------------------------------- IO seams


AUDIT_PROD_COMMAND = ("npm", "audit", "--omit=dev", "--audit-level=low", "--json")
AUDIT_DEV_COMMAND = ("npm", "audit", "--audit-level=high", "--json")
VERSIONS_COMMAND = ("npm", "view", EXCEPTION_PACKAGE, "versions", "--json")


def run_command(args: Sequence[str], cwd: Path) -> CommandResult:
    """Run one of exactly three allowlisted npm commands, without a shell.

    The commands are literal at their call sites on purpose: nothing from a report, a
    configuration file or the environment can reach an argument list. Any inability to run
    the command is a GateError.
    """
    key = tuple(args)
    options = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": NPM_TIMEOUT_SECONDS,
        "check": False,
    }
    try:
        if key == AUDIT_PROD_COMMAND:
            proc = subprocess.run(["npm", "audit", "--omit=dev", "--audit-level=low", "--json"], **options)
        elif key == AUDIT_DEV_COMMAND:
            proc = subprocess.run(["npm", "audit", "--audit-level=high", "--json"], **options)
        elif key == VERSIONS_COMMAND:
            proc = subprocess.run(["npm", "view", "picomatch", "versions", "--json"], **options)
        else:
            raise GateError("refusing to run a command that is not on the allowlist")
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"{' '.join(key)} timed out after {NPM_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise GateError(f"{' '.join(key)} could not run: {exc.__class__.__name__}") from exc
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def fetch_advisory_http() -> object:
    """GET the GitHub advisory record. Any network, HTTP or parse problem is a GateError."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dcim-frontend-audit-gate"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(ADVISORY_API_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https URL
            if response.status != 200:
                raise GateError(f"advisory API returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except GateError:
        raise
    except urllib.error.HTTPError as exc:
        raise GateError(f"advisory API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateError(f"advisory API unreachable: {exc.__class__.__name__}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise GateError("advisory API returned unparseable data") from exc


# --------------------------------------------------------------------------- audit parsing


def _audit(runner: Runner, frontend_dir: Path, args: Sequence[str]) -> tuple[CommandResult, AuditReport]:
    result = runner(["npm", "audit", *args, "--json"], frontend_dir)
    if not isinstance(result, CommandResult) or not is_int(result.returncode):
        raise GateError("npm audit runner returned an invalid result")
    if result.returncode not in (0, 1):
        raise GateError(f"npm audit exited with unexpected status {result.returncode}")
    return result, parse_audit(result.stdout)


# --------------------------------------------------------------------------- checks


def check_production(runner: Runner, frontend_dir: Path) -> None:
    """`npm audit --omit=dev --audit-level=low` must report zero vulnerabilities."""
    result, report = _audit(runner, frontend_dir, ["--omit=dev", "--audit-level=low"])
    if report.total != 0:
        raise GateError(f"production dependencies report {report.total} vulnerabilities; none are permitted")
    if result.returncode != 0:
        raise GateError("npm audit (production) exited nonzero although the report is empty")


def run_dev_audit(runner: Runner, frontend_dir: Path) -> tuple[AuditReport, Analysis]:
    result, report = _audit(runner, frontend_dir, ["--audit-level=high"])
    analysis = analyze(report)
    if result.returncode == 0 and analysis.gating:
        raise GateError("npm audit exited 0 but the report contains high/critical findings")
    if result.returncode == 1 and not analysis.gating:
        raise GateError("npm audit exited 1 but the report contains no high/critical finding")
    return report, analysis


def parse_version(text: object) -> tuple[int, int, int, str]:
    match = VERSION_RE.match(text) if isinstance(text, str) else None
    if match is None:
        raise GateError(f"registry returned an invalid version {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4) or ""


def check_registry(runner: Runner, frontend_dir: Path) -> None:
    """No picomatch release newer than the last vulnerable version may exist."""
    result = runner(["npm", "view", EXCEPTION_PACKAGE, "versions", "--json"], frontend_dir)
    if result.returncode != 0:
        raise GateError(f"npm registry lookup failed (exit {result.returncode})")
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise GateError("npm registry lookup returned invalid JSON") from exc
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list) or not data:
        raise GateError("npm registry lookup returned no versions")
    parsed = [parse_version(v) for v in data]
    if LAST_VULNERABLE + ("",) not in parsed:
        raise GateError(f"registry version list does not contain {'.'.join(map(str, LAST_VULNERABLE))}; metadata is inconsistent")
    newer = [v for v, p in zip(data, parsed) if p[:3] > LAST_VULNERABLE]
    if newer:
        raise GateError(
            f"picomatch release(s) newer than {'.'.join(map(str, LAST_VULNERABLE))} exist ({', '.join(map(str, newer[:5]))}); "
            "remediate the lockfile and remove the exception"
        )


def check_advisory(fetch: AdvisoryFetcher) -> None:
    """The advisory must still list no patch, the same range and no other package."""
    data = fetch()
    if not isinstance(data, dict) or data.get("ghsa_id") != EXCEPTION_ADVISORY:
        raise GateError("advisory API returned a record for a different advisory")
    if data.get("withdrawn_at") is not None:
        raise GateError("the advisory was withdrawn; remove the exception and re-run the strict gate")
    entries = data.get("vulnerabilities")
    if not isinstance(entries, list) or not entries:
        raise GateError("advisory record lists no vulnerable packages")
    for entry in entries:
        package = entry.get("package") if isinstance(entry, dict) else None
        if not isinstance(package, dict) or package.get("name") != EXCEPTION_PACKAGE or package.get("ecosystem") != "npm":
            raise GateError("advisory scope changed: it now lists a package other than npm picomatch")
        if entry.get("first_patched_version") is not None:
            raise GateError("a patched picomatch version is now listed by the advisory; remediate and remove the exception")
        rng = entry.get("vulnerable_version_range")
        if not isinstance(rng, str) or clause(rng) != EXCEPTION_RANGE:
            raise GateError(f"advisory vulnerable range changed to {rng!r}; re-evaluate the exception")


_STR = r"""(?:"[^"\\\n]*"|'[^'\\\n]*'|`[^`$\\\n]*`)"""
PROHIBITED_RE = re.compile(r"""(['"`])(?:%s)(?:/[^'"`\n]*)?\1""" % "|".join(map(re.escape, PROHIBITED_MODULES)))
GLOB_USE_RE = re.compile(r"import\.meta\.glob\w*")
GLOB_LITERAL_RE = re.compile(
    # The first argument must be a string literal or an array of string literals AND must be
    # followed directly by "," or ")", so `glob("./" + dir)` is not accepted as a literal.
    r"import\.meta\.glob(?:<[^>\n]*>)?\s*\(\s*(?:%s|\[\s*(?:%s(?:\s*,\s*%s)*\s*,?\s*)?\])\s*[,)]" % (_STR, _STR, _STR)
)


def scan_sources(frontend_dir: Path) -> list[str]:
    """Return violations for prohibited glob tooling in application source.

    Limitations (documented, conservative): the scan is textual, not an AST parse. It flags
    any quoted module specifier equal to a prohibited module even inside comments, which can
    only cause a false failure. It cannot see a specifier built at runtime, so such a
    construction is excluded by review and by the zero-production-vulnerability gate.
    """
    violations: list[str] = []
    for name in SCAN_DIRS:
        base = frontend_dir / name
        if base.is_symlink() or not base.is_dir():
            raise GateError(f"cannot verify source imports: {base.as_posix()} is missing or a symlink")
        for current, dirnames, filenames in os.walk(base, followlinks=False):
            for entry in [*dirnames, *filenames]:
                if (Path(current) / entry).is_symlink():
                    raise GateError(f"cannot verify source imports: symlink {(Path(current) / entry).as_posix()}")
            for filename in sorted(filenames):
                path = Path(current) / filename
                if path.suffix.lower() in BINARY_SUFFIXES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise GateError(f"cannot verify source imports: {path.as_posix()} is unreadable as UTF-8 text") from exc
                rel = path.relative_to(frontend_dir).as_posix()
                for match in PROHIBITED_RE.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    violations.append(f"{rel}:{line}: references prohibited module {match.group(0)}")
                for match in GLOB_USE_RE.finditer(text):
                    if not GLOB_LITERAL_RE.match(text, match.start()):
                        line = text.count("\n", 0, match.start()) + 1
                        violations.append(f"{rel}:{line}: import.meta.glob with a non-literal or unsupported pattern")
    return violations


def check_sources(frontend_dir: Path) -> None:
    violations = scan_sources(frontend_dir)
    if violations:
        shown = "; ".join(violations[:5])
        raise GateError(f"prohibited glob tooling in application source ({len(violations)}): {shown}")


# --------------------------------------------------------------------------- exception config


@dataclasses.dataclass(frozen=True)
class ExceptionConfig:
    approved_by: str
    approval_reference: str
    approved_on: dt.datetime
    expires: dt.datetime


def _read_record(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"exception configuration {path.as_posix()} is absent or unreadable") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise GateError("exception configuration is not valid JSON") from exc
    if not isinstance(doc, dict) or set(doc) != set(CONFIG_KEYS):
        raise GateError(f"exception configuration must contain exactly the keys {', '.join(CONFIG_KEYS)}")
    return doc


def _check_approval(doc: dict) -> None:
    if doc["approved"] is not True:
        raise GateError("security exception is NOT APPROVED (approved must be the JSON literal true)")
    if not is_int(doc["schema_version"]) or doc["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise GateError("unsupported exception configuration schema_version")
    if doc["advisory"] != EXCEPTION_ADVISORY:
        raise GateError(f"exception configuration names {doc['advisory']!r}, not {EXCEPTION_ADVISORY}")
    if not isinstance(doc["approved_by"], str) or not doc["approved_by"].strip():
        raise GateError("exception configuration has no approved_by")
    reference = doc["approval_reference"]
    if not isinstance(reference, str) or APPROVAL_REFERENCE_RE.match(reference) is None:
        raise GateError("approval_reference must be a link to the owner's written authorization on this repository")


def _parse_window(doc: dict) -> tuple[dt.datetime, dt.datetime]:
    approved_on, expires_text = doc["approved_on"], doc["expires_utc"]
    if not isinstance(approved_on, str) or DATE_RE.match(approved_on) is None:
        raise GateError("approved_on must be a YYYY-MM-DD date")
    if not isinstance(expires_text, str) or EXPIRY_RE.match(expires_text) is None:
        raise GateError("expires_utc must be a UTC timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        approved = dt.datetime.strptime(approved_on, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        expires = dt.datetime.strptime(expires_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise GateError("approved_on or expires_utc is not a real calendar date/time") from exc
    return approved, expires


def load_config(path: Path, now: dt.datetime) -> ExceptionConfig:
    """Read and strictly validate the owner-approved exception record."""
    if now.tzinfo is None or now.utcoffset() != dt.timedelta(0):
        raise GateError("internal error: the clock must be timezone-aware UTC")
    doc = _read_record(path)
    _check_approval(doc)
    approved, expires = _parse_window(doc)
    if approved > now:
        raise GateError("approved_on is in the future")
    if expires <= approved:
        raise GateError("expires_utc must be after approved_on")
    if expires - approved > dt.timedelta(days=MAX_LIFETIME_DAYS):
        raise GateError(f"the exception may not last longer than {MAX_LIFETIME_DAYS} days")
    if now >= expires:
        raise GateError(f"the security exception expired at {doc['expires_utc']}; remediate instead")
    return ExceptionConfig(doc["approved_by"], doc["approval_reference"], approved, expires)


# --------------------------------------------------------------------------- orchestration


def _attempt(label: str, check: Callable[[], object], failures: list[str]) -> object:
    try:
        return check()
    except GateError as exc:
        failures.append(f"{label}: {exc}")
        return None


def run_gate(
    mode: str,
    frontend_dir: Path,
    config_path: Path,
    now: dt.datetime,
    runner: Runner,
    advisory_fetcher: AdvisoryFetcher,
    out: Callable[[str], None] = print,
) -> int:
    failures: list[str] = []
    if mode not in ("strict", "exception"):
        raise GateError(f"unknown mode {mode!r}")

    if mode == "strict":
        _attempt("production audit", lambda: check_production(runner, frontend_dir), failures)
        outcome = _attempt("frontend audit", lambda: run_dev_audit(runner, frontend_dir), failures)
        if outcome is not None and outcome[1].gating:
            failures.append(
                "frontend audit: high/critical advisories present in " + ", ".join(outcome[1].gating)
                + (" (all attributable to " + EXCEPTION_ADVISORY + "; no exception is active in strict mode)" if outcome[1].exception_only else "")
            )
    else:
        config = _attempt("exception configuration", lambda: load_config(config_path, now), failures)
        _attempt("production audit", lambda: check_production(runner, frontend_dir), failures)
        outcome = _attempt("frontend audit", lambda: run_dev_audit(runner, frontend_dir), failures)
        if outcome is not None and outcome[1].foreign:
            failures.append("frontend audit: high/critical findings not exclusively caused by " + EXCEPTION_ADVISORY + " in " + ", ".join(outcome[1].foreign))
        _attempt("picomatch registry check", lambda: check_registry(runner, frontend_dir), failures)
        _attempt("advisory database check", lambda: check_advisory(advisory_fetcher), failures)
        _attempt("source import guard", lambda: check_sources(frontend_dir), failures)
        if not failures and outcome is not None and config is not None:
            if outcome[1].exception_only:
                out(f"SECURITY EXCEPTION ACTIVE for {EXCEPTION_ADVISORY} until {config.expires:%Y-%m-%dT%H:%M:%SZ} (approval: {config.approval_reference})")
                out("Tolerated nodes: " + ", ".join(outcome[1].gating))
            else:
                out("No high/critical findings; the exception is not needed.")

    if failures:
        for failure in failures:
            out(f"::error::frontend audit gate: {failure}")
        out(f"frontend audit gate: FAILED ({mode} mode)")
        return 1
    out(f"frontend audit gate: passed ({mode} mode)")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = run_command,
    advisory_fetcher: AdvisoryFetcher = fetch_advisory_http,
    now: dt.datetime | None = None,
) -> int:
    """CLI entry point. The keyword seams exist for tests; the CLI never overrides them."""
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Fail-closed frontend dependency audit gate (exception inactive by default).")
    parser.add_argument("--mode", choices=("strict", "exception"), default="strict")
    parser.add_argument("--frontend-dir", type=Path, default=root / "frontend")
    parser.add_argument("--exception-config", type=Path, default=root / DEFAULT_CONFIG_RELATIVE)
    args = parser.parse_args(argv)
    try:
        return run_gate(
            args.mode,
            args.frontend_dir,
            args.exception_config,
            now if now is not None else dt.datetime.now(dt.timezone.utc),
            runner,
            advisory_fetcher,
        )
    except GateError as exc:
        print(f"::error::frontend audit gate: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"::error::frontend audit gate: unexpected {exc.__class__.__name__}; failing closed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
