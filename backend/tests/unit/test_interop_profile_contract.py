"""The edge interoperability harness (edge_collector/interop) walks a copy of the LLDP and CDP profile specs.
That copy must equal what the product ships, or the harness would validate a profile nobody uses."""

from edge_collector.interop import scenarios

from app.application.network.profile_templates import CDP_SPEC, LLDP_SPEC, TEMPLATES


def test_harness_specs_equal_the_shipped_profile_templates():
    assert scenarios.LLDP_SPEC == LLDP_SPEC
    assert scenarios.CDP_SPEC == CDP_SPEC


def test_every_shipped_neighbor_template_is_covered_by_the_harness_plan():
    shipped = {
        protocol
        for template in TEMPLATES.values()
        for protocol in template["vendor"]["neighbor_discovery"]
    }
    assert shipped == set(scenarios.DISCOVERY_PLAN["neighbor_discovery"])


def test_harness_algorithm_matrix_matches_the_edge_and_backend_policies():
    from edge_collector import snmp_usm as edge

    from app.application.drivers import snmpv3 as backend

    every = {(a, p) for a in edge.AUTH_PROTOCOLS for p in edge.PRIV_PROTOCOLS}

    def accepted(validate, error) -> set[tuple[str, str]]:
        out = set()
        for pair in every:
            try:
                validate(*pair)
            except error:
                continue
            out.add(pair)
        return out

    edge_accepted = accepted(edge.validate_algorithms, edge.SNMPv3ConfigError)
    assert set(scenarios.SUPPORTED_COMBINATIONS) == edge_accepted  # the harness exercises exactly what the product accepts
    assert set(edge.AUTH_PROTOCOLS) == set(backend.AUTH_PROTOCOL_HASH_BYTES)
    assert set(edge.PRIV_PROTOCOLS) == set(backend.PRIV_PROTOCOL_KEY_BYTES)
    assert accepted(backend.validate_algorithms, backend.SnmpV3CredentialError) == edge_accepted
