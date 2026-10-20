import pytest
from pydantic import ValidationError

from app.application.network.profile_schema import (
    DeviceProfileContent,
    VendorProfileContent,
    compare_versions,
    oid_has_prefix,
    parse_version,
    reject_secret_like_keys,
    validate_oid,
)
from app.application.network.profile_templates import TEMPLATES


def test_oid_validation():
    assert validate_oid("1.3.6.1.4.1.9") == "1.3.6.1.4.1.9"
    for bad in ("", "1", "3.6.1", "1.3.6.01", "1.3.x", "1..3", "1.3.6.1." + "9" * 11):
        with pytest.raises(ValueError):
            validate_oid(bad)


def test_prefix_is_arc_aligned():
    assert oid_has_prefix("1.3.6.1.4.1.9.1", "1.3.6.1.4.1.9")
    assert not oid_has_prefix("1.3.6.1.4.1.99", "1.3.6.1.4.1.9")


def test_version_parsing_and_comparison():
    assert parse_version("9.10") == (9, 10)
    assert compare_versions((9, 10), (9, 9)) == 1
    assert compare_versions((12,), (12, 0, 0)) == 0
    for bad in ("v1", "1.2.x", "", "1..2"):
        with pytest.raises(ValueError):
            parse_version(bad)


def test_every_shipped_template_validates():
    for template in TEMPLATES.values():
        vendor = {k: v for k, v in template["vendor"].items() if k != "code"}
        VendorProfileContent.model_validate(vendor)
        for device in template["devices"]:
            DeviceProfileContent.model_validate({k: v for k, v in device.items() if k != "code"})


def test_vendor_rejects_unknown_fields_and_bad_protocols():
    with pytest.raises(ValidationError):
        VendorProfileContent.model_validate({"name": "x", "surprise": 1})
    with pytest.raises(ValidationError):
        VendorProfileContent.model_validate({"name": "x", "supported_protocols": ["telnet"]})
    with pytest.raises(ValidationError):
        VendorProfileContent.model_validate({"name": "x", "sys_object_id_prefixes": ["1.3.6", "1.3.6"]})
    with pytest.raises(ValidationError):
        VendorProfileContent.model_validate({"name": "x", "discovery_oids": {"nope": "1.3.6.1"}})


def test_enabled_neighbor_protocol_requires_table_and_snmp():
    with pytest.raises(ValidationError):
        VendorProfileContent.model_validate(
            {"name": "x", "supported_protocols": ["snmp"], "neighbor_discovery": {"lldp": {"enabled": True}}}
        )
    template = TEMPLATES["ieee-lldp-switch"]["vendor"]
    document = {k: v for k, v in template.items() if k != "code"} | {"supported_protocols": ["icmp"]}
    with pytest.raises(ValidationError):
        VendorProfileContent.model_validate(document)


def test_device_behaviour_must_be_backed_by_capability_and_firmware_order():
    with pytest.raises(ValidationError):
        DeviceProfileContent.model_validate({"name": "d", "neighbor_behavior": {"cdp": {"enabled": True}}})
    with pytest.raises(ValidationError):
        DeviceProfileContent.model_validate({"name": "d", "firmware_min": "10.0", "firmware_max": "9.0"})
    with pytest.raises(ValidationError):
        DeviceProfileContent.model_validate({"name": "d", "firmware_min": "ten"})


def test_match_criteria_shapes():
    ok = {"name": "d", "match_criteria": [{"field": "sys_object_id", "op": "prefix", "value": "1.3.6.1.4.1.9.1"}]}
    DeviceProfileContent.model_validate(ok)
    for bad in (
        {"field": "sys_object_id", "op": "contains", "value": "1.3"},
        {"field": "sys_object_id", "op": "equals", "value": "not-an-oid"},
        {"field": "model", "op": "in", "value": "single"},
        {"field": "model", "op": "equals", "value": ["a", "b"]},
        {"field": "model", "op": "regex", "value": ".*"},
        {"field": "model", "op": "equals", "value": "x" * 257},
    ):
        with pytest.raises(ValidationError):
            DeviceProfileContent.model_validate({"name": "d", "match_criteria": [bad]})


def test_secret_like_keys_are_refused_at_any_depth():
    reject_secret_like_keys({"a": {"b": [{"ok": 1}]}})
    for bad in ({"community": "x"}, {"a": {"auth_password": "x"}}, {"a": [{"privKey": "x"}]}, {"api_token": "x"}):
        with pytest.raises(ValueError):
            reject_secret_like_keys(bad)
