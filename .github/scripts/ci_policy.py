"""CI routing policy: map changed paths to the CI worker jobs that must run.

The policy is a pure function. It reads no git state, no environment and no
network. The router (ci_router.py) loads this file from the PR base commit, so a
PR cannot weaken the policy that classifies that same PR.

Levels:
* fast: no worker job applies (documentation only). The route, ci-policy and
  gate jobs still run.
* component: only the worker jobs of the changed components run.
* full: every worker job runs.

Fail-closed rules:
* An empty path list, an unknown path or a high-risk path selects full.
* A path that matches no component rule selects full.

One exception lowers a CI-control change to fast: a pin-only change (see pin_only
below). It applies only when every changed file is a modified, allowed file whose
only differences from the base are the 40-hex commit pins on its action `uses:`
lines (and their version comments). Nothing else about the file may differ.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable

try:  # The route job installs no YAML parser. Without one a workflow is never pin-only.
    import yaml
except ImportError:  # pragma: no cover - exercised by a test that blocks the import
    yaml = None

POLICY_VERSION = 3

FAST = "fast"
COMPONENT = "component"
FULL = "full"
LEVELS = (FAST, COMPONENT, FULL)

# The worker job ids in .github/workflows/ci.yml. The required checks `backend`
# and `frontend` are gate jobs, not workers: they never appear here.
WORKERS = (
    "backend-suite",
    "frontend-suite",
    "backend-runtime-matrix",
    "edge-collector",
    "edge-interop",
    "browser-e2e",
)

# Any of these selects full. Prefix rules end with "/".
HIGH_RISK_PATHS = (
    ".github/",
    "tests/",
    "scripts/",
    "backend/migrations/",
    "backend/scripts/",
    "backend/app/core/",
    "backend/app/db/",
    "backend/app/domain/auth/",
    "backend/app/api/deps.py",
    "backend/app/api/v1/auth.py",
    "backend/app/application/auth_service.py",
    "backend/app/application/rbac.py",
    "backend/app/application/authority_lock.py",
    "backend/app/application/access_control.py",
    "backend/app/application/access_preflight.py",
    "backend/app/main.py",
    "backend/pyproject.toml",
    "backend/alembic.ini",
    "backend/Dockerfile",
    "frontend/Dockerfile",
    "frontend/nginx.conf",
    "frontend/package.json",
    "frontend/package-lock.json",
    "edge_collector/pyproject.toml",
    "edge_collector/interop/requirements.txt",
    "docker-compose.yml",
    "docker-compose.production.yml",
    ".env.example",
)

# A path segment with one of these words selects full, wherever it is. This
# catches security code that a new file adds outside the paths above.
HIGH_RISK_WORDS = re.compile(
    r"auth|secur|rbac|permission|authority|privileg|crypt|secret|credential|"
    r"password|token|session|sandbox|opencode|preflight|retention|audit",
)

# Component rules. First match wins. Each rule names the workers it selects.
COMPONENT_RULES = (
    # The backend contract tests import the Edge scheduler constants.
    ("edge_collector/", ("edge-collector", "edge-interop", "backend-suite")),
    ("backend/", ("backend-suite", "browser-e2e")),
    ("frontend/", ("frontend-suite", "browser-e2e")),
)

DOC_DIRS = ("docs/",)


def _matches(path: str, rule: str) -> bool:
    return path.startswith(rule) if rule.endswith("/") else path == rule


def _is_doc(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in DOC_DIRS):
        return True
    return "/" not in path and path.endswith(".md")


def classify(paths: Iterable[str]) -> dict:
    """Return {"level", "jobs", "reasons"} for a list of repository paths."""
    paths = list(paths)
    reasons: list[str] = []
    jobs: set[str] = set()
    level = FAST
    if not paths:
        return full(["no changed paths: change detection gave no evidence"])
    for path in paths:
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
            return full([f"unparseable path: {path!r}"])
        lowered = path.lower()
        if any(_matches(path, rule) for rule in HIGH_RISK_PATHS):
            return full([f"high-risk path: {path}"])
        if HIGH_RISK_WORDS.search(lowered):
            return full([f"high-risk path word: {path}"])
        if _is_doc(path):
            reasons.append(f"documentation: {path}")
            continue
        for prefix, selected in COMPONENT_RULES:
            if path.startswith(prefix):
                jobs.update(selected)
                level = COMPONENT
                reasons.append(f"component {prefix}: {path}")
                break
        else:
            return full([f"unknown path: {path}"])
    return {
        "level": level,
        "jobs": {job: job in jobs for job in WORKERS},
        "reasons": reasons,
    }


def full(reasons: list[str]) -> dict:
    return {"level": FULL, "jobs": {job: True for job in WORKERS}, "reasons": reasons}


# ---------------------------------------------------------------------------
# Pin-only changes
#
# A Dependabot bump of an action pin changes one 40-hex SHA and a version comment.
# The backend test suites cannot test that: they never run the workflow. The review
# of the new pin against the upstream tag does. A change that is *only* a pin bump
# may therefore route to fast. Everything else under .github/ stays full.
#
# The rule is per file. A line counts as a pin line only when the whole line has
# the exact shape below. Two texts are a pin-only change when they are equal after
# each pin line is reduced to its action reference and "@<pin>".
# ---------------------------------------------------------------------------

_ACTION = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+"
_PIN = r"[0-9a-f]{40}"
# A version comment holds only characters a shell and YAML both leave inert. A pin-shaped line can sit
# inside a `run: |` block, where the comment is script text, so free text there could run.
_VERSION = r"[A-Za-z0-9][A-Za-z0-9._+-]*"
_WORKFLOW_USES = re.compile(rf"^([ \t]*(?:-[ \t]+)?uses:[ \t]+{_ACTION})@{_PIN}(?:[ \t]+#[ \t]*{_VERSION})?$")
_POLICY_JSON = re.compile(rf'^([ \t]*"review_action":[ \t]*"{_ACTION})@{_PIN}"(,?)$')
_ADAPTER = re.compile(rf"^(ACTION = '{_ACTION})@{_PIN}'$")

WORKFLOW_DIR = ".github/workflows/"
# Workflows whose pins stay full: the CI controls themselves, the deployment, the workflow
# that holds REFRESH_PAT (a token with write rights to PR branches), and the workflows that mint
# or use write-capable GitHub App tokens (OpenCode, agent collaboration, Claude comment trigger).
PIN_EXCLUDED = (
    ".github/workflows/agent-collaboration.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/claude.yml",
    ".github/workflows/deploy.yml",
    ".github/workflows/opencode.yml",
    ".github/workflows/refresh-branch.yml",
)
PIN_POLICY_JSON = ".github/acceptance/policy.json"
PIN_ADAPTER = ".github/scripts/orchestration_claude_adapter.py"
PIN_MAX_BYTES = 512 * 1024
# A pin-only file holds only characters that are printable YAML and not line breaks beyond LF:
# tab, LF, printable ASCII, and the Unicode ranges YAML allows minus U+0085, U+2028, U+2029,
# the BOM U+FEFF and the noncharacters U+FFFE and U+FFFF. Normalization drops comments, so a
# line separator in one could hide a step, and a forbidden character in one could break the
# workflow. Anything else sends the file to full.
SAFE_TEXT = re.compile("[\t\n\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufefe\uff00-\ufffd\U00010000-\U0010ffff]*")
REGULAR_FILE_MODE = "100644"


def _pin_rule(path: str):
    if path.startswith(WORKFLOW_DIR) and path.endswith(".yml") and "/" not in path[len(WORKFLOW_DIR):] \
            and path not in PIN_EXCLUDED:
        return _WORKFLOW_USES
    if path == PIN_POLICY_JSON:
        return _POLICY_JSON
    if path == PIN_ADAPTER:
        return _ADAPTER
    return None


def normalize_pins(path: str, text: str) -> str | None:
    """Reduce every pin line of text to its action reference. None: the path is not allowed."""
    rule = _pin_rule(path)
    if rule is None or SAFE_TEXT.fullmatch(text) is None:
        return None
    lines = []
    for line in text.split("\n"):
        match = rule.match(line)
        if match is None:
            lines.append(line)
        elif rule is _POLICY_JSON:
            lines.append(f"{match.group(1)}@<pin>\"{match.group(2)}")
        else:
            lines.append(f"{match.group(1)}@<pin>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Structure check. The line rules above see lines, not YAML. A pin-shaped line can be data: an
# `env` value, a flow mapping, a quoted multi-line string, a `run: |` script. The structure
# check parses both texts, masks the 40-hex pin of every real action reference (a `uses` value
# of a job or of a step) and requires the two parsed structures to be equal. A change in data,
# in a script or in any key then shows up as a difference.
# ---------------------------------------------------------------------------

_USES_VALUE = re.compile(rf"({_ACTION})@{_PIN}")


def _mask(value):
    if isinstance(value, str):
        match = _USES_VALUE.fullmatch(value)
        if match is not None:
            return f"{match.group(1)}@<pin>"
    return value


class _StrictLoader(yaml.SafeLoader if yaml is not None else object):
    """SafeLoader that only composes nodes. It rejects anchors, aliases and explicit tags.

    Nodes are never constructed into Python objects. PyYAML resolves YAML 1.1 scalars (`on` is
    True, `012` is 10) and lets equal keys overwrite each other; GitHub does neither. Comparing
    the raw scalar texts, styles and order closes that parser differential.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML alias")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise ValueError("YAML anchor")
        if getattr(event, "tag", None) is not None:
            raise ValueError("YAML tag")
        return super().compose_node(parent, index)


