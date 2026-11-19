"""Validation and root-advisory analysis of `npm audit --json` (report version 2).

Part of the inactive frontend audit gate. SECURITY EXCEPTION NOT APPROVED OR ACTIVE: this module only
parses and classifies; it grants nothing. See .github/security-exceptions/README.md.
"""

from __future__ import annotations

import dataclasses
import json
import re

EXCEPTION_ADVISORY = "GHSA-fc5q-39xc-vjpf"
EXCEPTION_PACKAGE = "picomatch"
EXCEPTION_RANGE = "<=4.0.7"
SEVERITIES = ("info", "low", "moderate", "high", "critical")
GATING = ("high", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
AUDIT_REPORT_VERSION = 2
ADVISORY_URL_RE = re.compile(
    r"^https://github\.com/advisories/(GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4})$"
)


class GateError(Exception):
    """A check failed. The message states why, without leaking secrets."""


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


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_document(text: str) -> dict:
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
    if not is_int(doc.get("auditReportVersion")) or doc["auditReportVersion"] != AUDIT_REPORT_VERSION:
        raise GateError(f"unsupported auditReportVersion {doc.get('auditReportVersion')!r}; expected {AUDIT_REPORT_VERSION}")
    return doc


def _read_counts(doc: dict) -> tuple[dict, dict[str, int]]:
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
        if not is_int(value) or value < 0:
            raise GateError(f"audit metadata count {key!r} is missing or not a non-negative integer")
        counts[key] = value
    if counts["total"] != sum(counts[s] for s in SEVERITIES):
        raise GateError("audit metadata total does not equal the sum of its severities")
    return vulns, counts


def _collect_node(name: str, node: object, advisories: dict[str, Advisory]) -> tuple[set[str], set[str]]:
    """Validate one node. Return (advisory ids it reports itself, names of nodes it points to)."""
    if not isinstance(name, str) or not isinstance(node, dict) or node.get("name") != name:
        raise GateError(f"audit entry {name!r} is malformed or its name does not match its key")
    if node.get("severity") not in SEVERITIES:
        raise GateError(f"audit entry {name!r} has unknown severity {node.get('severity')!r}")
    via = node.get("via")
    if not isinstance(via, list) or not via:
        raise GateError(f"audit entry {name!r} has no resolvable 'via' list")
    ids: set[str] = set()
    targets: set[str] = set()
    for item in via:
        if isinstance(item, str):
            targets.add(item)
        elif isinstance(item, dict):
            advisory = _parse_via_advisory(name, item)
            known = advisories.get(advisory.id)
            if known is not None and known != advisory:
                raise GateError(f"advisory {advisory.id} is reported with inconsistent metadata")
            advisories[advisory.id] = advisory
            ids.add(advisory.id)
        else:
            raise GateError(f"audit entry {name!r} has an unrecognised 'via' element")
    return ids, targets


def _collect_nodes(vulns: dict) -> tuple[dict[str, Advisory], dict[str, set[str]], dict[str, set[str]]]:
    """Validate every node. Return (advisories by id, advisory ids per node, node-to-node links)."""
    advisories: dict[str, Advisory] = {}
    direct_ids: dict[str, set[str]] = {}
    links: dict[str, set[str]] = {}
    for name, node in vulns.items():
        direct_ids[name], links[name] = _collect_node(name, node, advisories)
    for name, targets in links.items():
        for target in targets:
            if target not in vulns:
                raise GateError(f"audit entry {name!r} depends on {target!r}, which is not in the report")
    return advisories, direct_ids, links


def _check_counts(vulns: dict, counts: dict[str, int]) -> None:
    actual = {s: 0 for s in SEVERITIES}
    for node in vulns.values():
        actual[node["severity"]] += 1
    if any(actual[s] != counts[s] for s in SEVERITIES) or counts["total"] != len(vulns):
        raise GateError("audit metadata counts do not match the reported vulnerabilities")


def _resolve_roots(vulns: dict, advisories: dict[str, Advisory], direct_ids: dict, links: dict) -> dict[str, frozenset[str]]:
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
    return {name: frozenset(ids) for name, ids in roots.items()}


def parse_audit(text: str) -> AuditReport:
    """Validate an `npm audit --json` (report version 2) document and resolve root advisories."""
    doc = _load_document(text)
    vulns, counts = _read_counts(doc)
    advisories, direct_ids, links = _collect_nodes(vulns)
    _check_counts(vulns, counts)
    roots = _resolve_roots(vulns, advisories, direct_ids, links)
    return AuditReport(vulns, counts, advisories, roots)


def _parse_via_advisory(owner: str, item: dict) -> Advisory:
    url = item.get("url")
    match = ADVISORY_URL_RE.match(url) if isinstance(url, str) else None
    if match is None:
        raise GateError(f"audit entry {owner!r} references an advisory with an unrecognised url")
    package, severity, rng = item.get("name"), item.get("severity"), item.get("range")
    if not isinstance(package, str) or not package or severity not in SEVERITIES or not isinstance(rng, str) or not rng:
        raise GateError(f"advisory {match.group(1)} (in {owner!r}) is missing name, severity or range")
    return Advisory(match.group(1), package, severity, rng)


def clause(text: str) -> str:
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
        advisory.package != EXCEPTION_PACKAGE or advisory.severity != "high" or clause(advisory.range) != EXCEPTION_RANGE
    ):
        raise GateError(f"{EXCEPTION_ADVISORY} metadata changed (package/severity/range); the exception must be re-evaluated")
    return Analysis(tuple(gating), tuple(foreign))
