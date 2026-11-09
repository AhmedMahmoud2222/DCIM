import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { transitionManagedAssetLifecycle } from "@/features/managed-assets/api";

export function LifecycleTransitionAction({
  assetId,
  status,
  assetLabel,
}: {
  assetId: string;
  status: string;
  assetLabel: "rack" | "equipment";
}) {
  const canUpdate = useHasPermission("managed_asset:update_lifecycle");
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => transitionManagedAssetLifecycle(assetId, "installed"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [assetLabel === "rack" ? "racks" : "equipment"] }),
  });

  if (!canUpdate || status !== "planned") return null;

  return (
    <span className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        aria-label={`Mark ${assetLabel} installed`}
        className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary hover:bg-surface-hover disabled:opacity-50"
      >
        {mutation.isPending ? "Saving…" : "Mark installed"}
      </button>
      {mutation.isError && <span role="alert" className="text-xs text-red-400">{(mutation.error as Error).message}</span>}
    </span>
  );
}
