import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { ApiError } from "@/lib/apiClient";

import {
  RackScope,
  SiteAccess,
  createGroup,
  deleteGroup,
  getGroup,
  getPermissionCatalog,
  listAllSites,
  listGroups,
  listSiteRacks,
  listUsers,
  setGroupMembers,
  setGroupPermissions,
  setGroupSiteAccess,
  updateGroup,
} from "./api";
import { PermissionState, applyPermissionState, groupByResource, permissionState, toggleSite } from "./permissionModel";

type Tab = "details" | "members" | "permissions" | "access";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Request failed.";
}

function RackPicker({ siteId, selected, onChange, disabled }: {
  siteId: string;
  selected: string[];
  onChange: (ids: string[]) => void;
  disabled: boolean;
}) {
  const racksQuery = useQuery({ queryKey: ["site-racks", siteId], queryFn: () => listSiteRacks(siteId) });
  if (racksQuery.isLoading) return <p className="text-xs text-slate-400">Loading racks…</p>;
  const racks = racksQuery.data?.items ?? [];
  if (racks.length === 0) return <p className="text-xs text-slate-500">No racks are placed in this site.</p>;
  return (
    <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2" aria-label="Racks in site">
      {racks.map((r) => (
        <li key={r.id}>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              disabled={disabled}
              checked={selected.includes(r.id)}
              onChange={(e) => onChange(e.target.checked ? [...selected, r.id] : selected.filter((id) => id !== r.id))}
            />
            {r.name} <span className="text-xs text-slate-500">{r.asset_tag}</span>
          </label>
        </li>
      ))}
    </ul>
  );
}

