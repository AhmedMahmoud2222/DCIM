#!/usr/bin/env python3
"""Fail-closed frontend dependency audit gate with an optional, inactive picomatch exception.

STATUS: SECURITY EXCEPTION NOT APPROVED OR ACTIVE. Nothing in .github/workflows calls this
file. The enforced CI gate is still `npm audit --audit-level=high` in the frontend-suite job
and it still fails while GHSA-fc5q-39xc-vjpf (picomatch <= 4.0.7, no patched release) is
reported. See .github/security-exceptions/README.md for the threat model and for the exact,
owner-authorized change that would activate this mechanism.

Two modes, chosen with --mode:

  strict (default)  Behaves like the CI gate it would replace: it fails on any high or critical
                    advisory. It also requires zero production vulnerabilities. It never reads
                    the exception configuration, so it cannot grant an exception.
  exception         Additionally tolerates high findings that are caused EXCLUSIVELY by the
                    one advisory id hard-coded below, and only while every condition holds:
                      1. the owner-approved configuration is valid and unexpired (UTC),
                      2. production dependencies report zero vulnerabilities,
                      3. no high/critical advisory other than the exception appears anywhere
                         in a dependency chain,
                      4. no patched or newer picomatch exists (npm registry and GitHub
                         advisory database both consulted),
                      5. no application source under frontend/src or frontend/e2e uses the
                         vulnerable glob tooling.

Every check fails closed. Unknown or inconsistent input is a failure, never a pass. The advisory
id is a constant in this file, so a configuration file cannot widen the exception to another
advisory, package, severity or dependency family.

Exit status: 0 only when the policy is satisfied; 1 for every other outcome, including
unexpected exceptions. (argparse usage errors exit 2, which is also nonzero.)
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

# --------------------------------------------------------------------------- constants

EXCEPTION_ADVISORY = "GHSA-fc5q-39xc-vjpf"
EXCEPTION_PACKAGE = "picomatch"
EXCEPTION_RANGE = "<=4.0.7"
LAST_VULNERABLE = (4, 0, 7)
ADVISORY_API_URL = "https://api.github.com/advisories/" + EXCEPTION_ADVISORY

SEVERITIES = ("info", "low", "moderate", "high", "critical")
GATING = ("high", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

AUDIT_REPORT_VERSION = 2
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
ADVISORY_URL_RE = re.compile(
    r"^https://github\.com/advisories/(GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4})$"
)
VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

PROHIBITED_MODULES = ("picomatch", "minimatch", "tinyglobby", "fdir")
SCAN_DIRS = ("src", "e2e")
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf", ".mp4", ".webm", ".zip", ".gz"}
)

NPM_TIMEOUT_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 20
DEFAULT_CONFIG_RELATIVE = ".github/security-exceptions/frontend-audit-picomatch.json"


class GateError(Exception):
    """A check failed. The message states why, without leaking secrets."""


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


Runner = Callable[[Sequence[str], Path], CommandResult]
AdvisoryFetcher = Callable[[], object]


# --------------------------------------------------------------------------- IO seams


def run_command(args: Sequence[str], cwd: Path) -> CommandResult:
    """Run a command without a shell. Any inability to run it is a GateError."""
    exe = shutil.which(args[0])
    if exe is None:
        raise GateError(f"cannot find {args[0]!r} on PATH")
    try:
        proc = subprocess.run(
            [exe, *args[1:]],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NPM_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"{' '.join(args)} timed out after {NPM_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise GateError(f"{' '.join(args)} could not run: {exc.__class__.__name__}") from exc
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


@dataclasses.dataclass(frozen=True)
class Advisory:
    id: str
    package: str
    severity: str
    range: str


@dataclasses.dataclass
class AuditReport:
    nodes: dict[str, dict]
    counts: dict[str, int]
    advisories: dict[str, Advisory]
    roots: dict[str, frozenset[str]]

    def gating_nodes(self) -> list[str]:
        return sorted(name for name, node in self.nodes.items() if node["severity"] in GATING)

    @property
    def total(self) -> int:
        return len(self.nodes)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_audit(text: str) -> AuditReport:
    """Validate an `npm audit --json` (report version 2) document and resolve root advisories."""
    if not isinstance(text, str) or not text.strip():
        raise GateError("npm audit produced no output")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise GateError("npm audit output is not valid JSON (truncated or corrupt)") from exc
    if not isinstance(doc, dict):
        raise GateError("npm audit output is not a JSON object")
    if "error" in doc:
        err = doc["error"]
        detail = ""
        if isinstance(err, dict):
            detail = f" ({str(err.get('code', ''))[:40]}: {str(err.get('summary', ''))[:120]})"
        raise GateError("npm audit reported an error instead of a report" + detail)
    if doc.get("auditReportVersion") != AUDIT_REPORT_VERSION or not _is_int(doc.get("auditReportVersion")):
        raise GateError(f"unsupported auditReportVersion {doc.get('auditReportVersion')!r}; expected {AUDIT_REPORT_VERSION}")
    vulns = doc.get("vulnerabilities")
    metadata = doc.get("metadata")
    if not isinstance(vulns, dict):
        raise GateError("audit report is missing 'vulnerabilities'")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("vulnerabilities"), dict):
        raise GateError("audit report is missing 'metadata.vulnerabilities'")
    meta = metadata["vulnerabilities"]
    counts: dict[str, int] = {}
    for key in (*SEVERITIES, "total"):
        value = meta.get(key)
        if not _is_int(value) or value < 0:
            raise GateError(f"audit metadata count {key!r} is missing or not a non-negative integer")
        counts[key] = value
    if counts["total"] != sum(counts[s] for s in SEVERITIES):
        raise GateError("audit metadata total does not equal the sum of its severities")

    advisories: dict[str, Advisory] = {}
    direct_ids: dict[str, set[str]] = {}
    links: dict[str, set[str]] = {}
    actual = {s: 0 for s in SEVERITIES}
    for name, node in vulns.items():
        if not isinstance(name, str) or not isinstance(node, dict) or node.get("name") != name:
            raise GateError(f"audit entry {name!r} is malformed or its name does not match its key")
        severity = node.get("severity")
        if severity not in SEVERITIES:
            raise GateError(f"audit entry {name!r} has unknown severity {severity!r}")
        actual[severity] += 1
        via = node.get("via")
        if not isinstance(via, list) or not via:
            raise GateError(f"audit entry {name!r} has no resolvable 'via' list")
        direct_ids[name] = set()
        links[name] = set()
        for item in via:
            if isinstance(item, str):
                links[name].add(item)
            elif isinstance(item, dict):
                advisory = _parse_via_advisory(name, item)
                known = advisories.get(advisory.id)
                if known is not None and known != advisory:
                    raise GateError(f"advisory {advisory.id} is reported with inconsistent metadata")
                advisories[advisory.id] = advisory
                direct_ids[name].add(advisory.id)
            else:
                raise GateError(f"audit entry {name!r} has an unrecognised 'via' element")
    for name, targets in links.items():
        for target in targets:
            if target not in vulns:
                raise GateError(f"audit entry {name!r} depends on {target!r}, which is not in the report")
    if any(actual[s] != counts[s] for s in SEVERITIES) or counts["total"] != len(vulns):
        raise GateError("audit metadata counts do not match the reported vulnerabilities")

    roots = {name: set(ids) for name, ids in direct_ids.items()}
    changed = True
    while changed:  # fixpoint, so dependency cycles terminate
        changed = False
        for name, targets in links.items():
            for target in targets:
                missing = roots[target] - roots[name]
                if missing:
                    roots[name] |= missing
                    changed = True
    for name, ids in roots.items():
        if not ids:
            raise GateError(f"cannot establish the root advisory for {name!r}")
        top = max(SEVERITY_RANK[advisories[i].severity] for i in ids)
        # npm may rate a dependent LOWER than its worst root advisory (observed in real reports),
        # but a node can never be rated higher than every advisory behind it.
        if SEVERITY_RANK[vulns[name]["severity"]] > top:
            raise GateError(f"severity of {name!r} is higher than any of its root advisories")
    return AuditReport(vulns, counts, advisories, {n: frozenset(i) for n, i in roots.items()})


def _parse_via_advisory(owner: str, item: dict) -> Advisory:
    url = item.get("url")
    match = ADVISORY_URL_RE.match(url) if isinstance(url, str) else None
    if match is None:
        raise GateError(f"audit entry {owner!r} references an advisory with an unrecognised url")
    package, severity, rng = item.get("name"), item.get("severity"), item.get("range")
    if not isinstance(package, str) or not package or severity not in SEVERITIES or not isinstance(rng, str) or not rng:
        raise GateError(f"advisory {match.group(1)} (in {owner!r}) is missing name, severity or range")
    return Advisory(match.group(1), package, severity, rng)


def _clause(text: str) -> str:
    return text.replace(" ", "")


@dataclasses.dataclass(frozen=True)
class Analysis:
    gating: tuple[str, ...]
    foreign: tuple[str, ...]  # gating nodes that are NOT exclusively the exception advisory

    @property
    def exception_only(self) -> bool:
        return bool(self.gating) and not self.foreign


def analyze(report: AuditReport) -> Analysis:
    """Classify every high/critical node by the high/critical advisories behind it."""
    gating = report.gating_nodes()
    foreign: list[str] = []
    for name in gating:
        high_roots = {i for i in report.roots[name] if report.advisories[i].severity in GATING}
        if high_roots != {EXCEPTION_ADVISORY}:
            foreign.append(name)
    advisory = report.advisories.get(EXCEPTION_ADVISORY)
    if advisory is not None and (
        advisory.package != EXCEPTION_PACKAGE or advisory.severity != "high" or _clause(advisory.range) != EXCEPTION_RANGE
    ):
        raise GateError(f"{EXCEPTION_ADVISORY} metadata changed (package/severity/range); the exception must be re-evaluated")
    return Analysis(tuple(gating), tuple(foreign))


def _audit(runner: Runner, frontend_dir: Path, args: Sequence[str]) -> tuple[CommandResult, AuditReport]:
    result = runner(["npm", "audit", *args, "--json"], frontend_dir)
    if not isinstance(result, CommandResult) or not _is_int(result.returncode):
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
        if not isinstance(rng, str) or _clause(rng) != EXCEPTION_RANGE:
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


def load_config(path: Path, now: dt.datetime) -> ExceptionConfig:
    """Read and strictly validate the owner-approved exception record."""
    if now.tzinfo is None or now.utcoffset() != dt.timedelta(0):
        raise GateError("internal error: the clock must be timezone-aware UTC")
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
    if doc["approved"] is not True:
        raise GateError("security exception is NOT APPROVED (approved must be the JSON literal true)")
    if not _is_int(doc["schema_version"]) or doc["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise GateError("unsupported exception configuration schema_version")
    if doc["advisory"] != EXCEPTION_ADVISORY:
        raise GateError(f"exception configuration names {doc['advisory']!r}, not {EXCEPTION_ADVISORY}")
    approver = doc["approved_by"]
    if not isinstance(approver, str) or not approver.strip():
        raise GateError("exception configuration has no approved_by")
    reference = doc["approval_reference"]
    if not isinstance(reference, str) or APPROVAL_REFERENCE_RE.match(reference) is None:
        raise GateError("approval_reference must be a link to the owner's written authorization on this repository")
    approved_on = doc["approved_on"]
    expires_text = doc["expires_utc"]
    if not isinstance(approved_on, str) or DATE_RE.match(approved_on) is None:
        raise GateError("approved_on must be a YYYY-MM-DD date")
    if not isinstance(expires_text, str) or EXPIRY_RE.match(expires_text) is None:
        raise GateError("expires_utc must be a UTC timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        approved = dt.datetime.strptime(approved_on, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        expires = dt.datetime.strptime(expires_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise GateError("approved_on or expires_utc is not a real calendar date/time") from exc
    if approved > now:
        raise GateError("approved_on is in the future")
    if expires <= approved:
        raise GateError("expires_utc must be after approved_on")
    if expires - approved > dt.timedelta(days=MAX_LIFETIME_DAYS):
        raise GateError(f"the exception may not last longer than {MAX_LIFETIME_DAYS} days")
    if now >= expires:
        raise GateError(f"the security exception expired at {expires_text}; remediate instead")
    return ExceptionConfig(approver, reference, approved, expires)


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
