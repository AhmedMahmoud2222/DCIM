"""Production secret policy (DB-08, Issue #359): one contract for application startup and deployment.

`app.core.config.Settings` applies it when ``ENVIRONMENT=production`` and
``scripts/deploy-docker-compose.sh`` runs this file as a script before any service starts. The module
imports only the standard library so the deploy host needs nothing installed besides ``python3``.

What is rejected (production only):
  * empty or blank values;
  * secrets shipped in the repository's example files (``KNOWN_EXAMPLE_VALUES``);
  * unmistakable placeholders: ``change_me`` / ``changeme`` / ``replace_me`` / ``placeholder`` /
    ``your_secret`` style markers, ``<...>`` / ``${...}`` / ``{{...}}`` template syntax, and a value made
    of one repeated character.

What is deliberately NOT rejected: any other value. There is no entropy, character-class or dictionary
rule beyond the above, so a randomly generated credential is never refused for its alphabet. The
existing minimum length of the JWT key (32) is unchanged and enforced by ``Settings``.

Findings carry the variable NAME and a category only. A secret value is never placed in a finding, an
exception message, a log line or standard output.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# Shipped in .env.example / docs. Compared case-insensitively against the whole, trimmed value.
KNOWN_EXAMPLE_VALUES = frozenset(
    {
        "change_me_at_least_32_characters",  # .env.example JWT_SECRET_KEY
        "change_me_app",  # .env.example DCIM_APP_PASSWORD
        "change_me_superuser",  # .env.example POSTGRES_PASSWORD
    }
)

# Trivially guessable database passwords. Whole-value match only, never a substring.
_COMMON_WEAK_PASSWORDS = frozenset({"password", "postgres", "admin", "secret", "dcim", "dcim_app", "changeit"})

# Substring markers that no generated credential contains by chance (>= 8 significant characters, or a
# word that exists only as a placeholder). `-`, `_`, spaces and dots are folded before matching.
_PLACEHOLDER_MARKERS = re.compile(r"change[-_. ]?me|replace[-_. ]?me|placeholder|your[-_. ]?(?:secret|password)", re.IGNORECASE)
_TEMPLATE_SYNTAX = re.compile(r"^\s*(?:<[^<>]*>|\$\{[^{}]*\}|\{\{[^{}]*\}\})\s*$")

EMPTY = "empty or blank"
KNOWN_EXAMPLE = "known example value from the repository"
PLACEHOLDER = "recognizable placeholder"
WEAK = "known weak password"
INVALID_FERNET = "not a valid Fernet key"
INVALID_JSON = "not a valid JSON object of version labels to Fernet keys"


def classify_secret(value: str | None, *, database_password: bool = False) -> str | None:
    """Return the category a value is rejected under, or None when it is acceptable."""
    if value is None or not value.strip():
        return EMPTY
    stripped = value.strip()
    folded = stripped.lower()
    if folded in KNOWN_EXAMPLE_VALUES:
        return KNOWN_EXAMPLE
    if _PLACEHOLDER_MARKERS.search(stripped) or _TEMPLATE_SYNTAX.match(stripped):
        return PLACEHOLDER
    if len(stripped) >= 8 and len(set(stripped)) == 1:
        return PLACEHOLDER
    if database_password and folded in _COMMON_WEAK_PASSWORDS:
        return WEAK
    return None


def is_valid_fernet_key(key: str) -> bool:
    """The acceptance rule of ``cryptography.fernet.Fernet.__init__``: url-safe base64 of 32 bytes.

    Identical acceptance to the constructor used at encrypt/decrypt time, so a v1 key that has ever
    sealed or opened a row is, by construction, valid here."""
    try:
        return len(base64.urlsafe_b64decode(key.encode())) == 32
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return False


def database_url_password(url: str | None) -> str | None:
    """The decoded password carried by a database URL, or None when it has none.

    Handles percent-encoding (``CHANGE%5FME%5FAPP``), a driver suffix (``postgresql+asyncpg``) and the
    ``?password=`` query form. A password containing unencoded URL delimiters is read as far as the last
    ``@`` of the authority; the policy then also scans that raw userinfo (see ``check_database_url``)."""
    if not url:
        return None
    userinfo = _raw_userinfo(url)
    if userinfo is not None and ":" in userinfo:
        return unquote(userinfo.split(":", 1)[1])
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    values = query.get("password")
    return unquote(values[-1]) if values else None


def _raw_userinfo(url: str) -> str | None:
    rest = url.split("://", 1)[1] if "://" in url else url
    # The authority ends at the first "/" after the LAST "@", so a "/" inside a password is tolerated.
    head, sep, _tail = rest.rpartition("@")
    return head if sep else None


def check_database_url(url: str | None) -> str | None:
    """Category the database URL's password is rejected under, or None. Never returns the password."""
    password = database_url_password(url)
    if password is None or not password.strip():
        return EMPTY
    category = classify_secret(password, database_password=True)
    if category is not None:
        return category
    # Defence in depth for odd encodings: scan the raw (still encoded) userinfo and its decoding too.
    userinfo = _raw_userinfo(url or "") or ""
    for candidate in (userinfo, unquote(userinfo), unquote(unquote(userinfo))):
        if _PLACEHOLDER_MARKERS.search(candidate) or any(known in candidate.lower() for known in KNOWN_EXAMPLE_VALUES):
            return PLACEHOLDER
    return None


