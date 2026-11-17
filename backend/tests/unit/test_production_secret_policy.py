"""DB-08 (Issue #359): production configuration must reject example, empty and placeholder secrets.

No test needs a real secret: every "valid" value is generated here and every "bad" value is either a
public example from `.env.example` or an obvious placeholder.
"""

import os
import secrets
import subprocess  # nosec B404 - test-only: runs this repo's own interpreter with fixed code
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core import secret_policy
from app.core.config import Settings, _database_password_category

BACKEND = Path(__file__).resolve().parents[2]
JWT_EXAMPLE = "CHANGE_ME_AT_LEAST_32_CHARACTERS"
APP_PW_EXAMPLE = "CHANGE_ME_APP"
GOOD_KEY = Fernet.generate_key().decode()


def _good_jwt() -> str:
    return secrets.token_urlsafe(48)


def _good_url(password: str | None = None) -> str:
    return f"postgresql+asyncpg://dcim_app:{quote(password or secrets.token_urlsafe(36), safe='')}@postgres:5432/dcim"


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "production",
        "database_url": _good_url(),
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret_key": _good_jwt(),
        "credential_encryption_key": GOOD_KEY,
    }
    return Settings(**{**values, **overrides})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ENVIRONMENT", "JWT_SECRET_KEY", "DATABASE_URL", "CREDENTIAL_ENCRYPTION_KEY", "CREDENTIAL_ENCRYPTION_KEYS"):
        monkeypatch.delenv(name, raising=False)


# 1. production rejects the known JWT example --------------------------------------------------------------
@pytest.mark.parametrize("value", [JWT_EXAMPLE, JWT_EXAMPLE.lower(), f"  {JWT_EXAMPLE}  ", " " * 40])
def test_production_rejects_jwt_example_and_blank(value):
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        _settings(jwt_secret_key=value)


@pytest.mark.parametrize(
    "value",
    ["changeme" * 5, "please-change-me-before-going-live-now", "REPLACE_ME_with_a_real_signing_key_x", "<your-signing-key-goes-here-32ch>", "${JWT_SECRET_KEY_FROM_VAULT_32CHARS}", "a" * 40],
)
def test_production_rejects_recognizable_placeholders(value):
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        _settings(jwt_secret_key=value)


def test_empty_jwt_still_fails_everywhere():
    with pytest.raises(ValidationError):
        _settings(jwt_secret_key="")
    with pytest.raises(ValidationError):
        _settings(environment="development", jwt_secret_key="")


# 2. production rejects placeholder database credentials ---------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+asyncpg://dcim_app:{APP_PW_EXAMPLE}@postgres:5432/dcim",
        "postgresql+asyncpg://dcim_app:CHANGE%5FME%5FAPP@postgres:5432/dcim",  # percent-encoded
        "postgresql+asyncpg://dcim_app:change_me_app@postgres:5432/dcim",
        "postgresql+asyncpg://dcim_app:CHANGE_ME_SUPERUSER@postgres:5432/dcim",
        "postgresql+psycopg://dcim_app:CHANGE_ME_APP@postgres/dcim",
        "postgresql+asyncpg://dcim_app:@postgres:5432/dcim",  # empty password
        "postgresql+asyncpg://dcim_app@postgres:5432/dcim",  # no password at all
        "postgresql+asyncpg://dcim_app:postgres@postgres:5432/dcim",
        "postgresql+asyncpg://dcim_app@postgres:5432/dcim?password=CHANGE_ME_APP",
        # '@' in the query or path must not move the credentials (AGY P2, PR #362)
        "postgresql+asyncpg://dcim_app:postgres@postgres:5432/dcim?x=@y",
        "postgresql+asyncpg://dcim_app@postgres:5432/dcim?x=@y",
        "postgresql+psycopg://dcim_app:postgres@postgres:5432/db@name",
        "postgresql+asyncpg://dcim_app:postgres@postgres:5432/dcim#frag@ment",
        "postgresql+asyncpg://dcim_app:%70ostgres@postgres:5432/dcim?x=@y",  # percent-encoded 'postgres'
        "postgresql+asyncpg://dcim_app:good-password-1234@postgres:5432/dcim?password=postgres",  # query wins
    ],
)
def test_production_rejects_placeholder_database_password(url):
    with pytest.raises(ValidationError, match="DATABASE_URL password"):
        _settings(database_url=url)


