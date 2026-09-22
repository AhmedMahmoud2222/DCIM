import { apiFetch } from "@/lib/apiClient";
export interface NetworkDevice { id:string; asset_tag:string; lifecycle_status:string; name:string; device_type:string; source:string; last_observed_at:string|null }
export interface NetworkInterface { id:string; device_id:string; name:string; interface_type:string; description:string|null; mac_address:string|null; role:string; admin_status:string; oper_status:string; speed_mbps:number|null; duplex:string|null; mtu:number|null; native_vlan:number|null; ip_address:string|null; source:string; last_observed_at:string|null }
export interface NetworkConnection { id:string; interface_a_id:string; interface_b_id:string; cable_label:string|null; source:string; is_authoritative:boolean }
export interface NetworkTopology { devices:NetworkDevice[]; interfaces:NetworkInterface[]; connections:NetworkConnection[] }
export interface NetworkTrace { state:"complete"|"incomplete"|"unknown"; statement:string; source_device_id:string; hops:Array<{device_id:string;device:string;device_type:string;interface_id:string|null;interface:string|null;vlan:number|null;operational_state:string|null}> }
export interface CreateNetworkConnectionInput { interface_a_id:string; interface_b_id:string; cable_label?:string|null }
export const getNetworkTopology=()=>apiFetch<NetworkTopology>("/network/topology");
export const getNetworkTrace=(id:string)=>apiFetch<NetworkTrace>(`/network/trace/${id}`);
export const getEquipmentNetworkContext=(id:string)=>apiFetch<NetworkTrace>(`/network/equipment/${id}/context`);
export const createNetworkConnection=(body:CreateNetworkConnectionInput)=>apiFetch<NetworkConnection>("/network/connections",{method:"POST",body:JSON.stringify(body)});
export const disconnectNetworkConnection=(connectionId:string)=>apiFetch<void>(`/network/connections/${connectionId}`,{method:"DELETE"});
