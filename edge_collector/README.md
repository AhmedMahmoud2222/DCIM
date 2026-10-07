# Edge Collector discovery runtime

Install the collector independently from the repository root:

```sh
pip install ./edge_collector
dcim-edge-collector --central-url https://central.example/api/v1 \
  --collector-id COLLECTOR_UUID --credentials /etc/dcim/snmp.json \
  --database /var/lib/dcim/queue.db --allow-network 10.20.0.0/16
```

Provide the collector HMAC secret using DCIM_COLLECTOR_SECRET through the service manager.
Do not put secrets in command-line arguments. Central uses HTTPS with certificate validation.
Run under a dedicated service account and restrict its configuration and queue directories.

SNMP credentials are provisioned locally, keyed by integration UUID. Central's signed
discovery plan contains assignments and profile data, never SNMP passwords. The supported
model is operator-managed local credentials; no plaintext Central-to-Edge secret delivery
is implemented. See credentials.py for the JSON shape. Set the file to mode 0600 and
restart the process after credential changes. Store backup copies with the same protection.

The executable invokes EdgeRuntime once per second. Discovery uses a single worker with
at most one outstanding task, while SQLite writes, delivery retries and heartbeats remain
on the runtime thread. Plans refresh every 60 seconds and expire after 300 seconds without
a successful refresh. Integrations poll at their assigned interval (60–1800 seconds).
Unprofiled integrations do not perform discovery. One failed integration does not stop
the next; LLDP and CDP are isolated by the existing per-protocol runner.

Discovery currently requires numeric target addresses to avoid unbounded operating-system
DNS resolution. The local CIDR allowlist is enforced independently of Central. UDP attempts
have a two-second timeout and no retransmissions in this runtime; SNMPv3 engine discovery
and resynchronization have bounded attempts. Each table gets a 20-second deadline plus
at most one in-flight request, and a maximum of 4096 visited varbinds. There are at most
three LLDP tables and two CDP tables per integration. No worker task backlog accumulates.

The local credential version must match Central's assignment: a v3 assignment cannot
silently use a v2c community. V3 sessions enforce authPriv. Failed or expired scans do
not generate successful completion markers. Discovery evidence remains separate from
operator-confirmed logical connections and physical cables.

Troubleshooting: check Central assignment/enabled status, selected active profile,
numeric target address and CIDR policy, local credential UUID/version and file permissions,
then UDP reachability. Do not enable payload/credential debug logging. Offline delivery
continues to use the bounded durable queue and existing backoff.

## Interoperability

`edge_collector/interop` runs this SNMPv3 stack against an independent implementation (pysnmp) over loopback: every supported
authentication/privacy combination, failure modes, time-window resynchronisation, an LLDP/CDP discovery walk and a check that no
secret appears on the wire. See `edge_collector/interop/README.md`. The supported-profile matrix, including which entries are
not yet validated on hardware, is in `docs/implementation/ISSUE_101_SUPPORTED_PROFILES.md`.