def test_database_url_without_password_is_rejected_whatever_follows_the_authority():
    for url in [
        "postgresql+asyncpg://dcim_app@postgres:5432/dcim",
        "postgresql+asyncpg://dcim_app@postgres:5432/dcim?x=@y",
        "postgresql+psycopg://dcim_app@postgres/db@name",
    ]:
        assert _database_password_category(url) is not None, url


def test_policy_and_engine_agree_on_ordinary_urls():
    from sqlalchemy.engine import make_url

    for password in ["Pa@ss/w:rd#1?&=+%", secrets.token_urlsafe(30), "ünï-" + secrets.token_hex(6)]:
        for driver in ("postgresql+asyncpg", "postgresql+psycopg"):
            url = f"{driver}://dcim_app:{quote(password, safe='')}@postgres:5432/dcim?application_name=a@b"
            assert make_url(url).password == password
            assert _database_password_category(url) is None, url


@pytest.mark.parametrize("driver", ["postgresql+asyncpg", "postgresql+psycopg"])
def test_both_driver_forms_accept_a_generated_password_with_at_signs_elsewhere(driver):
    password = secrets.token_urlsafe(30)
    assert _settings(database_url=f"{driver}://dcim_app:{password}@postgres:5432/dcim?application_name=a@b").is_production


# 3. valid generated secrets are accepted ------------------------------------------------------------------
def test_generated_secrets_are_accepted():
    for _ in range(25):
        settings = _settings()
        assert settings.is_production


@pytest.mark.parametrize("password", ["p@ss/w:rd#1?&=+%x" + secrets.token_hex(8), "Ã¼nÃ¯code-" + secrets.token_hex(8), "a-b_c.d~" + secrets.token_urlsafe(24)])
def test_unusual_but_legitimate_passwords_are_accepted(password):
    assert _settings(database_url=_good_url(password)).is_production


# 4. development and test keep their behaviour -------------------------------------------------------------
@pytest.mark.parametrize("environment", ["development", "test"])
def test_non_production_environments_accept_examples(environment):
    settings = _settings(
        environment=environment,
        jwt_secret_key=JWT_EXAMPLE,
        database_url=f"postgresql+asyncpg://dcim_app:{APP_PW_EXAMPLE}@postgres:5432/dcim",
        credential_encryption_key="x" * 40,  # historical v1 min_length-only behaviour is unchanged
    )
    assert not settings.is_production


# 5. environment / .env override precedence ----------------------------------------------------------------
def test_environment_variable_overrides_dotenv_in_both_directions(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    base = {"REDIS_URL": "redis://localhost:6379/0", "CREDENTIAL_ENCRYPTION_KEY": GOOD_KEY, "ENVIRONMENT": "production"}
    good_url = _good_url()

    def write(jwt):
        lines = {**base, "JWT_SECRET_KEY": jwt, "DATABASE_URL": good_url}
        dotenv.write_text("".join(f"{k}={v}\n" for k, v in lines.items()), encoding="utf-8")

    # placeholder in .env, good value in the process environment: the environment wins and it is accepted
    write(JWT_EXAMPLE)
    monkeypatch.setenv("JWT_SECRET_KEY", _good_jwt())
    assert Settings(_env_file=dotenv).is_production
    # good value in .env, placeholder in the process environment: the environment wins and it is rejected
    write(_good_jwt())
    monkeypatch.setenv("JWT_SECRET_KEY", JWT_EXAMPLE)
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        Settings(_env_file=dotenv)
    # placeholder only in .env, nothing in the environment: rejected
    monkeypatch.delenv("JWT_SECRET_KEY")
    write(JWT_EXAMPLE)
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        Settings(_env_file=dotenv)


def test_database_url_environment_overrides_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"ENVIRONMENT=production\nREDIS_URL=redis://h/0\nJWT_SECRET_KEY={_good_jwt()}\nCREDENTIAL_ENCRYPTION_KEY={GOOD_KEY}\nDATABASE_URL={_good_url()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://dcim_app:{APP_PW_EXAMPLE}@postgres:5432/dcim")
    with pytest.raises(ValidationError, match="DATABASE_URL password"):
        Settings(_env_file=dotenv)


# 6. API, worker and migration entrypoints enforce the same policy ---------------------------------------------
_ENTRYPOINT_CODE = {"app.main": "import app.main", "app.infrastructure.celery_app": "import app.infrastructure.celery_app"}


