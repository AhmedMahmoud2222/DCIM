import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { apiFetch } from "@/lib/apiClient";
import { Organization, Page } from "@/types";

export function LocationsPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => apiFetch<Page<Organization>>("/organizations"),
  });

  const createOrganization = useMutation({
    mutationFn: (orgName: string) =>
      apiFetch<Organization>("/organizations", { method: "POST", body: JSON.stringify({ name: orgName }) }),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["organizations"] });
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (name.trim()) createOrganization.mutate(name.trim());
  }

  return (
    <div>
      <h1 className="mb-4 text-lg font-semibold">Location Hierarchy — Organizations</h1>

      <form onSubmit={handleSubmit} className="mb-6 flex gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="New organization name"
          className="w-64 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={createOrganization.isPending}
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          Create
        </button>
      </form>
      {createOrganization.isError && (
        <p className="mb-4 text-sm text-red-400">{(createOrganization.error as Error).message}</p>
      )}

      {isLoading && <p className="text-sm text-slate-400">Loading…</p>}
      {error && <p className="text-sm text-red-400">{(error as Error).message}</p>}

      {data && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-left text-slate-400">
              <th className="pb-2">Name</th>
              <th className="pb-2">Created</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((org) => (
              <tr key={org.id} className="border-b border-slate-900">
                <td className="py-2">{org.name}</td>
                <td className="py-2 text-slate-400">{new Date(org.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
