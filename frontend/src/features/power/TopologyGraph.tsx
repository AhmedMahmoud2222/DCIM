import { useMemo } from "react";

import { PowerConnection, PowerNode } from "@/types";

const typeColor: Record<string, string> = { utility_intake: "#c084fc", generator: "#fb923c", ups: "#60a5fa", power_panel: "#2dd4bf", pdu: "#4ade80", pdu_outlet: "#a3e635", equipment_power_input: "#94a3b8" };

export function TopologyGraph({ nodes, connections, selectedId, onSelect }: { nodes: PowerNode[]; connections: PowerConnection[]; selectedId: string | null; onSelect: (id: string) => void }) {
  const layout = useMemo(() => {
    const active = connections.filter((connection) => connection.effective_to === null && connection.status === "active");
    const incoming = new Map<string, number>();
    const outgoing = new Map<string, string[]>();
    nodes.forEach((node) => { incoming.set(node.id, 0); outgoing.set(node.id, []); });
    active.forEach((connection) => { incoming.set(connection.target_node_id, (incoming.get(connection.target_node_id) ?? 0) + 1); outgoing.get(connection.source_node_id)?.push(connection.target_node_id); });
    const levels = new Map<string, number>();
    const queue = nodes.filter((node) => (incoming.get(node.id) ?? 0) === 0).map((node) => node.id);
    queue.forEach((id) => levels.set(id, 0));
    for (let cursor = 0; cursor < queue.length; cursor += 1) { const id = queue[cursor]; const level = levels.get(id) ?? 0; (outgoing.get(id) ?? []).forEach((target) => { if (!levels.has(target)) { levels.set(target, level + 1); queue.push(target); } }); }
    nodes.forEach((node) => { if (!levels.has(node.id)) levels.set(node.id, 0); });
    const grouped = new Map<number, PowerNode[]>();
    nodes.forEach((node) => { const level = levels.get(node.id) ?? 0; grouped.set(level, [...(grouped.get(level) ?? []), node]); });
    const positions = new Map<string, { x: number; y: number }>();
    [...grouped.entries()].forEach(([level, group]) => group.forEach((node, index) => positions.set(node.id, { x: 110 + level * 220, y: 74 + index * 112 })));
    return { active, positions, width: Math.max(680, (Math.max(...levels.values(), 0) + 1) * 220 + 110), height: Math.max(260, Math.max(...[...grouped.values()].map((group) => group.length), 1) * 112 + 70) };
  }, [connections, nodes]);

  if (nodes.length === 0) return <div className="empty-state"><div><p className="font-medium text-slate-200">No power topology yet</p><p className="mt-1 text-sm text-slate-500">Create power nodes and connections to see a directional topology here.</p></div></div>;
  return <div className="overflow-auto rounded-xl border border-slate-800 bg-[#080d17] p-3"><svg width={layout.width} height={layout.height} className="min-w-full" role="img" aria-label="Power topology graph"><defs><marker id="power-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#475569" /></marker></defs>{layout.active.map((connection) => { const source = layout.positions.get(connection.source_node_id); const target = layout.positions.get(connection.target_node_id); if (!source || !target) return null; return <line key={connection.id} x1={source.x + 68} y1={source.y} x2={target.x - 68} y2={target.y} stroke="#475569" strokeWidth="2" markerEnd="url(#power-arrow)" />; })}{nodes.map((node) => { const position = layout.positions.get(node.id)!; const selected = node.id === selectedId; return <g key={node.id} transform={`translate(${position.x - 68} ${position.y - 32})`} onClick={() => onSelect(node.id)} className="cursor-pointer"><rect width="136" height="64" rx="10" fill={selected ? "#312e81" : "#111827"} stroke={selected ? "#a5b4fc" : typeColor[node.node_type] ?? "#64748b"} strokeWidth={selected ? "3" : "2"} /><text x="12" y="25" fill="#e2e8f0" fontSize="12" fontWeight="600">{node.label.slice(0, 19)}</text><text x="12" y="46" fill={typeColor[node.node_type] ?? "#94a3b8"} fontSize="10">{node.node_type.replace(/_/g, " ")}</text></g>; })}</svg></div>;
}