def _run_import(module: str, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"JWT_SECRET_KEY", "DATABASE_URL", "ENVIRONMENT", "CREDENTIAL_ENCRYPTION_KEY"}}
    env.update(
        ENVIRONMENT="production",
        DATABASE_URL=_good_url(),
        REDIS_URL="redis://localhost:6379/0",
        JWT_SECRET_KEY=_good_jwt(),
        CREDENTIAL_ENCRYPTION_KEY=GOOD_KEY,
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.update(env_overrides)
    code = _ENTRYPOINT_CODE[module]  # fixed literals only: no external input reaches the interpreter
    return subprocess.run(  # nosec B603  # nosemgrep
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120, shell=False
    )


@pytest.mark.parametrize("module", ["app.main", "app.infrastructure.celery_app"])
@pytest.mark.parametrize(
    ("variable", "value"),
    [("JWT_SECRET_KEY", JWT_EXAMPLE), ("DATABASE_URL", f"postgresql+asyncpg://dcim_app:{APP_PW_EXAMPLE}@postgres:5432/dcim")],
)
def test_api_and_worker_entrypoints_refuse_placeholders_without_leaking(module, variable, value):
    result = _run_import(module, **{variable: value})
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Insecure configuration rejected" in output
    assert value not in output and APP_PW_EXAMPLE not in output


@pytest.mark.parametrize("module", ["app.main", "app.infrastructure.celery_app"])
def test_entrypoints_start_with_generated_secrets(module):
    result = _run_import(module)
    assert result.returncode == 0, result.stderr[-2000:]


def test_alembic_env_uses_the_same_settings():
    source = (BACKEND / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "get_settings()" in source


# 7. errors and logs never reveal secret values -----------------------------------------------------------
def test_validation_errors_name_variables_but_never_values():
    jwt = f"{JWT_EXAMPLE}"
    password = "Zq8Lm" + secrets.token_hex(6) + "CHANGE_ME_X"
    url = f"postgresql+asyncpg://dcim_app:{password}@postgres:5432/dcim"
    with pytest.raises(ValidationError) as caught:
        _settings(jwt_secret_key=jwt, database_url=url, credential_encryption_key="not-a-fernet-key-" + "z" * 20)
    # str() is what a traceback or log prints (hide_input_in_errors). errors() is only safe without the input.
    text = str(caught.value) + repr(caught.value.errors(include_input=False))
    for name in ("JWT_SECRET_KEY", "DATABASE_URL password", "CREDENTIAL_ENCRYPTION_KEY"):
        assert name in text
    for secret in (jwt, password, "not-a-fernet-key", url):
        assert secret not in text


def test_policy_findings_never_contain_values():
    marker = "Ab1" + secrets.token_hex(8) + "change_me"
    findings = secret_policy.preflight_findings(
        {"POSTGRES_PASSWORD": marker, "DCIM_APP_PASSWORD": marker, "JWT_SECRET_KEY": marker, "CREDENTIAL_ENCRYPTION_KEY": marker}
    )
    assert len(findings) == 4
    assert marker not in secret_policy.format_findings(findings)


# 9. credential-encryption keys: fail closed in production, nothing historical is invalidated -----------------
@pytest.mark.parametrize("bad", ["x" * 44, "short", "A" * 43, "not base64 !!" + "=" * 31])
def test_production_rejects_invalid_v1_fernet_key(bad):
    with pytest.raises(ValidationError, match="CREDENTIAL_ENCRYPTION_KEY"):
        _settings(credential_encryption_key=bad if len(bad) >= 32 else bad.ljust(32, "x"))


def test_production_accepts_valid_v1_and_additional_versions():
    extra = f'{{"v2": "{Fernet.generate_key().decode()}"}}'
    settings = _settings(credential_encryption_keys=extra)
    assert sorted(settings.credential_encryption_key_versions()) == [1, 2]


def test_policy_fernet_rule_matches_cryptography_exactly():
    samples = [Fernet.generate_key().decode() for _ in range(10)] + ["A" * 43 + "=", "A" * 44, "x" * 44, "short", "", "Ã©" * 44, "A" * 42 + "==", "-_" * 22]
    for sample in samples:
        try:
            Fernet(sample.encode())
            expected = True
        except (ValueError, TypeError):  # UnicodeEncodeError is a ValueError subclass
            expected = False
        assert secret_policy.is_valid_fernet_key(sample) is expected, sample


def test_existing_valid_v1_ciphertext_still_round_trips_under_production_settings(monkeypatch):
    from app.core.secrets import SettingsKeyProvider, decrypt_secret, encrypt_secret

    legacy_key = Fernet.generate_key().decode()
    legacy_ciphertext = Fernet(legacy_key.encode()).encrypt(b"snmp-community").decode()  # unprefixed, pre-versioning format
    provider = SettingsKeyProvider(_settings(credential_encryption_key=legacy_key))
    assert decrypt_secret(legacy_ciphertext, provider=provider) == "snmp-community"
    assert decrypt_secret(encrypt_secret("api-key", provider=provider), provider=provider) == "api-key"


# 10. deployment preflight shares the policy ---------------------------------------------------------------------
def test_preflight_environment_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"JWT_SECRET_KEY={JWT_EXAMPLE}\nDCIM_APP_PASSWORD='{APP_PW_EXAMPLE}'\n# POSTGRES_PASSWORD=ignored\n", encoding="utf-8")
    merged = secret_policy.effective_environment({"JWT_SECRET_KEY": _good_jwt()}, dotenv)
    assert merged["DCIM_APP_PASSWORD"] == APP_PW_EXAMPLE
    assert secret_policy.classify_secret(merged["JWT_SECRET_KEY"]) is None
    assert "POSTGRES_PASSWORD" not in merged


# 11. `.env` parsing agrees with Compose for quoted values and comments (AGY P2, PR #362) ---------------------
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('POSTGRES_PASSWORD="postgres" # comment', "postgres"),
        ("POSTGRES_PASSWORD='postgres' # comment", "postgres"),
        ('POSTGRES_PASSWORD="postgres"', "postgres"),
        ("POSTGRES_PASSWORD='postgres'", "postgres"),
        ("POSTGRES_PASSWORD=postgres # comment", "postgres"),
        ("POSTGRES_PASSWORD=postgres", "postgres"),
        ('POSTGRES_PASSWORD="a # b"', "a # b"),  # '#' inside quotes is data
        ("POSTGRES_PASSWORD=a#b", "a#b"),  # '#' not preceded by whitespace is data
        ('POSTGRES_PASSWORD="p\\"q"', 'p"q'),
        ("export POSTGRES_PASSWORD='x y'   # c", "x y"),
        ('POSTGRES_PASSWORD="unterminated', '"unterminated'),
    ],
)
def test_dotenv_value_parsing(tmp_path, line, expected):
    dotenv = tmp_path / ".env"
    dotenv.write_text(line + "\n", encoding="utf-8")
    assert secret_policy.read_dotenv(dotenv)["POSTGRES_PASSWORD"] == expected


