import { apiFetch } from "@/lib/apiClient";
import type { CoolingLayout, HeatMap, RoomAirflow, RoomCapacity, RoomExceptions, ThermalMetric } from "@/types";

const base = (roomId: string) => `/cooling/rooms/${roomId}`;

export const getCoolingLayout = (roomId: string) => apiFetch<CoolingLayout>(`${base(roomId)}/layout`);
export const getHeatMap = (roomId: string, metric: ThermalMetric) => apiFetch<HeatMap>(`${base(roomId)}/heat-map?metric=${metric}`);
export const getRoomAirflow = (roomId: string) => apiFetch<RoomAirflow>(`${base(roomId)}/airflow`);
export const getRoomCapacity = (roomId: string) => apiFetch<RoomCapacity>(`${base(roomId)}/capacity`);
export const getRoomExceptions = (roomId: string) => apiFetch<RoomExceptions>(`${base(roomId)}/exceptions`);
