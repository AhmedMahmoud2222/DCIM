"""SNMPv3 authPriv credential handling through the integration API: write-only secrets,
explicit rejection of weak/unsupported combinations, and unchanged v2c behaviour."""

import io
import json

import pytest
import structlog
from sqlalchemy import text

from app.core import errors as error_module
from app.core.secrets import decrypt_secret
from tests.api._phase8_helpers import create_integration

AUTH_SECRET = "synthetic-auth-secret-SEC04"
PRIV_SECRET = "synthetic-priv-secret-SEC04"
SECRETS = (AUTH_SECRET, PRIV_SECRET)


def v3_body(**changes) -> dict:
    body = {
        "username": "dcim-poller", "auth_protocol": "sha256", "auth_secret": AUTH_SECRET,
        "priv_protocol": "aes128", "priv_secret": PRIV_SECRET,
    }
    return body | changes


async def create_v3(client, headers, **changes) -> dict:
    return await create_integration(client, headers, integration_type="snmp", snmpv3=v3_body(**changes))


def assert_no_secrets(*texts: str) -> None:
    for blob in texts:
        for secret in SECRETS:
            assert secret not in blob


async def test_valid_auth_priv_integration_is_created_with_write_only_secrets(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    created = await create_v3(client, headers)
    assert created["credential_kind"] == "snmpv3" and created["has_credential"] is True
    assert created["config"]["version"] == "v3"
    assert created["config"]["snmpv3"] == {
        "security_level": "authPriv", "username": "dcim-poller", "auth_protocol": "sha256",
        "priv_protocol": "aes128", "context_name": "",
    }
    fetched = (await client.get(f"/api/v1/integrations/{created['id']}", headers=headers)).text
    listing = (await client.get("/api/v1/integrations", headers=headers)).text
    assert_no_secrets(json.dumps(created), fetched, listing)
    assert "auth_secret" not in fetched and "priv_secret" not in fetched

    stored = (await db_session.execute(text("SELECT credential_ciphertext, config FROM integration"))).one()
    assert_no_secrets(stored.credential_ciphertext, json.dumps(stored.config))
    plaintext = json.loads(decrypt_secret(stored.credential_ciphertext))
    assert plaintext["auth_secret"] == AUTH_SECRET and plaintext["priv_secret"] == PRIV_SECRET

    audit = (await db_session.execute(text("SELECT coalesce(before::text,'') || coalesce(after::text,'') FROM audit_log"))).scalars().all()
    assert_no_secrets(*audit)


@pytest.mark.parametrize("auth", ["sha1", "sha224", "sha256", "sha384", "sha512"])
async def test_all_supported_auth_protocols_are_accepted(client, auth_headers, auth):
    headers = await auth_headers("DCIM Manager")
    assert (await create_v3(client, headers, auth_protocol=auth))["config"]["snmpv3"]["auth_protocol"] == auth


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"auth_protocol": "md5"}, "insecure"),
        ({"priv_protocol": "des"}, "insecure"),
        ({"auth_protocol": "none"}, "insecure"),
        ({"priv_protocol": "none"}, "insecure"),
        ({"auth_protocol": "sha3"}, "unsupported authentication"),
        ({"priv_protocol": "camellia"}, "unsupported privacy"),
        ({"auth_protocol": "sha1", "priv_protocol": "aes256"}, "longer authentication hash"),
        ({"auth_protocol": "sha224", "priv_protocol": "aes256"}, "longer authentication hash"),
        ({"auth_secret": "short"}, "authentication secret"),
        ({"priv_secret": "1234567"}, "privacy secret"),
        ({"auth_secret": "tab\tinside-secret"}, "control characters"),
        ({"username": ""}, "username"),
        ({"username": "x" * 33}, "username"),
    ],
)
async def test_invalid_or_weak_combinations_are_rejected_without_echoing_secrets(client, auth_headers, changes, fragment):
    headers = await auth_headers("DCIM Manager")
    body = {"name": "i-bad", "integration_type": "snmp", "target_host": "192.0.2.10", "snmpv3": v3_body(**changes)}
    response = await client.post("/api/v1/integrations", json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]
    assert_no_secrets(response.text)
    for value in changes.values():
        if isinstance(value, str) and len(value) >= 5 and "secret" in str(changes):
            assert value not in response.text
    assert (await client.get("/api/v1/integrations", headers=headers)).json() == []


async def test_structural_misuse_is_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    base = {"name": "i-misuse", "target_host": "192.0.2.11"}
    cases = [
        {"integration_type": "icmp", "snmpv3": v3_body()},
        {"integration_type": "snmp", "snmpv3": v3_body(), "credential": "public"},
        {"integration_type": "snmp", "snmpv3": v3_body(), "config": {"version": "v2c"}},
        {"integration_type": "snmp", "config": {"version": "v3"}},
        {"integration_type": "snmp", "config": {"version": "v3"}, "credential": "public"},
        {"integration_type": "snmp", "config": {"version": "v9"}},
        {"integration_type": "snmp", "config": {"community": "public"}},
        {"integration_type": "snmp", "config": {"version": "v2c", "nested": {"priv_key": "x"}}},
        {"integration_type": "snmp", "config": {"snmpv3": {"security_level": "noAuthNoPriv"}}},
        {"integration_type": "snmp", "snmpv3": v3_body() | {"surprise": 1}},
        {"integration_type": "snmp", "snmpv3": {"username": "u"}},
    ]
    for case in cases:
        response = await client.post("/api/v1/integrations", json=base | case, headers=headers)
        assert response.status_code == 422, (case, response.text)
        assert_no_secrets(response.text)