@pytest.mark.parametrize("line", ['POSTGRES_PASSWORD="postgres" # comment', "POSTGRES_PASSWORD='postgres' # c", 'POSTGRES_PASSWORD="CHANGE_ME_SUPERUSER" # c'])
def test_quoted_weak_dotenv_password_is_rejected(tmp_path, line):
    dotenv = tmp_path / ".env"
    dotenv.write_text(line + "\n", encoding="utf-8")
    findings = secret_policy.preflight_findings(secret_policy.effective_environment({}, dotenv))
    assert any(name == "POSTGRES_PASSWORD" for name, _ in findings)


@pytest.mark.parametrize("quote", ["'", '"', ""])
def test_valid_quoted_fernet_key_with_comment_is_accepted(tmp_path, quote):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"CREDENTIAL_ENCRYPTION_KEY={quote}{GOOD_KEY}{quote} # shared key\n", encoding="utf-8")
    effective = secret_policy.effective_environment({}, dotenv)
    assert effective["CREDENTIAL_ENCRYPTION_KEY"] == GOOD_KEY
    assert not secret_policy.check_fernet_keys(effective["CREDENTIAL_ENCRYPTION_KEY"], None)


def test_environment_overrides_quoted_dotenv_value(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text('POSTGRES_PASSWORD="postgres" # c\n', encoding="utf-8")
    good = secrets.token_urlsafe(30)
    effective = secret_policy.effective_environment({"POSTGRES_PASSWORD": good}, dotenv)
    assert effective["POSTGRES_PASSWORD"] == good
    assert not [f for f in secret_policy.preflight_findings(effective) if f[0] == "POSTGRES_PASSWORD"]
