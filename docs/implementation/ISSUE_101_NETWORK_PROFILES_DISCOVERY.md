# Issue #101: vendor/device profiles, SNMPv3, LLDP/CDP and cable tracing

Version 2. Base: `main@7364e68`. This file states what the implementation does today. Companion documents:

- `ISSUE_101_SUPPORTED_PROFILES.md`: supported-profile matrix and interoperability status.
- `edge_collector/interop/README.md`: the SNMPv3 interoperability harness.
- `ISSUE_101_HANDOVER.md`: history of how the work was built and reviewed.

The Protocol, Driver, Collector and authoritative-reconciliation boundaries are unchanged. Security requirement SEC-04 stays
linked to issue #38 until SNMPv3 passes the independent review.

## 1. Profiles

`VendorProfile` and `DeviceProfile` are persistent rows holding JSONB documents validated by strict schemas (unknown keys and
secret-like keys are rejected). The matcher ranks candidates by criteria count, `sysObjectID` specificity and priority, matches
`sysObjectID` prefixes on arc boundaries, and refuses an ambiguous result instead of choosing. Updates use `If-Match`;
retirement is terminal. Binding an integration to a device profile takes share locks on the device and vendor profile, so it
cannot race with retirement, and a vendor or device update that would strand a bound integration returns 409. Permissions:
`network_profile:read`, `network_profile:manage`. These are not site-scoped, so a restricted user cannot reach them.

## 2. SNMPv3

- authPriv only. Authentication: SHA-1, SHA-224, SHA-256, SHA-384, SHA-512 (RFC 7860 truncations). Privacy: AES-128, 192, 256.
  AES-192 and AES-256 need a hash at least as long as the key. MD5, DES, noAuthNoPriv and authNoPriv are refused, never
  negotiated down.
- Implemented on `cryptography` in `edge_collector/snmp_usm.py`, `snmp_v3.py` and `snmp_wire.py`: key localization, MAC,
  CFB128 privacy, engine discovery, time-window resynchronisation, bounded retries and walks, strict response identity checks.
- Interoperability: the real stack is run against pysnmp 7.1.30 by `edge_collector/interop` for all twelve accepted
  combinations (authenticated encrypted GET, multi-page GETBULK walk), plus failure cases, resynchronisation, an LLDP/CDP
  discovery walk and a wire-confidentiality check. See the matrix document for what this does and does not prove. No physical
  device has been tested.
- Secrets: Central stores them Fernet-encrypted; the API never returns them; the discovery plan carries none. The Edge
  Collector reads them from a local mode-0600 file keyed by integration UUID. There is no plaintext Central-to-Edge delivery.
- SNMPv2c residual risk: v2c stays for the existing metric poller and for discovery on integrations that use it. A community
  string travels in cleartext and the protocol has no integrity or replay protection. It is not exercised by the pysnmp
  harness. See section 3 of the matrix document.

## 3. LLDP/CDP discovery

Table-driven edge adapters read the LLDP-MIB and CISCO-CDP-MIB tables named by the profile, sanitize every string, tolerate
malformed rows, and bound size per row, per table and per record. Central stores each sighting as `DiscoveredNeighbor`
evidence keyed by a SHA-256 identity key. Discovery never writes `port_connection`, `cable` or a pass-through. An operator
confirms, rejects or revokes a neighbor in the review page; confirming binds to the proposal the operator saw. A confirmed
neighbor becomes a cable only through a separate explicit action. Permissions: `discovery:read`, `discovery:reconcile`.

## 4. Cables and multi-hop topology

Three records describe "port A connects to port B", and they stay separate:

- `Cable` and `CableEndpoint`: the physical cable, with identity, two endpoints and a lifecycle (planned, installed, removed;
  removed cables stay as history). A live cable realizes one logical `PortConnection`; it never rewrites one it did not create,
  and the legacy connect API refuses to change a connection a live cable owns.
- `PortConnection` (existing): the logical edge the impact services traverse. Unchanged.
- `PortPassThrough` and `PortPassThroughMember` (migration 0040): how a signal continues inside one device, for example patch
  panel front 01 to rear 01. Each pass-through belongs to one `Equipment` and has exactly two member ports.

PostgreSQL enforces the pass-through rules: both ports belong to the pass-through's equipment (composite foreign keys), a port is
in at most one pass-through (unique member port), a deferred trigger requires exactly two members at commit, and members cannot
be re-pointed. Writers take the topology advisory lock, shared with cable and neighbor changes.

### Trace semantics (`GET /api/v1/topology/ports/{id}/trace`, format `trace.v2`)

From the start port the trace repeats: follow the port's live cable (planned or installed) to its far port, else its logical
`PortConnection`; at the far port, look for a pass-through; if there is one, continue from its partner port over that port's own
link. Removed cables are never followed (they appear in `previous_cables` for the start port). LLDP/CDP evidence is reported
separately and never traversed. The trace only reads.

Each `path` element holds `link`, `hop` (the arrival port, or `restricted`) and `pass_through` (how the signal continues inside
the arrival device, or null). One-cable traces keep their v1 shape: one element with a `link` and a `hop`. New fields:
`hop_count`, `max_hops`, `terminated_reason`, `format: "trace.v2"`.

| `terminated` | Meaning |
| --- | --- |
| `end_of_path` | Arrived at a port with no pass-through. |
| `no_link` | The start port, or the partner port of the last pass-through, has no link. |
| `restricted` | The next equipment is outside the caller's scope. The trace stops there. |
| `cycle_detected` | The path returned to a port it already visited. |
| `hop_limit` | More than 32 links. |
| `broken_topology` | Stored data is inconsistent (a cable without two endpoints, a pass-through without two members, a missing port, or more than one live link on a port). `terminated_reason` says which, without identifiers. |

Visibility follows `cable:read` and the caller's site and rack scope. The start port must be visible (404 otherwise). The trace
stops at the first hidden device and returns no identifier of it or anything beyond it, including a visible device that lies
behind a hidden one.

### Remaining limits

- The trace follows the start port's own link outward. Starting at a patch-panel port shows one direction; trace from the other
  end for the other direction.
- Pass-throughs are one-to-one and structural. Fan-out devices (splitters, multiplexers), layer-2 forwarding through a switch
  and logical circuits are not modeled.
- Failure-impact services still read `PortConnection` only; pass-throughs do not change impact results.
- Cable and pass-through lists are not paged beyond the API limit of 200 per request.

## 5. Interfaces

Cables: `/api/v1/cables`. Pass-throughs: `/api/v1/pass-throughs` (`cable:read`, `cable:manage`; scope-aware; 404 for anything
outside the caller's scope). Frontend: Cables, Pass-throughs, Trace, Neighbors and Network Profiles pages.

Migration chain: 0035 units and metric registry, 0036 catalog extraction apply, 0037 network profiles, 0038 discovered
neighbors, 0039 cables, 0040 port pass-through. One Alembic head. Downgrades refuse to discard recorded profiles, decided
neighbors, cables or pass-throughs.
