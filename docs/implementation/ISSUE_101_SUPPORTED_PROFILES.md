# Issue #101 supported-profile matrix

Version 1. Maintained with the code: change this file in the same pull request that changes a profile template,
the SNMPv3 algorithm policy, or the interoperability harness.

## How to read the status columns

| Status | Meaning |
| --- | --- |
| **Supported by code** | Implemented and covered by unit, API or integration tests that run in CI. |
| **Automated interop verified** | Exercised by `edge_collector/interop` (`run_interop.py`) against pysnmp 7.1.30, an independent SNMP implementation, over loopback. Runs in the `edge-interop` CI job. |
| **Manual interop verified** | Run by hand against an external implementation, outside CI. The only manual run on record is the pysnmp check of five algorithm combinations during Slice B. The automated harness now covers all twelve, so no row depends on it. |
| **Not yet hardware validated** | No physical vendor device has been polled. Every row in this document carries this status. |

Nothing in this document claims that a physical switch, router, or patch-panel management card was tested. A row moves to
"hardware validated" only after someone runs the checklist at the end of this file against a real device and records the
device model, firmware and date here.

## 1. Profile templates

Templates live in `backend/app/application/network/profile_templates.py` and are listed by
`GET /api/v1/network-profiles/templates`. An administrator copies one into the vendor and device profile tables. Nothing is
applied automatically.

| | Generic IEEE LLDP | Cisco CDP + LLDP |
| --- | --- | --- |
| Template key | `ieee-lldp-switch` | `cisco-cdp-lldp` |
| Vendor profile / device profile | `generic-lldp` / `generic-lldp-switch` | `cisco` / `cisco-switch` |
| Vendor / family | Vendor-neutral. No `sysObjectID` prefix is set; add the vendor's enterprise prefix before the matcher can select it. | Cisco, enterprise `1.3.6.1.4.1.9` (IOS and NX-OS style devices) |
| Device class | switch | switch |
| Protocols | SNMP | SNMP |
| SNMP versions declared by the device profile | v2c, v3 | v2c, v3 |
| SNMPv3 authentication | sha1, sha224, sha256, sha384, sha512 | same |
| SNMPv3 privacy | aes128, aes192, aes256 (192 and 256 need sha224+ and sha256+ respectively) | same |
| LLDP | Yes | Yes |
| CDP | No | Yes |
| Standard MIBs | SNMPv2-MIB system group; LLDP-MIB `lldpRemTable` (1.0.8802.1.1.2.1.4.1.1), `lldpLocPortTable` (1.0.8802.1.1.2.1.3.7.1), `lldpRemManAddrTable` (1.0.8802.1.1.2.1.4.2.1); IF-MIB `ifXTable` | same |
| Vendor MIBs | None | CISCO-CDP-MIB `cdpCacheTable` (1.3.6.1.4.1.9.9.23.1.2.1.1) |
| Interface discovery strategy | `if_xtable`, names from `ifName` | same |
| Local port name source | `lldp_loc_port_id` (LLDP local port table) | LLDP: `lldp_loc_port_id`. CDP: `ifName` from IF-MIB, by ifIndex. CDP device IDs are stripped of the domain (`strip_domain`) unless they are IP literals. |
| Metrics and mappings | Device profile declares `metrics: true`. Metric mappings are stored centrally per profile (`PUT .../metric-mappings`) and delivered in the discovery plan. The Edge Collector's own metric poller reads one OID, `sysUpTime` (`1.3.6.1.2.1.1.3.0`), over SNMPv2c. No other mapped metric is polled by the edge yet. | same |
| **Supported by code** | Yes | Yes |
| **Automated interop verified** | LLDP table, local port table and management address walk and decode over SNMPv3 (sha256+aes128 and sha512+aes256) against pysnmp-served rows | LLDP as left, plus CDP cache and ifXTable walk and decode over SNMPv3 against pysnmp-served rows |
| **Manual interop verified** | No | No |
| **Not yet hardware validated** | Yes: no physical device | Yes: no physical Cisco device |
| Known limitations | The rows pysnmp serves are fixtures written for the harness, not captures from a device. They prove the walk, BER and decode path, not any vendor's table quirks. LLDP chassis and port IDs are rendered by subtype (`render_lldp_id`): MAC and network addresses are formatted, name-like subtypes are shown as text when printable, and anything else, including an unknown subtype, is shown as hex. | As left. A CDP management address is rendered only when the raw value is 4 bytes (IPv4) or 16 bytes (IPv6); any other length leaves the management address empty. A device with CDP disabled returns an empty table and produces no observation. |