def _canon(node, masked=frozenset()):
    """A tuple of every raw scalar text, style and order in a node tree. Raise on any doubt."""
    if isinstance(node, yaml.ScalarNode):
        value = _mask(node.value) if node in masked else node.value
        return ("scalar", node.style, value)
    if isinstance(node, yaml.SequenceNode):
        return ("seq", node.flow_style, tuple(_canon(item, masked) for item in node.value))
    if isinstance(node, yaml.MappingNode):
        names = []
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value == "<<":
                raise ValueError("unsupported YAML key")
            names.append(key.value)
        # Raw texts, before any constructor: `on` and `1`, or `Run` and `run`, never collide silently.
        if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
            raise ValueError("duplicate YAML key")
        return ("map", node.flow_style, tuple((_canon(key), _canon(value, masked)) for key, value in node.value))
    raise ValueError("unknown YAML node")


def _mapping_items(node):
    return node.value if isinstance(node, yaml.MappingNode) else []


def _uses_nodes(root) -> set:
    """The scalar nodes of every job-level and step-level `uses` value."""
    jobs = [value for key, value in _mapping_items(root) if getattr(key, "value", None) == "jobs"]
    if len(jobs) != 1 or not isinstance(jobs[0], yaml.MappingNode):
        raise ValueError("no jobs mapping")
    found = set()
    for _, job in jobs[0].value:
        for key, value in _mapping_items(job):
            if key.value == "uses" and isinstance(value, yaml.ScalarNode):
                found.add(value)
            elif key.value == "steps" and isinstance(value, yaml.SequenceNode):
                for step in value.value:
                    for step_key, step_value in _mapping_items(step):
                        if step_key.value == "uses" and isinstance(step_value, yaml.ScalarNode):
                            found.add(step_value)
    return found


