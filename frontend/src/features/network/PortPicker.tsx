import { useQuery } from "@tanstack/react-query";

import { getEquipmentPorts, listEquipment } from "@/features/equipment/api";

interface Props {
  label: string;
  equipmentId: string;
  portId: string;
  onChange: (value: { equipmentId: string; portId: string }) => void;
}

/** Two linked selects: equipment, then one of its network ports. */
export function PortPicker({ label, equipmentId, portId, onChange }: Props) {
  const equipment = useQuery({ queryKey: ["equipment", "list"], queryFn: listEquipment });
  const ports = useQuery({
    queryKey: ["equipment", equipmentId, "ports"],
    queryFn: () => getEquipmentPorts(equipmentId),
    enabled: equipmentId !== "",
  });
  return (
    <fieldset className="space-y-1">
      <legend className="text-xs text-slate-400">{label}</legend>
      <select
        aria-label={`${label} equipment`}
        value={equipmentId}
        onChange={(e) => onChange({ equipmentId: e.target.value, portId: "" })}
        className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
      >
        <option value="">Select equipment…</option>
        {equipment.data?.items.map((item) => (
          <option key={item.id} value={item.id}>
            {item.hostname ?? item.asset_tag} ({item.asset_tag})
          </option>
        ))}
      </select>
      <select
        aria-label={`${label} port`}
        value={portId}
        disabled={equipmentId === ""}
        onChange={(e) => onChange({ equipmentId, portId: e.target.value })}
        className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100 disabled:opacity-50"
      >
        <option value="">Select port…</option>
        {ports.data?.ports.map((port) => (
          <option key={port.id} value={port.id}>
            {port.display_name}
            {port.connection ? " (connected)" : ""}
          </option>
        ))}
      </select>
    </fieldset>
  );
}