export function GroupsPage() {
  const queryClient = useQueryClient();
  const canManage = useHasPermission("group:manage");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("details");
  const [newName, setNewName] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const groupsQuery = useQuery({ queryKey: ["admin-groups"], queryFn: listGroups });
  const detailQuery = useQuery({
    queryKey: ["admin-group", selectedId],
    queryFn: () => getGroup(selectedId!),
    enabled: !!selectedId,
  });
  const catalogQuery = useQuery({ queryKey: ["permission-catalog"], queryFn: getPermissionCatalog });
  const usersQuery = useQuery({ queryKey: ["admin-users", ""], queryFn: () => listUsers(), enabled: tab === "members" });
  const sitesQuery = useQuery({ queryKey: ["all-sites"], queryFn: listAllSites, enabled: tab === "access" });

  const detail = detailQuery.data;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [allow, setAllow] = useState<Set<string>>(new Set());
  const [deny, setDeny] = useState<Set<string>>(new Set());
  const [sites, setSites] = useState<SiteAccess[]>([]);

  useEffect(() => {
    if (!detail) return;
    setName(detail.name);
    setDescription(detail.description ?? "");
    setMembers(detail.member_ids);
    setAllow(new Set(detail.allow_permissions));
    setDeny(new Set(detail.deny_permissions));
    setSites(detail.sites);
  }, [detail]);

  const onSaved = (text: string) => {
    setMessage({ kind: "ok", text });
    queryClient.invalidateQueries({ queryKey: ["admin-groups"] });
    queryClient.invalidateQueries({ queryKey: ["admin-group"] });
    queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    queryClient.invalidateQueries({ queryKey: ["effective-access"] });
  };
  const onError = (e: unknown) => setMessage({ kind: "error", text: errorText(e) });

  const createMutation = useMutation({
    mutationFn: () => createGroup({ name: newName }),
    onSuccess: (g) => {
      setNewName("");
      setSelectedId(g.id);
      setTab("details");
      onSaved("Group created.");
    },
    onError,
  });
  const detailsMutation = useMutation({ mutationFn: () => updateGroup(selectedId!, { name, description }), onSuccess: () => onSaved("Group updated."), onError });
  const deleteMutation = useMutation({
    mutationFn: () => deleteGroup(selectedId!),
    onSuccess: () => {
      setSelectedId(null);
      onSaved("Group deleted.");
    },
    onError,
  });
  const membersMutation = useMutation({ mutationFn: () => setGroupMembers(selectedId!, members), onSuccess: () => onSaved("Members saved."), onError });
  const permissionsMutation = useMutation({
    mutationFn: () => setGroupPermissions(selectedId!, [...allow], [...deny]),
    onSuccess: () => onSaved("Permissions saved."),
    onError,
  });
  const accessMutation = useMutation({
    mutationFn: () => setGroupSiteAccess(selectedId!, sites.map(({ site_id, rack_scope, rack_ids }) => ({ site_id, rack_scope, rack_ids }))),
    onSuccess: () => onSaved("Site and rack access saved."),
    onError,
  });

  function submitCreate(e: FormEvent) {
    e.preventDefault();
    setMessage(null);
    createMutation.mutate();
  }

  function setState(code: string, state: PermissionState) {
    const next = applyPermissionState(allow, deny, code, state);
    setAllow(next.allow);
    setDeny(next.deny);
  }

  function updateSite(siteId: string, patch: Partial<SiteAccess>) {
    setSites(sites.map((s) => (s.site_id === siteId ? { ...s, ...patch } : s)));
  }

  const tabs: [Tab, string][] = [["details", "Details"], ["members", "Members"], ["permissions", "Permissions"], ["access", "Site & rack access"]];

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Groups</h1>
      <p className="mb-4 max-w-2xl text-sm text-slate-400">
        A group carries permissions and site/rack access. A user gets the union of all their groups, and an explicit deny
        in any group overrides an allow. A new group grants nothing.
      </p>
      {message && (
        <p role={message.kind === "error" ? "alert" : "status"} className={`mb-3 text-sm ${message.kind === "error" ? "text-red-400" : "text-green-400"}`}>
          {message.text}
        </p>
      )}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          {canManage && (
            <form onSubmit={submitCreate} className="mb-3 flex gap-2" aria-label="Create group">
              <input aria-label="New group name" required maxLength={100} value={newName} onChange={(e) => setNewName(e.target.value)}
                placeholder="New group name" className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
              <button type="submit" className="rounded bg-blue-600 px-2 py-1 text-xs text-white">Add</button>
            </form>
          )}
          {groupsQuery.isError && <p role="alert" className="text-sm text-red-400">{errorText(groupsQuery.error)}</p>}
          <ul className="space-y-1" aria-label="Groups">
            {groupsQuery.data?.items.map((g) => (
              <li key={g.id}>
                <button
                  onClick={() => { setSelectedId(g.id); setMessage(null); }}
                  aria-pressed={selectedId === g.id}
                  className={`w-full rounded px-2 py-2 text-left text-sm ${selectedId === g.id ? "bg-blue-600 text-white" : "hover:bg-slate-800"}`}
                >
                  <span className="block">{g.name}</span>
                  <span className="block text-xs text-slate-300">{g.member_count} member(s) · {g.site_count} site(s)</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded border border-slate-800 bg-slate-900 p-4 lg:col-span-2">
          {!selectedId && <p className="text-sm text-slate-400">Select or create a group.</p>}
          {selectedId && detailQuery.isError && <p role="alert" className="text-sm text-red-400">{errorText(detailQuery.error)}</p>}
          {selectedId && detail && (
            <div>
              <div role="tablist" aria-label="Group sections" className="mb-4 flex flex-wrap gap-1">
                {tabs.map(([key, label]) => (
                  <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
                    className={`rounded px-3 py-1 text-sm ${tab === key ? "bg-blue-600 text-white" : "bg-slate-800 text-slate-300 hover:bg-slate-700"}`}>
                    {label}
                  </button>
                ))}
              </div>

              {tab === "details" && (
                <form onSubmit={(e) => { e.preventDefault(); detailsMutation.mutate(); }} className="space-y-3" aria-label="Group details">
                  <label className="block text-xs text-slate-400">
                    Name
                    <input value={name} onChange={(e) => setName(e.target.value)} disabled={!canManage} maxLength={100}
                      className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                  </label>
                  <label className="block text-xs text-slate-400">
                    Description
                    <textarea value={description} onChange={(e) => setDescription(e.target.value)} disabled={!canManage} maxLength={500}
                      className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                  </label>
                  {canManage && (
                    <div className="flex gap-2">
                      <button type="submit" className="rounded bg-blue-600 px-3 py-1 text-sm text-white">Save</button>
                      <button type="button" onClick={() => window.confirm(`Delete group ${detail.name}? Members lose the access it grants.`) && deleteMutation.mutate()}
                        className="rounded bg-red-900 px-3 py-1 text-sm text-red-100">Delete group</button>
                    </div>
                  )}
                </form>
              )}

              {tab === "members" && (
                <div className="space-y-3">
                  <ul className="max-h-96 space-y-1 overflow-auto" aria-label="Candidate members">
                    {usersQuery.data?.items.map((u) => (
                      <li key={u.id}>
                        <label className="flex items-center gap-2 text-sm">
                          <input type="checkbox" disabled={!canManage} checked={members.includes(u.id)}
                            onChange={(e) => setMembers(e.target.checked ? [...members, u.id] : members.filter((id) => id !== u.id))} />
                          {u.email} <span className="text-xs text-slate-500">{u.full_name}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                  {canManage && <button onClick={() => membersMutation.mutate()} className="rounded bg-blue-600 px-3 py-1 text-sm text-white">Save members</button>}
                </div>
              )}

              {tab === "permissions" && (
                <div className="space-y-4">
                  <p className="text-xs text-slate-400">
                    You can only allow permissions you hold yourself. Deny overrides an allow from any group or role.
                    Permissions marked <em>global users only</em> are not honoured for site-restricted members.
                  </p>
                  {groupByResource(catalogQuery.data ?? []).map(([resource, items]) => (
                    <fieldset key={resource} className="rounded border border-slate-800 p-3">
                      <legend className="px-1 text-sm font-semibold capitalize">{resource.replace(/_/g, " ")}</legend>
                      <div className="space-y-1">
                        {items.map((p) => (
                          <div key={p.code} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                            <span>
                              <span className="font-mono text-xs">{p.code}</span>
                              {!p.site_scoped && <span className="ml-2 text-[10px] text-amber-300">global users only</span>}
                            </span>
                            <select aria-label={`Effect for ${p.code}`} disabled={!canManage}
                              value={permissionState(allow, deny, p.code)}
                              onChange={(e) => setState(p.code, e.target.value as PermissionState)}
                              className="rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-xs">
                              <option value="none">Not set</option>
                              <option value="allow">Allow</option>
                              <option value="deny">Deny</option>
                            </select>
                          </div>
                        ))}
                      </div>
                    </fieldset>
                  ))}
                  {canManage && <button onClick={() => permissionsMutation.mutate()} className="rounded bg-blue-600 px-3 py-1 text-sm text-white">Save permissions</button>}
                </div>
              )}

              {tab === "access" && (
                <div className="space-y-4">
                  <p className="text-xs text-slate-400">
                    Members see only the sites checked below. With &ldquo;selected racks&rdquo; they see only the racks you tick;
                    a site with no racks ticked exposes none. Nothing is granted by default.
                  </p>
                  {sitesQuery.data?.items.map((site) => {
                    const grant = sites.find((s) => s.site_id === site.id);
                    return (
                      <div key={site.id} className="rounded border border-slate-800 p-3">
                        <label className="flex items-center gap-2 text-sm font-semibold">
                          <input type="checkbox" disabled={!canManage} checked={!!grant}
                            onChange={() => setSites(toggleSite(sites, site.id, () => ({
                              site_id: site.id, site_code: site.code, site_name: site.name, rack_scope: "selected", rack_ids: [],
                            })))} />
                          {site.code} · {site.name}
                        </label>
                        {grant && (
                          <div className="mt-2 space-y-2 pl-6">
                            <label className="block text-xs text-slate-400">
                              Rack access
                              <select aria-label={`Rack access for ${site.code}`} disabled={!canManage} value={grant.rack_scope}
                                onChange={(e) => updateSite(site.id, { rack_scope: e.target.value as RackScope, rack_ids: [] })}
                                className="ml-2 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-xs">
                                <option value="selected">Selected racks only</option>
                                <option value="all">All racks in site</option>
                              </select>
                            </label>
                            {grant.rack_scope === "selected" && (
                              <RackPicker siteId={site.id} selected={grant.rack_ids} disabled={!canManage}
                                onChange={(ids) => updateSite(site.id, { rack_ids: ids })} />
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {canManage && <button onClick={() => accessMutation.mutate()} className="rounded bg-blue-600 px-3 py-1 text-sm text-white">Save site &amp; rack access</button>}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
