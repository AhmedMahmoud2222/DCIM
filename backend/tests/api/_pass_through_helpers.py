"""Builders for multi-hop topology tests: switches, patch panels, cables and pass-throughs."""

from tests.api._cable_helpers import create_cable
from tests.api._network_inventory import make_device

PASS_THROUGHS = "/api/v1/pass-throughs"


def trace_url(port_id: str) -> str:
    return f"/api/v1/topology/ports/{port_id}/trace"


async def switch(client, headers, hostname: str, ports=("Eth1/1", "Eth1/24")) -> dict:
    return await make_device(client, headers, list(ports), hostname=hostname)


async def patch_panel(client, headers, hostname: str, pairs: int = 1) -> dict:
    return await make_device(client, headers, [f"{side}{n:02d}" for n in range(1, pairs + 1) for side in ("Front", "Rear")], hostname=hostname)


async def pass_through(client, headers, a: str, b: str, *, label: str | None = None) -> dict:
    response = await client.post(PASS_THROUGHS, json={"port_a_id": a, "port_b_id": b, "label": label}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def link(client, headers, a: str, b: str, *, label: str, status: str = "installed") -> dict:
    return await create_cable(client, headers, a, b, label=label, status=status)


async def build_chain(client, headers, *, panels: int = 1, prefix: str = "chain") -> dict:
    """swA Eth1/1 -> [PPn Front01 =pass-through= Rear01 -> ]* swB Eth1/24."""
    sw_a = await switch(client, headers, f"{prefix}-swa")
    sw_b = await switch(client, headers, f"{prefix}-swb")
    pps = [await patch_panel(client, headers, f"{prefix}-pp{i + 1}") for i in range(panels)]
    cables, pts = [], []
    previous = sw_a["port_by_name"]["Eth1/1"]
    for i, pp in enumerate(pps):
        cables.append(await link(client, headers, previous, pp["port_by_name"]["Front01"], label=f"{prefix}-C{i}"))
        pts.append(await pass_through(client, headers, pp["port_by_name"]["Front01"], pp["port_by_name"]["Rear01"], label=f"{prefix}-PT{i}"))
        previous = pp["port_by_name"]["Rear01"]
    cables.append(await link(client, headers, previous, sw_b["port_by_name"]["Eth1/24"], label=f"{prefix}-C{panels}"))
    return {"sw_a": sw_a, "sw_b": sw_b, "panels": pps, "cables": cables, "pass_throughs": pts}


def hops(trace: dict) -> list[tuple[str, str]]:
    """(hostname, port name) for each arrival port, in order."""
    return [(h["hop"]["remote"]["equipment_hostname"], h["hop"]["remote"]["port_name"]) for h in trace["path"] if not h["hop"]["restricted"]]
