import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

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
  const [completed, setCompleted] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const completionRef = useRef<HTMLSpanElement>(null);
  const errorRef = useRef<HTMLSpanElement>(null);
  const wasConfirming = useRef(false);
  const queryKey = [assetLabel === "rack" ? "racks" : "equipment"];
  const mutation = useMutation({
    mutationFn: () => transitionManagedAssetLifecycle(assetId, "installed"),
    onSuccess: () => setCompleted(true),
    onSettled: (_data, error) => {
      if (!error) setConfirming(false);
      return queryClient.invalidateQueries({ queryKey });
    },
  });

  useEffect(() => {
    if (completed) {
      wasConfirming.current = false;
      completionRef.current?.focus();
    } else if (status !== "planned" && mutation.isError) {
      wasConfirming.current = false;
      errorRef.current?.focus();
    } else if (confirming) {
      wasConfirming.current = true;
      confirmRef.current?.focus();
    } else if (wasConfirming.current) {
      wasConfirming.current = false;
      triggerRef.current?.focus();
    }
  }, [completed, confirming, mutation.isError, status]);

  if (!canUpdate) return null;

  return (
    <span className="inline-flex flex-col items-start gap-1">
      {completed ? (
        <span ref={completionRef} tabIndex={-1} role="status" className="text-xs text-green-300">Installation status saved.</span>
      ) : status !== "planned" ? null : !confirming ? (
        <button
          type="button"
          ref={triggerRef}
          onClick={() => {
            mutation.reset();
            setConfirming(true);
          }}
          aria-label={`Mark installed: ${assetName}`}
          className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary hover:bg-surface-hover"
        >
          Mark installed
        </button>
      ) : (
        <span className="inline-flex flex-col gap-1">
          <span role="status" className="text-xs text-amber-300">Installation cannot be changed back to planned.</span>
          <span className="flex gap-1">
            <button ref={confirmRef} type="button" onClick={() => mutation.mutate()} disabled={mutation.isPending} className="rounded-sm bg-blue-700 px-2 py-1 text-xs text-white disabled:opacity-50">
              {mutation.isPending ? "Saving…" : `Confirm install ${assetName}`}
            </button>
            <button type="button" onClick={() => {
              mutation.reset();
              setConfirming(false);
            }} disabled={mutation.isPending} className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary disabled:opacity-50">
              Cancel
            </button>
          </span>
        </span>
      )}
      {mutation.isError && <span ref={errorRef} tabIndex={-1} role="alert" className="text-xs text-red-400">{(mutation.error as Error).message}</span>}
    </span>
  );
}