def _workflow_structure(text: str):
    if yaml is None:
        return None
    root = yaml.compose(text, Loader=_StrictLoader)  # noqa: S506 - composes nodes only, constructs nothing
    if root is None:
        return None
    return _canon(root, _uses_nodes(root))


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def _json_structure(text: str):
    data = json.loads(text, object_pairs_hook=_no_duplicates)
    if not isinstance(data, dict) or "review_action" not in data:
        return None
    data["review_action"] = _mask(data["review_action"])
    return data


def _adapter_structure(text: str):
    tree = ast.parse(text)
    masked = 0
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "ACTION" \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            node.value.value = _mask(node.value.value)
            masked += 1
    return ast.dump(tree) if masked == 1 else None


def structure(path: str, text: str):
    """The parsed structure of text with action pins masked, or None when it cannot be trusted."""
    try:
        if path == PIN_POLICY_JSON:
            return _json_structure(text)
        if path == PIN_ADAPTER:
            return _adapter_structure(text)
        return _workflow_structure(text)
    except Exception:  # noqa: BLE001 - any doubt means the file is not pin-only
        return None


def parse_raw_diff(output: str) -> list[dict]:
    """Parse `git diff --raw --no-renames -z` into [{path, status, old_mode, new_mode}].

    Raise ValueError on any doubt, so a caller treats the change as not pin-only.
    """
    tokens = output.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    if len(tokens) % 2:
        raise ValueError("raw diff has an odd token count")
    entries = []
    for meta, path in zip(tokens[0::2], tokens[1::2]):
        fields = meta.split(" ")
        if len(fields) != 5 or not fields[0].startswith(":") or not path:
            raise ValueError(f"unparseable raw diff entry: {meta!r}")
        entries.append({"path": path, "status": fields[4], "old_mode": fields[0][1:], "new_mode": fields[1]})
    return entries


def pin_only(changes: Iterable[dict]) -> dict | None:
    """Return a fast decision when every change is a pin-only change, else None.

    changes: dicts with path, status, old_mode, new_mode, old, new (the file texts).
    """
    changes = list(changes)
    if not changes or len(changes) > 20:
        return None
    paths = []
    for change in changes:
        path = change.get("path")
        old, new = change.get("old"), change.get("new")
        if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
            return None
        if change.get("status") != "M" or change.get("old_mode") != REGULAR_FILE_MODE \
                or change.get("new_mode") != REGULAR_FILE_MODE:
            return None
        if len(old) > PIN_MAX_BYTES or len(new) > PIN_MAX_BYTES or old == new:
            return None
        before, after = normalize_pins(path, old), normalize_pins(path, new)
        if before is None or before != after:
            return None
        before_structure = structure(path, old)
        if before_structure is None or before_structure != structure(path, new):
            return None
        paths.append(path)
    return {
        "level": FAST,
        "jobs": {job: False for job in WORKERS},
        "reasons": [f"pin-only change: {path}" for path in sorted(paths)],
        "paths": sorted(paths),
    }
