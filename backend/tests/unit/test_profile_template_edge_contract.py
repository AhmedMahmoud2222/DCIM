"""The neighbor tables a profile template defines must be consumable by the edge adapters."""

import pytest
from edge_collector.neighbors import ProtocolSpec

from app.application.network.profile_schema import VendorProfileContent
from app.application.network.profile_templates import TEMPLATES


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_every_enabled_template_protocol_is_accepted_by_the_edge_adapter(key):
    vendor = {k: v for k, v in TEMPLATES[key]["vendor"].items() if k != "code"}
    stored = VendorProfileContent.model_validate(vendor).neighbor_discovery.model_dump(exclude_none=True)
    assert stored, "template should define at least one neighbor protocol"
    for protocol, spec in stored.items():
        parsed = ProtocolSpec.from_plan(spec)
        assert parsed.table_oid == TEMPLATES[key]["vendor"]["neighbor_discovery"][protocol]["table_oid"]
        assert set(parsed.columns) == set(spec["columns"])
        assert dict(parsed.local_port_columns) == spec.get("local_port_columns", {})