Limits that apply to both templates (see `edge_collector/README.md`): numeric target addresses only; a local CIDR allow-list;
20 seconds and 4096 varbinds per table; at most three LLDP tables and two CDP tables per integration; no GETBULK retransmission
beyond the session's bounded retries.

## 2. SNMP protocol and algorithm matrix

Authentication and privacy are fixed to authPriv. md5, des, 3des, noAuthNoPriv and authNoPriv are refused by both the
backend (when storing a credential) and the Edge Collector (when building a session). The Edge Collector never falls back to a
weaker level.

| Authentication | Privacy | Supported by code | Automated interop verified (pysnmp 7.1.30) | Hardware |
| --- | --- | --- | --- | --- |
| sha1 | aes128 | Yes | GET and 25-row walk | Not yet validated |
| sha224 | aes128 | Yes | GET and walk | Not yet validated |
| sha224 | aes192 | Yes | GET and walk | Not yet validated |
| sha256 | aes128 | Yes | GET and walk; also LLDP+CDP discovery walk | Not yet validated |
| sha256 | aes192 | Yes | GET and walk | Not yet validated |
| sha256 | aes256 | Yes | GET and walk | Not yet validated |
| sha384 | aes128 | Yes | GET and walk | Not yet validated |
| sha384 | aes192 | Yes | GET and walk | Not yet validated |
| sha384 | aes256 | Yes | GET and walk | Not yet validated |
| sha512 | aes128 | Yes | GET and walk | Not yet validated |
| sha512 | aes192 | Yes | GET and walk | Not yet validated |
| sha512 | aes256 | Yes | GET and walk; also LLDP+CDP discovery walk | Not yet validated |

Refused combinations, each asserted by the harness to fail before any packet is sent: md5 (any privacy), des (any
authentication), sha1 with aes192 or aes256, sha224 with aes256, and noAuthNoPriv. The three SHA/AES pairs are refused because
the localized authentication key is shorter than the AES key and the key-extension scheme is deliberately not implemented.

Every combination in the table passed in the recorded run. Nothing is listed as implementation-supported but unverified: pysnmp
7.1.30 can be configured with every combination the product accepts.

Protocol behavior verified by the harness, independent of algorithm: engine discovery; authenticated `notInTimeWindow` report
followed by resynchronisation and a successful retry; unknown user, wrong authentication secret, wrong privacy secret, and
mismatched authentication or privacy algorithm each rejected with a fixed message and no secret; `noSuchObject` surfaced as an
unknown-OID error; no secret, key or plaintext value in any captured datagram.

| Protocol | Status |
| --- | --- |
| SNMPv3 authPriv | Supported by code. Automated interop verified for the table above. Not yet hardware validated. |
| SNMPv2c | Supported by code (GET collector and walk session for discovery). Tested against the in-repo agent only; **not** exercised by the pysnmp harness. Not yet hardware validated. |
| SNMPv1 | Not supported. |

## 3. Residual risk: SNMPv2c

SNMPv2c remains available because the metric poller and existing integrations use it. A v2c community string is a shared
password sent in cleartext in every request, with no integrity protection and no replay protection beyond a request ID match.
An attacker on the path can read it, forge responses, and replay requests. Mitigations in the product: the Edge Collector
enforces a local CIDR allow-list and numeric targets; a v3 assignment can never fall back to a community string; v2c
communities are stored in the same encrypted credential field as v3 secrets and are never returned by the API. Operators who
need confidentiality or integrity on the management network should use SNMPv3 and treat v2c as a legacy path restricted to
trusted segments. Removing v2c from the metric poller is outside Issue #101.

## 4. Credential provisioning

- Central stores SNMPv3 secrets Fernet-encrypted (`Integration.credential_ciphertext`). The API is write-only for secrets;
  responses expose only `has_credential`, `credential_kind` and the non-secret descriptor (username, algorithms, context).
- Central's signed discovery plan contains no secret. No plaintext Central-to-Edge secret delivery exists.
- The Edge Collector reads credentials from a local JSON file, keyed by integration UUID, that must have mode 0600
  (`edge_collector/credentials.py`). An operator provisions the same secrets there. The local protocol version must match
  Central's assignment or discovery stops with an error.

## 5. Checklist to record a hardware validation

1. Device model, firmware, and which template was used.
2. Create an SNMPv3 user on the device with one combination from section 2; note which combination.
3. Provision the credential on Central and in the Edge Collector's local file.
4. Run discovery. Confirm the neighbors in the review page match the device's own `show lldp neighbors` or `show cdp neighbors`.
5. Record the result in a new "Hardware validated" table in this file with the date, who ran it, and any deviation.

Until then, treat every device as "supported by code, automated interop verified, not yet hardware validated".
