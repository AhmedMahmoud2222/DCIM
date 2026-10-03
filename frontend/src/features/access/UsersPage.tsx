import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { ApiError } from "@/lib/apiClient";

import {
  AdminUser,
  createUser,
  deleteUser,
  getEffectiveAccess,
  listGroups,
  listUsers,
  updateUser,
} from "./api";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Request failed.";
}

function GroupChecklist({
  groups,
  selected,
  onChange,
  disabled,
}: {
  groups: { id: string; name: string }[];
  selected: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset className="space-y-1" disabled={disabled}>
      <legend className="mb-1 text-xs text-slate-400">Groups</legend>
      {groups.length === 0 && <p className="text-xs text-slate-500">No groups defined yet.</p>}
      {groups.map((g) => (
        <label key={g.id} className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={selected.includes(g.id)}
            onChange={(e) => onChange(e.target.checked ? [...selected, g.id] : selected.filter((id) => id !== g.id))}
          />
          {g.name}
        </label>
      ))}
    </fieldset>
  );
}

export function EffectiveAccessPanel({ userId }: { userId: string }) {
  const query = useQuery({ queryKey: ["effective-access", userId], queryFn: () => getEffectiveAccess(userId) });
  if (query.isLoading) return <p className="text-sm text-slate-400">Loading effective access…</p>;
  if (query.isError || !query.data) return <p className="text-sm text-red-400">{errorText(query.error)}</p>;
  const access = query.data;
  const codes = Object.keys(access.permissions);
  return (
    <section aria-label="Effective access" className="space-y-3 text-sm">
      <p className="text-slate-300">
        {access.unrestricted
          ? "Unrestricted: holds a global role, so all sites and racks are visible."
          : "Site-restricted: only the sites and racks listed below are visible."}
      </p>
      {access.role_names.length > 0 && <p className="text-slate-400">Roles: {access.role_names.join(", ")}</p>}
      <div>
        <h3 className="mb-1 font-semibold text-slate-300">Effective permissions ({codes.length})</h3>
        {codes.length === 0 && <p className="text-slate-500">None. Access is denied by default.</p>}
        <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          {codes.map((code) => (
            <li key={code} className="rounded-sm bg-slate-800 px-2 py-1">
              <span className="font-mono text-xs">{code}</span>
              <span className="ml-2 text-xs text-slate-400">via {access.permissions[code].join(", ")}</span>
            </li>
          ))}
        </ul>
      </div>
      {access.denied_permissions.length > 0 && (
        <div>
          <h3 className="mb-1 font-semibold text-red-300">Explicitly denied</h3>
          <p className="font-mono text-xs">{access.denied_permissions.join(", ")}</p>
        </div>
      )}
      {access.inactive_permissions.length > 0 && (
        <div>
          <h3 className="mb-1 font-semibold text-amber-300">Granted but inactive</h3>
          <p className="text-xs text-slate-400">
            These endpoints are not site-scoped yet, so a restricted user cannot use them:
          </p>
          <p className="font-mono text-xs">{access.inactive_permissions.join(", ")}</p>
        </div>
      )}
      {!access.unrestricted && (
        <div>
          <h3 className="mb-1 font-semibold text-slate-300">Sites ({access.sites.length})</h3>
          {access.sites.length === 0 && <p className="text-slate-500">No site access.</p>}
          <ul className="space-y-1">
            {access.sites.map((s) => (
              <li key={s.site_id} className="rounded-sm bg-slate-800 px-2 py-1">
                {s.code} · {s.name} ·{" "}
                {s.rack_scope === "all" ? "all racks" : `${s.rack_ids.length} selected rack(s)`}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function UsersPage() {
  const queryClient = useQueryClient();
  const canManage = useHasPermission("user:manage");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ email: "", full_name: "", password: "", group_ids: [] as string[] });
  const [editName, setEditName] = useState("");
  const [editPassword, setEditPassword] = useState("");
  const [editGroups, setEditGroups] = useState<string[]>([]);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const usersQuery = useQuery({ queryKey: ["admin-users", search], queryFn: () => listUsers(search || undefined) });
  const groupsQuery = useQuery({ queryKey: ["admin-groups"], queryFn: listGroups });
  const groups = groupsQuery.data?.items ?? [];

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    queryClient.invalidateQueries({ queryKey: ["admin-groups"] });
    queryClient.invalidateQueries({ queryKey: ["effective-access"] });
  };
  const onError = (e: unknown) => setMessage({ kind: "error", text: errorText(e) });

  const createMutation = useMutation({
    mutationFn: () => createUser({ ...form, is_active: true }),
    onSuccess: () => {
      setShowCreate(false);
      setForm({ email: "", full_name: "", password: "", group_ids: [] });
      setMessage({ kind: "ok", text: "User created." });
      refresh();
    },
    onError,
  });
  const updateMutation = useMutation({
    mutationFn: (vars: { id: string; body: Parameters<typeof updateUser>[1] }) => updateUser(vars.id, vars.body),
    onSuccess: (user) => {
      setSelected(user);
      setEditPassword("");
      setMessage({ kind: "ok", text: "User updated." });
      refresh();
    },
    onError,
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: () => {
      setSelected(null);
      setMessage({ kind: "ok", text: "User deleted." });
      refresh();
    },
    onError,
  });

  function select(user: AdminUser) {
    setSelected(user);
    setEditName(user.full_name);
    setEditGroups(user.groups.map((g) => g.id));
    setEditPassword("");
    setMessage(null);
  }

  function submitCreate(e: FormEvent) {
    e.preventDefault();
    setMessage(null);
    createMutation.mutate();
  }

  function saveSelected(e: FormEvent) {
    e.preventDefault();
    if (!selected) return;
    setMessage(null);
    updateMutation.mutate({
      id: selected.id,
      body: {
        full_name: editName,
        group_ids: editGroups,
        ...(editPassword ? { password: editPassword } : {}),
      },
    });
  }

  function confirmDelete() {
    if (selected && window.confirm(`Delete ${selected.email}? This cannot be undone. Consider deactivating instead.`)) {
      deleteMutation.mutate(selected.id);
    }
  }

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Users</h1>
      <p className="mb-4 max-w-2xl text-sm text-slate-400">
        Create and manage users and assign them to groups. Group membership decides which sites and racks a user can see.
        All rules are enforced by the server.
      </p>
      {message && (
        <p role={message.kind === "error" ? "alert" : "status"} className={`mb-3 text-sm ${message.kind === "error" ? "text-red-400" : "text-green-400"}`}>
          {message.text}
        </p>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4 lg:col-span-1">
          <div className="mb-3 flex items-center gap-2">
            <input
              aria-label="Search users"
              placeholder="Search email or name"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="min-w-0 flex-1 rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm"
            />
            {canManage && (
              <button onClick={() => setShowCreate((v) => !v)} className="rounded-sm bg-slate-800 px-2 py-1 text-xs hover:bg-slate-700">
                + New user
              </button>
            )}
          </div>
          {showCreate && canManage && (
            <form onSubmit={submitCreate} className="mb-4 space-y-2 rounded-sm border border-slate-800 p-3" aria-label="Create user">
              <label className="block text-xs text-slate-400">
                Email
                <input type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })}
                  className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
              </label>
              <label className="block text-xs text-slate-400">
                Full name
                <input required maxLength={200} value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                  className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
              </label>
              <label className="block text-xs text-slate-400">
                Initial password (12+ characters)
                <input type="password" required minLength={12} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })}
                  className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
              </label>
              <GroupChecklist groups={groups} selected={form.group_ids} onChange={(ids) => setForm({ ...form, group_ids: ids })} />
              <button type="submit" disabled={createMutation.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
                Create user
              </button>
            </form>
          )}
          {usersQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
          {usersQuery.isError && <p role="alert" className="text-sm text-red-400">{errorText(usersQuery.error)}</p>}
          <ul className="space-y-1" aria-label="Users">
            {usersQuery.data?.items.map((u) => (
              <li key={u.id}>
                <button
                  onClick={() => select(u)}
                  aria-pressed={selected?.id === u.id}
                  className={`w-full rounded-sm px-2 py-2 text-left text-sm ${selected?.id === u.id ? "bg-blue-600 text-white" : "hover:bg-slate-800"}`}
                >
                  <span className="block break-all">{u.email}</span>
                  <span className="block text-xs text-slate-300">
                    {u.full_name} · {u.is_active ? "Active" : "Inactive"} · {u.is_restricted ? "Restricted" : "Unrestricted"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4 lg:col-span-2">
          {!selected && <p className="text-sm text-slate-400">Select a user to view details and effective access.</p>}
          {selected && (
            <div className="space-y-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="break-all text-base font-semibold">{selected.email}</h2>
                {canManage && (
                  <div className="flex gap-2">
                    <button
                      onClick={() => updateMutation.mutate({ id: selected.id, body: { is_active: !selected.is_active } })}
                      className="rounded-sm bg-slate-800 px-2 py-1 text-xs hover:bg-slate-700"
                    >
                      {selected.is_active ? "Deactivate" : "Activate"}
                    </button>
                    <button onClick={confirmDelete} className="rounded-sm bg-red-900 px-2 py-1 text-xs text-red-100 hover:bg-red-800">
                      Delete
                    </button>
                  </div>
                )}
              </div>
              <form onSubmit={saveSelected} className="space-y-3" aria-label="Edit user">
                <label className="block text-xs text-slate-400">
                  Full name
                  <input value={editName} onChange={(e) => setEditName(e.target.value)} disabled={!canManage} maxLength={200}
                    className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                </label>
                <GroupChecklist groups={groups} selected={editGroups} onChange={setEditGroups} disabled={!canManage} />
                {canManage && (
                  <>
                    <label className="block text-xs text-slate-400">
                      Reset password (optional, 12+ characters)
                      <input type="password" minLength={12} value={editPassword} onChange={(e) => setEditPassword(e.target.value)}
                        className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                    </label>
                    <button type="submit" disabled={updateMutation.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
                      Save changes
                    </button>
                  </>
                )}
              </form>
              <EffectiveAccessPanel userId={selected.id} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
