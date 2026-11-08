"""Run the outbound Edge Collector: python -m edge_collector --help."""
from __future__ import annotations

import argparse
import ipaddress
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .client import CentralClient
from .config import CollectorConfig
from .credentials import CredentialStoreError, LocalCredentialStore
from .discovery_scheduler import ScheduledDiscovery
from .discovery_sessions import local_session_factory
from .queue import SQLiteQueue
from .runtime import EdgeRuntime
from .snmp import SNMPTargetPolicy
from .snmp_metrics import collect_raw_numeral, metric_session_for
from .telemetry_scheduler import ScheduledTelemetry


def main() -> int:
    parser = argparse.ArgumentParser(description="DCIM outbound collector with bounded LLDP/CDP discovery")
    parser.add_argument("--central-url", required=True, help="HTTPS Central API URL, including /api/v1")
    parser.add_argument("--collector-id", required=True, type=uuid.UUID)
    parser.add_argument("--credentials", required=True, type=Path, help="Private local SNMP credential JSON file")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--allow-network", action="append", required=True, help="Explicit SNMP CIDR allowlist; repeatable")
    args = parser.parse_args()
    url = urlsplit(args.central_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        parser.error("central-url must be HTTPS without embedded credentials")
    secret = os.environ.get("DCIM_COLLECTOR_SECRET")
    if not secret:
        parser.error("DCIM_COLLECTOR_SECRET is required")
    try:
        store = LocalCredentialStore.load(args.credentials)
        policy = SNMPTargetPolicy(allowed_networks=tuple(ipaddress.ip_network(n) for n in args.allow_network))
    except (CredentialStoreError, ValueError):
        parser.error("invalid local credential file or network policy")
    client = CentralClient(args.central_url, args.collector_id, secret)
    queue = SQLiteQueue(CollectorConfig(args.database))
    discovery = ScheduledDiscovery(client.get_discovery_plan, local_session_factory(store, policy))

    def read_numeral(item, target, oid):
        session = metric_session_for(store, str(item["integration_id"]), target, policy=policy, timeout_seconds=2.0, retries=0)
        return collect_raw_numeral(session, oid)

    telemetry = ScheduledTelemetry(client.get_discovery_plan, client.get_telemetry_contracts, read_numeral)
    runtime = EdgeRuntime(queue, client, discovery=discovery, telemetry=telemetry)
    try:
        while True:
            runtime.run_once()
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        discovery.close()
        telemetry.close()
        queue.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
