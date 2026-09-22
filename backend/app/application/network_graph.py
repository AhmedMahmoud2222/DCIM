"""Deterministic, bounded traversal of the modeled physical network graph."""

from collections import deque
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class GraphInterface:
    id: UUID
    device_id: UUID
    name: str


def trace_to_core(
    source_device: UUID,
    interfaces: list[GraphInterface],
    connections: list[tuple[UUID, UUID]],
    core_devices: set[UUID],
    max_devices: int = 256,
) -> tuple[str, list[UUID]]:
    """Return state and alternating interface IDs; sorted BFS makes equal paths stable."""
    by_id = {item.id: item for item in interfaces}
    device_interfaces: dict[UUID, list[UUID]] = {}
    for item in interfaces:
        device_interfaces.setdefault(item.device_id, []).append(item.id)
    adjacency: dict[UUID, list[UUID]] = {}
    for left, right in connections:
        if left in by_id and right in by_id and left != right:
            adjacency.setdefault(left, []).append(right)
            adjacency.setdefault(right, []).append(left)
    queue: deque[tuple[UUID, list[UUID]]] = deque([(source_device, [])])
    visited = {source_device}
    while queue and len(visited) <= max_devices:
        device_id, path = queue.popleft()
        if device_id in core_devices:
            return "complete", path
        candidates: list[tuple[str, UUID, UUID, UUID]] = []
        for local_id in device_interfaces.get(device_id, []):
            for remote_id in adjacency.get(local_id, []):
                remote = by_id[remote_id]
                if remote.device_id not in visited:
                    candidates.append(
                        (f"{by_id[local_id].name}\0{remote.name}\0{remote.device_id}", local_id, remote_id, remote.device_id)
                    )
        for _, local_id, remote_id, next_device in sorted(candidates):
            visited.add(next_device)
            queue.append((next_device, [*path, local_id, remote_id]))
    return ("incomplete" if device_interfaces.get(source_device) else "unknown"), []
