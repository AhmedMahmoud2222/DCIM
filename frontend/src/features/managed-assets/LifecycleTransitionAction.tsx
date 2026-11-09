import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { transitionManagedAssetLifecycle } from "@/features/managed-assets/api";

export function LifecycleTransitionAction({
  assetId,
  assetName,
  status,
  assetLabel,
}: {
  assetId: string;
  assetName: string;
  status: string;
  assetLabel: "rack" | "equipment";
}) {
  const canUpdate = useHasPermission("managed_asset:update_lifecycle");
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const queryKey = [assetLabel === "rack" ? "racks" : "equipment"];
  const mutation = useMutation({
    mutationFn: () => transitionManagedAssetLifecycle(assetId, "installed"),
    onSettled: () => {
      setConfirming(false);
      return queryClient.invalidateQueries({ queryKey });
    },
  });

  if (!canUpdate || status !== "planned") return null;

  return (
    <span className="inline-flex flex-col items-start gap-1">
      {!confirming ? (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          aria-label={`Mark installed: ${assetName}`}
          className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary hover:bg-surface-hover"
        >
          Mark installed
        </button>
      ) : (
        <span className="inline-flex flex-col gap-1">
          <span role="status" className="text-xs text-amber-300">Installation cannot be changed back to planned.</span>
          <span className="flex gap-1">
            <button type="button" onClick={() => mutation.mutate()} disabled={mutation.isPending} className="rounded-sm bg-blue-700 px-2 py-1 text-xs text-white disabled:opacity-50">
              {mutation.isPending ? "Saving…" : `Confirm install ${assetName}`}
            </button>
            <button type="button" onClick={() => setConfirming(false)} disabled={mutation.isPending} className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary disabled:opacity-50">
              Cancel
            </button>
          </span>
        </span>
      )}
      {mutation.isError && <span role="alert" className="text-xs text-red-400">{(mutation.error as Error).message}</span>}
    </span>
  );
}
