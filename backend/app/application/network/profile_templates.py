"""Reviewable profile templates.

Templates are *data* an administrator may copy into `POST /network-profiles/vendors`; they
are never applied automatically and carry no behaviour. They exist so the standard MIB
layouts (IEEE LLDP-MIB, IF-MIB, CISCO-CDP-MIB) are written down once, in one reviewed
place, instead of being re-typed per tenant or hard-coded in a collector.
"""

LLDP_SPEC = {
    "enabled": True,
    "table_oid": "1.0.8802.1.1.2.1.4.1.1",  # LLDP-MIB::lldpRemEntry
    "columns": {
        "chassis_id_subtype": 4, "chassis_id": 5, "port_id_subtype": 6, "port_id": 7, "port_desc": 8,
        "sys_name": 9, "sys_desc": 10, "sys_cap_supported": 11, "sys_cap_enabled": 12,
    },
    "index_fields": ["time_mark", "local_port_num", "rem_index"],
    "local_port_table_oid": "1.0.8802.1.1.2.1.3.7.1",  # LLDP-MIB::lldpLocPortEntry
    "management_address_table_oid": "1.0.8802.1.1.2.1.4.2.1",  # LLDP-MIB::lldpRemManAddrEntry
}

CDP_SPEC = {
    "enabled": True,
    "table_oid": "1.3.6.1.4.1.9.9.23.1.2.1.1",  # CISCO-CDP-MIB::cdpCacheEntry
    "columns": {
        "address_type": 3, "address": 4, "version": 5, "device_id": 6, "device_port": 7, "platform": 8,
        "capabilities": 9, "native_vlan": 11, "duplex": 12,
    },
    "index_fields": ["if_index", "device_index"],
    "local_port_table_oid": "1.3.6.1.2.1.31.1.1.1.1",  # IF-MIB::ifName
}

STANDARD_DISCOVERY_OIDS = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_object_id": "1.3.6.1.2.1.1.2.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_contact": "1.3.6.1.2.1.1.4.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
    "sys_location": "1.3.6.1.2.1.1.6.0",
}

TEMPLATES: dict[str, dict] = {
    "ieee-lldp-switch": {
        "title": "Generic LLDP-capable SNMP device (IEEE LLDP-MIB)",
        "vendor": {
            "code": "generic-lldp",
            "name": "Generic LLDP device",
            "description": "Vendor-neutral SNMP profile. Add the vendor's enterprise sysObjectID prefix before use.",
            "sys_object_id_prefixes": [],
            "supported_protocols": ["snmp"],
            "discovery_oids": STANDARD_DISCOVERY_OIDS,
            "neighbor_discovery": {"lldp": LLDP_SPEC},
        },
        "devices": [
            {
                "code": "generic-lldp-switch",
                "name": "Generic LLDP switch",
                "device_class": "switch",
                "match_criteria": [],
                "capabilities": {"metrics": True, "interfaces": True, "lldp": True, "snmp_versions": ["v2c", "v3"]},
                "interface_discovery": {"strategy": "if_xtable", "name_source": "if_name"},
                "neighbor_behavior": {"lldp": {"enabled": True, "local_port_source": "lldp_loc_port_id"}},
            }
        ],
    },
    "cisco-cdp-lldp": {
        "title": "Cisco IOS/NX-OS style device (CDP + LLDP)",
        "vendor": {
            "code": "cisco",
            "name": "Cisco",
            "description": "Enterprise 1.3.6.1.4.1.9 devices with CISCO-CDP-MIB and LLDP-MIB.",
            "sys_object_id_prefixes": ["1.3.6.1.4.1.9"],
            "supported_protocols": ["snmp"],
            "discovery_oids": STANDARD_DISCOVERY_OIDS,
            "neighbor_discovery": {"lldp": LLDP_SPEC, "cdp": CDP_SPEC},
        },
        "devices": [
            {
                "code": "cisco-switch",
                "name": "Cisco switch",
                "device_class": "switch",
                "match_criteria": [],
                "capabilities": {
                    "metrics": True, "interfaces": True, "lldp": True, "cdp": True, "snmp_versions": ["v2c", "v3"],
                },
                "interface_discovery": {"strategy": "if_xtable", "name_source": "if_name"},
                "neighbor_behavior": {
                    "lldp": {"enabled": True, "local_port_source": "lldp_loc_port_id"},
                    "cdp": {"enabled": True, "device_id_normalization": "strip_domain"},
                },
            }
        ],
    },
}