async def test_update_replaces_credentials_and_keeps_secrets_out_of_responses(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    created = await create_v3(client, headers)
    new_auth = "rotated-auth-secret-SEC04"
    patched = await client.patch(
        f"/api/v1/integrations/{created['id']}",
        json={"snmpv3": v3_body(auth_secret=new_auth, auth_protocol="sha512", priv_protocol="aes256")},
        headers=headers | {"If-Match": str(created["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["config"]["snmpv3"]["auth_protocol"] == "sha512"
    assert new_auth not in patched.text and AUTH_SECRET not in patched.text
    ciphertext = (await db_session.execute(text("SELECT credential_ciphertext FROM integration"))).scalar_one()
    assert json.loads(decrypt_secret(ciphertext))["auth_secret"] == new_auth


async def test_other_updates_preserve_the_v3_credential_and_forbid_downgrade_by_config(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    created = await create_v3(client, headers)
    before = (await db_session.execute(text("SELECT credential_ciphertext FROM integration"))).scalar_one()
    keep = await client.patch(
        f"/api/v1/integrations/{created['id']}", json={"config": {"version": "v3", "note": "edited"}},
        headers=headers | {"If-Match": "1"},
    )
    assert keep.status_code == 200 and keep.json()["config"]["snmpv3"]["username"] == "dcim-poller"
    assert keep.json()["config"]["note"] == "edited"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT credential_ciphertext FROM integration"))).scalar_one() == before

    for config in (
        {"version": "v2c"},                                      # silent downgrade without a new community string
        {"version": "v3", "snmpv3": {"security_level": "noAuthNoPriv", "username": "x"}},  # forged descriptor
    ):
        response = await client.patch(
            f"/api/v1/integrations/{created['id']}", json={"config": config}, headers=headers | {"If-Match": "2"}
        )
        assert response.status_code == 422, (config, response.text)
    deliberate = await client.patch(
        f"/api/v1/integrations/{created['id']}",
        json={"config": {"version": "v2c"}, "credential": "community-string"}, headers=headers | {"If-Match": "2"},
    )
    assert deliberate.status_code == 200
    assert deliberate.json()["credential_kind"] == "secret" and "snmpv3" not in deliberate.json()["config"]


async def test_v2c_behaviour_is_unchanged(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    created = await create_integration(
        client, headers, integration_type="snmp", config={"version": "v2c"}, credential="v2c-community-string"
    )
    assert created["credential_kind"] == "secret" and created["config"] == {"version": "v2c"}
    assert "v2c-community-string" not in json.dumps(created)
    ciphertext = (await db_session.execute(text("SELECT credential_ciphertext FROM integration"))).scalar_one()
    assert decrypt_secret(ciphertext) == "v2c-community-string"
    patched = await client.patch(
        f"/api/v1/integrations/{created['id']}", json={"credential": "rotated-community"}, headers=headers | {"If-Match": "1"}
    )
    assert patched.status_code == 200
    plain = await create_integration(client, headers, integration_type="snmp")  # no config, no credential
    assert plain["credential_kind"] == "none"
    other = await create_integration(client, headers, integration_type="rest", config={"headers": {"X": "1"}}, credential="api-key")
    assert other["credential_kind"] == "secret"


async def test_secrets_never_reach_error_logs_or_output(client, auth_headers, monkeypatch, capsys, caplog):
    headers = await auth_headers("DCIM Manager")
    sink = io.StringIO()
    monkeypatch.setattr(
        error_module, "logger", structlog.wrap_logger(structlog.PrintLogger(file=sink), processors=[structlog.processors.JSONRenderer()])
    )
    await create_v3(client, headers)
    bad = await client.post(
        "/api/v1/integrations",
        json={"name": "i-x", "integration_type": "snmp", "target_host": "h", "snmpv3": v3_body(auth_protocol="md5")},
        headers=headers,
    )
    assert bad.status_code == 422
    malformed = await client.post("/api/v1/integrations", content=b'{"snmpv3": {"auth_secret": "' + AUTH_SECRET.encode() + b'"', headers=headers | {"Content-Type": "application/json"})
    assert malformed.status_code == 422
    captured = capsys.readouterr()
    assert_no_secrets(sink.getvalue(), caplog.text, captured.out, captured.err, bad.text, malformed.text)


async def test_rbac_for_credential_updates(client, auth_headers):
    manager = await auth_headers("DCIM Manager")
    created = await create_v3(client, manager)
    for role in ("Viewer", "Operator", "Engineer"):
        headers = await auth_headers(role)
        denied = await client.patch(
            f"/api/v1/integrations/{created['id']}", json={"snmpv3": v3_body()}, headers=headers | {"If-Match": "1"}
        )
        assert denied.status_code == 403, role
        assert (await client.post("/api/v1/integrations", json={"name": "n", "integration_type": "snmp", "target_host": "h",
                                                                 "snmpv3": v3_body()}, headers=headers)).status_code == 403
        read = await client.get(f"/api/v1/integrations/{created['id']}", headers=headers)
        assert read.status_code == 200 and secret_free(read.text)


def secret_free(blob: str) -> bool:
    return not any(secret in blob for secret in SECRETS)
