import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { transitionManagedAssetLifecycle } from "@/features/managed-assets/api";
import { useLifecycleSettings } from "@/features/managed-assets/useLifecycleSettings";
import { LifecycleStatus } from "@/features/status/StatusBadges";

function errorMessage(err: unknown, fallback = "Request failed."): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

const REASON_REQUIRED_TARGETS = new Set(["decommissioned", "removed"]);

export function LifecycleTransitionAction({
  assetId,
  assetName,
  status,
  assetLabel,
  onCompleted,
  onStarted,
}: {
  assetId: string;
  assetName: string;
  status: string;
  assetLabel: "rack" | "equipment";
  onCompleted?: (message: string) => void;
  onStarted?: () => void;
}) {
  const canUpdate = useHasPermission("managed_asset:update_lifecycle");
  const queryClient = useQueryClient();
  const { permittedTargets, stateByStatus } = useLifecycleSettings();

  const [confirming, setConfirming] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [completed, setCompleted] = useState(false);
  const targets = permittedTargets(status);
  const [selectedTarget, setSelectedTarget] = useState<string>(targets[0]?.status ?? "");
  const [reason, setReason] = useState<string>("");
  const [validationError, setValidationError] = useState<string | null>(null);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLSpanElement>(null);
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const wasConfirming = useRef(false);
  const queryKey = [assetLabel === "rack" ? "racks" : "equipment"];

  useEffect(() => {
    setCompleted(false);
    setDialogOpen(false);
    setReason("");
    setValidationError(null);
  }, [assetId, status]);

  useEffect(() => {
    if (targets.length > 0 && !targets.some((t) => t.status === selectedTarget)) {
      setSelectedTarget(targets[0].status);
    }
  }, [targets, selectedTarget]);

  const mutation = useMutation({
    mutationFn: (vars?: { toStatus: string; transitionReason?: string }) => {
      if (!vars) {
        return transitionManagedAssetLifecycle(assetId, "installed");
      }
      return transitionManagedAssetLifecycle(assetId, vars.toStatus, vars.transitionReason);
    },
    onMutate: () => onStarted?.(),
    onSuccess: (_data, vars) => {
      const toStatus = vars?.toStatus ?? "installed";
      const targetCfg = stateByStatus(toStatus);
      const targetLabel = targetCfg?.label ?? toStatus;
      setDialogOpen(false);
      setReason("");
      setValidationError(null);
      if (!vars) {
        setCompleted(true);
        onCompleted?.(`${assetName} installation status saved.`);
      } else if (toStatus === "installed") {
        onCompleted?.(`${assetName} installation status saved.`);
      } else {
        onCompleted?.(`${assetName} lifecycle state changed to ${targetLabel}.`);
      }
    },
    onSettled: async (_data, error) => {
      if (!error) setConfirming(false);
      await queryClient.invalidateQueries({ queryKey });
      if (assetLabel === "equipment") {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["equipment", assetId] }),
          queryClient.invalidateQueries({ queryKey: ["managed-assets"] }),
          queryClient.invalidateQueries({ queryKey: ["lifecycle-history", assetId] }),
        ]);
      }
    },
  });

  useEffect(() => {
    if (completed) {
      wasConfirming.current = false;
    } else if (status !== "planned" && mutation.isError && !dialogOpen) {
      wasConfirming.current = false;
      errorRef.current?.focus();
    } else if (confirming) {
      wasConfirming.current = true;
      confirmRef.current?.focus();
    } else if (wasConfirming.current) {
      wasConfirming.current = false;
      triggerRef.current?.focus();
    }
  }, [completed, confirming, dialogOpen, mutation.isError, status]);

  if (status === "reserved") {
    if (assetLabel !== "equipment") return null;
    return (
      <span
        data-testid={`legacy-reserved-explanation-${assetId}`}
        className="text-xs text-fg-secondary"
      >
        Legacy &lsquo;reserved&rsquo; state — read-only until a separate migration policy is approved.
      </span>
    );
  }

  if (!canUpdate) return null;

  const canQuickInstall = status === "planned" && targets.some((t) => t.status === "installed");
  const showFullLifecycle = assetLabel === "equipment" && targets.length > 0;
  const activeTargetConfig = stateByStatus(selectedTarget);
  const currentConfig = stateByStatus(status);
  const reasonRequired = REASON_REQUIRED_TARGETS.has(selectedTarget);

  const handleDialogConfirm = () => {
    setValidationError(null);
    const trimmed = reason.trim();
    if (reasonRequired && !trimmed) {
      setValidationError(
        `A reason is required when transitioning to ${activeTargetConfig?.label ?? selectedTarget}.`,
      );
      return;
    }
    mutation.mutate({
      toStatus: selectedTarget,
      transitionReason: trimmed ? trimmed : undefined,
    });
  };

  return (
    <span
      ref={wrapperRef}
      tabIndex={-1}
      data-testid="lifecycle-action"
      className="inline-flex flex-col items-start gap-1"
    >
      <span className="inline-flex flex-wrap items-center gap-1.5">
        {completed || !canQuickInstall || dialogOpen ? null : !confirming ? (
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
            <span role="status" className="text-xs text-amber-300">
              Installation cannot be changed back to planned.
            </span>
            <span className="flex gap-1">
              <button
                ref={confirmRef}
                type="button"
                onClick={() => mutation.mutate(undefined)}
                disabled={mutation.isPending}
                className="rounded-sm bg-blue-700 px-2 py-1 text-xs text-white disabled:opacity-50"
              >
                {mutation.isPending ? "Saving…" : `Confirm install ${assetName}`}
              </button>
              <button
                type="button"
                onClick={() => {
                  mutation.reset();
                  setConfirming(false);
                }}
                disabled={mutation.isPending}
                className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary disabled:opacity-50"
              >
                Cancel
              </button>
            </span>
          </span>
        )}

        {showFullLifecycle && !confirming && !dialogOpen && !completed && (
          <button
            type="button"
            onClick={() => {
              mutation.reset();
              setValidationError(null);
              setReason("");
              setSelectedTarget(targets[0]?.status ?? "");
              setDialogOpen(true);
            }}
            aria-label={`Change lifecycle state: ${assetName}`}
            className="rounded-sm border border-line-subtle bg-surface-raised px-2 py-1 text-xs font-medium text-fg-primary hover:bg-surface-hover"
          >
            Change Lifecycle State
          </button>
        )}
      </span>

      {showFullLifecycle && dialogOpen && (
        <div
          role="dialog"
          aria-modal="false"
          aria-label={`Change lifecycle state for ${assetName}`}
          className="mt-1 w-full min-w-[18rem] max-w-md rounded-md border border-line-default bg-surface-panel p-3 shadow-sm"
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-xs font-semibold text-fg-primary">
              Confirm Lifecycle Transition — {assetName}
            </span>
            <LifecycleStatus
              value={status}
              size="sm"
              labelOverride={currentConfig?.label}
              toneOverride={currentConfig?.badge_color}
            />
          </div>

          <div className="space-y-2">
            <div>
              <label
                htmlFor={`lifecycle-target-${assetId}`}
                className="mb-1 block text-xs font-medium text-fg-secondary"
              >
                Target lifecycle state
              </label>
              <select
                id={`lifecycle-target-${assetId}`}
                aria-label="Target lifecycle state"
                value={selectedTarget}
                onChange={(e) => {
                  setSelectedTarget(e.target.value);
                  setValidationError(null);
                  mutation.reset();
                }}
                disabled={mutation.isPending}
                className="w-full rounded border border-line-default bg-surface-input px-2.5 py-1.5 text-xs text-fg-primary"
              >
                {targets.map((target) => (
                  <option key={target.status} value={target.status}>
                    {target.label}
                  </option>
                ))}
              </select>
            </div>

            {activeTargetConfig?.description && (
              <p className="text-[11px] text-fg-secondary">{activeTargetConfig.description}</p>
            )}

            <div>
              <label
                htmlFor={`lifecycle-reason-${assetId}`}
                className="mb-1 block text-xs font-medium text-fg-secondary"
              >
                {reasonRequired
                  ? "Reason (required for decommissioning and removal)"
                  : "Reason (optional)"}
              </label>
              <textarea
                id={`lifecycle-reason-${assetId}`}
                aria-label="Reason for lifecycle transition"
                required={reasonRequired}
                rows={2}
                maxLength={500}
                value={reason}
                onChange={(e) => {
                  setReason(e.target.value);
                  if (validationError) setValidationError(null);
                }}
                disabled={mutation.isPending}
                placeholder={
                  reasonRequired
                    ? "Enter mandatory reason for decommissioning or removal…"
                    : "Optional transition note…"
                }
                className="w-full rounded border border-line-default bg-surface-input px-2.5 py-1.5 text-xs text-fg-primary"
              />
            </div>

            {validationError && (
              <p role="alert" className="text-xs text-red-400">
                {validationError}
              </p>
            )}

            <div className="flex items-center gap-2 pt-1">
              <button
                type="button"
                disabled={mutation.isPending}
                onClick={handleDialogConfirm}
                aria-label={`Confirm lifecycle transition for ${assetName}`}
                className="rounded-sm bg-blue-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-600 disabled:opacity-50"
              >
                {mutation.isPending ? "Saving…" : "Confirm transition"}
              </button>
              <button
                type="button"
                disabled={mutation.isPending}
                onClick={() => {
                  setDialogOpen(false);
                  setReason("");
                  setValidationError(null);
                  mutation.reset();
                }}
                className="rounded-sm border border-line-subtle px-2.5 py-1 text-xs text-fg-primary hover:bg-surface-hover disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {mutation.isError && (
        <span className="inline-flex items-center gap-2">
          <span ref={errorRef} tabIndex={-1} role="alert" className="text-xs text-red-400">
            {errorMessage(mutation.error, (mutation.error as Error).message)}
          </span>
          {status !== "planned" && !dialogOpen && (
            <button
              type="button"
              aria-label={`Dismiss message: ${assetName}`}
              onClick={() => {
                mutation.reset();
                setConfirming(false);
                wrapperRef.current?.focus();
              }}
              className="rounded-sm border border-line-subtle px-2 py-1 text-xs text-fg-primary hover:bg-surface-hover"
            >
              Dismiss
            </button>
          )}
        </span>
      )}
    </span>
  );
}