def check_fernet_keys(v1: str | None, extra_json: str | None) -> list[tuple[str, str]]:
    """Findings for CREDENTIAL_ENCRYPTION_KEY (v1) and the optional CREDENTIAL_ENCRYPTION_KEYS JSON."""
    findings: list[tuple[str, str]] = []
    if v1 is None or not v1.strip():
        findings.append(("CREDENTIAL_ENCRYPTION_KEY", EMPTY))
    elif not is_valid_fernet_key(v1):
        findings.append(("CREDENTIAL_ENCRYPTION_KEY", INVALID_FERNET))
    raw = (extra_json or "").strip()
    if raw:
        try:
            extra = json.loads(raw)
        except ValueError:
            return [*findings, ("CREDENTIAL_ENCRYPTION_KEYS", INVALID_JSON)]
        if not isinstance(extra, dict) or not extra:
            return [*findings, ("CREDENTIAL_ENCRYPTION_KEYS", INVALID_JSON)]
        for label, key in extra.items():
            if not isinstance(key, str) or not is_valid_fernet_key(key):
                # The version label is not secret and is safe to name; the key never is.
                findings.append((f"CREDENTIAL_ENCRYPTION_KEYS[{_safe_label(label)}]", INVALID_FERNET))
    return findings


def _safe_label(label: object) -> str:
    return label if isinstance(label, str) and re.fullmatch(r"v[1-9][0-9]{0,8}", label) else "?"


def format_findings(findings: list[tuple[str, str]]) -> str:
    return "; ".join(f"{name}: {category}" for name, category in findings)


# --------------------------------------------------------------------------------------------------
# Deployment preflight (run as a script). Reads the EFFECTIVE Compose environment: a variable exported
# in the process environment wins over `.env` in the deploy directory, exactly as Compose interpolation
# and pydantic-settings both resolve it.
# --------------------------------------------------------------------------------------------------

_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if not match:
            continue
        name, raw = match.groups()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        elif " #" in raw:
            raw = raw.split(" #", 1)[0].rstrip()
        values[name] = raw
    return values


def effective_environment(env: Mapping[str, str], dotenv_path: Path | None) -> dict[str, str]:
    merged = read_dotenv(dotenv_path) if dotenv_path else {}
    merged.update({name: value for name, value in env.items() if name in _PREFLIGHT_NAMES})
    return merged


_PREFLIGHT_NAMES = frozenset(
    {"POSTGRES_PASSWORD", "DCIM_APP_PASSWORD", "JWT_SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY", "CREDENTIAL_ENCRYPTION_KEYS"}
)


def preflight_findings(effective: Mapping[str, str]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for name in ("POSTGRES_PASSWORD", "DCIM_APP_PASSWORD"):
        category = classify_secret(effective.get(name), database_password=True)
        if category:
            findings.append((name, category))
    category = classify_secret(effective.get("JWT_SECRET_KEY"))
    if category:
        findings.append(("JWT_SECRET_KEY", category))
    findings.extend(check_fernet_keys(effective.get("CREDENTIAL_ENCRYPTION_KEY"), effective.get("CREDENTIAL_ENCRYPTION_KEYS")))
    return findings


def main(argv: list[str]) -> int:
    dotenv = Path(argv[1]) if len(argv) > 1 else None
    findings = preflight_findings(effective_environment(os.environ, dotenv))
    if findings:
        print(f"Insecure production secrets rejected (values withheld): {format_findings(findings)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
