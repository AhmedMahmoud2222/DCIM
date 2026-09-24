import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { compareRevisions } from "@/features/catalog-designer/api";

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function RevisionCompareView() {
  const [searchParams] = useSearchParams();
  const left = searchParams.get("left");
  const right = searchParams.get("right");

  const compareQuery = useQuery({
    queryKey: ["catalog", "revisions", "compare", left, right],
    queryFn: () => compareRevisions(left!, right!),
    enabled: !!left && !!right,
  });

  if (!left || !right) return <p className="text-sm text-red-400">Both ?left= and ?right= revision ids are required.</p>;
  if (compareQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (compareQuery.error) return <p className="text-sm text-red-400">{(compareQuery.error as Error).message}</p>;
  const compare = compareQuery.data;
  if (!compare) return null;

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Compare Revisions</h1>
        <div className="flex gap-4 text-sm">
          <Link to={`/admin/catalog/revisions/${left}`} className="text-blue-400 hover:underline">
            View left
          </Link>
          <Link to={`/admin/catalog/revisions/${right}`} className="text-blue-400 hover:underline">
            View right
          </Link>
        </div>
      </div>

      <div className="mb-6 rounded border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Field changes</h2>
        {compare.field_diffs.length === 0 ? (
          <p className="text-sm italic text-slate-500">No scalar field differences.</p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-slate-400">
                <th className="pb-2">Field</th>
                <th className="pb-2">Left</th>
                <th className="pb-2">Right</th>
              </tr>
            </thead>
            <tbody>
              {compare.field_diffs.map((diff) => (
                <tr key={diff.field} className="border-b border-slate-900">
                  <td className="py-2 font-mono text-xs text-slate-400">{diff.field}</td>
                  <td className="py-2 text-slate-300">{renderValue(diff.left)}</td>
                  <td className="py-2 text-slate-300">{renderValue(diff.right)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="rounded border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Child template changes</h2>
        {compare.child_diffs.length === 0 ? (
          <p className="text-sm italic text-slate-500">No child template differences.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {compare.child_diffs.map((diff, i) => (
              <li key={`${diff.collection}-${diff.stable_key}-${i}`} className="rounded bg-slate-800/50 px-3 py-2">
                <span className="mr-2 rounded bg-slate-700 px-2 py-0.5 text-xs uppercase text-slate-300">{diff.change}</span>
                <span className="text-slate-400">{diff.collection}</span> / <span className="font-mono text-xs">{diff.stable_key}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
