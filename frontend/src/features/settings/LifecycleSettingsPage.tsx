import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/layout/PageHeader";
import {
  getLifecycleSettings,
  restoreDefaultLifecycleSettings,
  updateLifecycleSettings,
} from "@/features/managed-assets/api";
import { useIsLifecycleAdministrator } from "@/features/auth/useAuthorization";
import { LIFECYCLE_SETTINGS_QUERY_KEY } from "@/features/managed-assets/useLifecycleSettings";
import { LifecycleStatus } from "@/features/status/StatusBadges";
import { ApiError } from "@/lib/apiClient";
import {
  LIFECYCLE_BADGE_COLORS,
  LifecycleBadgeColor,
  LifecycleSettingsResponse,
  LifecycleStateConfig,
} from "@/types";

function errorMessage(err: unknown, fallback = "Request failed."): string {
  if (err instanceof ApiError && err.status === 409) {
    return "Lifecycle settings were modified by another administrator — reload the latest configuration and try again.";
  }
  return err instanceof Error && err.message ? err.message : fallback;
}

const BADGE_COLOR_LABELS: Record<LifecycleBadgeColor, string> = {
  neutral: "Neutral (Gray)",
  info: "Info (Blue)",
  ok: "OK / Active (Green)",
  caution: "Caution / Maintenance (Amber)",
  serious: "Serious / Retired (Orange)",
  danger: "Danger / Removed (Red)",
};

export function LifecycleSettingsPage() {
  const queryClient = useQueryClient();
  const isAdmin = useIsLifecycleAdministrator();

  const settingsQuery = useQuery<LifecycleSettingsResponse>({
    queryKey: LIFECYCLE_SETTINGS_QUERY_KEY,
    queryFn: async () => {
      return await getLifecycleSettings();
    },
  });

  const [draftStates, setDraftStates] = useState<LifecycleStateConfig[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  // Unsaved edits are never replaced silently: when the server copy changes under a dirty draft we
  // keep the draft and offer an explicit reload/discard instead.
  const [dirty, setDirty] = useState(false);
  const [serverChanged, setServerChanged] = useState(false);
  const [reloadError, setReloadError] = useState<string | null>(null);
  const baseVersion = useRef<number | undefined>(undefined);
  const draftRevision = useRef(0);

  useEffect(() => {
    const server = settingsQuery.data;
    if (!server?.states) return;
    const sorted = [...server.states].sort((a, b) => a.display_order - b.display_order);
    if (baseVersion.current === undefined) {
      // First load: the draft is based on this server version regardless of any prior state.
      baseVersion.current = server.version;
      setDraftStates(sorted);
      return;
    }
    if (server.version !== baseVersion.current) {
      if (dirty) {
        setServerChanged(true);
        return;
      }
      baseVersion.current = server.version;
      setDraftStates(sorted);
      return;
    }
    if (!dirty) setDraftStates(sorted);
  }, [settingsQuery.data, dirty]);

  const saveMutation = useMutation({
    mutationFn: async (states: LifecycleStateConfig[]) => {
      const base = baseVersion.current;
      if (base === undefined) {
        throw new Error("Lifecycle settings have not loaded yet; reload and try again.");
      }
      const submittedRevision = draftRevision.current;
      // Precondition is the version the draft was based on, never the latest fetched version.
      const updated = await updateLifecycleSettings(states, base);
      return { updated, submittedRevision };
    },
    onSuccess: ({ updated, submittedRevision }) => {
      queryClient.setQueryData(LIFECYCLE_SETTINGS_QUERY_KEY, updated);
      baseVersion.current = updated.version;
      if (draftRevision.current === submittedRevision) {
        setDirty(false);
      }
      setServerChanged(false);
      setValidationError(null);
      setNotice("Lifecycle settings saved.");
    },
  });

  const restoreMutation = useMutation({
    mutationFn: async () => {
      const base = baseVersion.current;
      if (base === undefined) {
        throw new Error("Lifecycle settings have not loaded yet; reload and try again.");
      }
      const submittedRevision = draftRevision.current;
      const restored = await restoreDefaultLifecycleSettings(base);
      return { restored, submittedRevision };
    },
    onSuccess: ({ restored, submittedRevision }) => {
      queryClient.setQueryData(LIFECYCLE_SETTINGS_QUERY_KEY, restored);
      baseVersion.current = restored.version;
      if (draftRevision.current === submittedRevision) {
        setDraftStates([...restored.states].sort((a, b) => a.display_order - b.display_order));
        setDirty(false);
      }
      setServerChanged(false);
      setValidationError(null);
      setNotice("Default lifecycle settings restored.");
    },
  });

  const isMutating = saveMutation.isPending || restoreMutation.isPending;

  const handleReload = async () => {
    const result = await settingsQuery.refetch();
    if (result.isError || !result.data?.states) {
      // Keep the draft and the conflict state; only a fresh successful fetch may replace them.
      setReloadError("Could not reload the latest lifecycle settings. Your edits are kept; try again.");
      return;
    }
    setReloadError(null);
    baseVersion.current = result.data.version;
    draftRevision.current += 1;
    setDraftStates([...result.data.states].sort((a, b) => a.display_order - b.display_order));
    setDirty(false);
    setServerChanged(false);
    setValidationError(null);
    setNotice(null);
    saveMutation.reset();
    restoreMutation.reset();
  };

  const isConflict =
    (saveMutation.error ?? restoreMutation.error) instanceof ApiError &&
    ((saveMutation.error ?? restoreMutation.error) as ApiError).status === 409;

  if (!isAdmin) {
    return (
      <div>
        <nav aria-label="Settings breadcrumb" className="mb-2 text-xs text-fg-secondary">
          Settings / Asset Management / Lifecycle Settings
        </nav>
        <PageHeader
          title="Lifecycle Settings"
          description="Configure display labels, descriptions, badge colors, display order, and allowed transitions for the six equipment lifecycle states."
        />
        <div className="rounded-lg border border-line-default bg-surface-panel p-6">
          <h2 className="text-base font-semibold text-fg-primary">Access restricted</h2>
          <p className="mt-1 text-sm text-fg-secondary">
            Administrator role and <code>managed_asset:manage</code> permission are required to modify lifecycle
            settings.
          </p>
        </div>
      </div>
    );
  }

  const data = settingsQuery.data;
  if (!data) {
    return (
      <div>
        <PageHeader title="Lifecycle Settings" description="Loading the current lifecycle configuration…" />
        {settingsQuery.isError ? (
          <p role="alert" className="rounded-md border border-tone-danger-fg bg-tone-danger-bg px-3 py-2 text-sm text-tone-danger-fg">
            Could not load lifecycle settings.
            <button
              type="button"
              onClick={() => void settingsQuery.refetch()}
              className="ml-3 rounded-sm border border-tone-danger-fg px-2 py-0.5 text-xs font-medium"
            >
              Retry
            </button>
          </p>
        ) : (
          <p role="status" className="text-sm text-fg-secondary">Loading lifecycle settings…</p>
        )}
      </div>
    );
  }

  const updateStateField = <K extends keyof LifecycleStateConfig>(
    statusKey: string,
    field: K,
    value: LifecycleStateConfig[K],
  ) => {
    setNotice(null);
    setValidationError(null);
    draftRevision.current += 1;
    setDirty(true);
    setDraftStates((prev) =>
      prev.map((item) => (item.status === statusKey ? { ...item, [field]: value } : item)),
    );
  };

  const moveOrder = (index: number, direction: -1 | 1) => {
    const targetIndex = index + direction;
    if (targetIndex < 0 || targetIndex >= draftStates.length) return;
    setNotice(null);
    setValidationError(null);
    draftRevision.current += 1;
    setDirty(true);
    setDraftStates((prev) => {
      const next = [...prev];
      const temp = next[index];
      next[index] = next[targetIndex];
      next[targetIndex] = temp;
      return next.map((item, idx) => ({ ...item, display_order: idx + 1 }));
    });
  };

  const toggleAllowedTransition = (fromStatus: string, toStatus: string) => {
    setNotice(null);
    setValidationError(null);
    draftRevision.current += 1;
    setDirty(true);
    setDraftStates((prev) =>
      prev.map((item) => {
        if (item.status !== fromStatus) return item;
        const exists = item.allowed_transitions.includes(toStatus);
        const updatedTransitions = exists
          ? item.allowed_transitions.filter((t) => t !== toStatus)
          : [...item.allowed_transitions, toStatus];
        return { ...item, allowed_transitions: updatedTransitions };
      }),
    );
  };

  const handleSave = (e: FormEvent) => {
    e.preventDefault();
    if (isMutating) return;
    setNotice(null);
    setValidationError(null);
    for (const s of draftStates) {
      if (!s.label.trim()) {
        setValidationError(`Display label for '${s.status}' cannot be blank.`);
        return;
      }
    }
    saveMutation.mutate(draftStates);
  };

  const mandatoryMap = data.mandatory_allowed_transitions;

  return (
    <div>
      <nav aria-label="Settings breadcrumb" className="mb-2 text-xs text-fg-secondary">
        <span>Settings</span> <span aria-hidden="true">→</span> <span>Asset Management</span>{" "}
        <span aria-hidden="true">→</span> <span className="font-medium text-fg-primary">Lifecycle Settings</span>
      </nav>

      <PageHeader
        title="Lifecycle Settings"
        description="Configure display labels, descriptions, badge colors, display order, and allowed transitions for the six equipment lifecycle states. Internal status identifiers are fixed."
        actions={
          <button
            type="button"
            disabled={isMutating || serverChanged}
            onClick={() => restoreMutation.mutate()}
            className="rounded-md border border-line-default bg-surface-raised px-3 py-1.5 text-sm font-medium text-fg-primary hover:bg-surface-hover disabled:opacity-50"
          >
            {restoreMutation.isPending ? "Restoring…" : "Restore Default Settings"}
          </button>
        }
      />

      <div
        className="mb-4 rounded-md border border-line-subtle bg-surface-panel px-4 py-3 text-xs text-fg-secondary"
        data-testid="lifecycle-policy-note"
      >
        The six selectable lifecycle states (<code>planned</code>, <code>installed</code>, <code>active</code>,{" "}
        <code>maintenance</code>, <code>decommissioned</code>, <code>removed</code>) cannot be added to or deleted.
        Legacy <code>reserved</code> records remain readable as a read-only state and are excluded from selection.
      </div>

      {notice && (
        <p role="status" className="mb-4 rounded-md border border-tone-ok-fg bg-tone-ok-bg px-3 py-2 text-sm text-tone-ok-fg">
          {notice}
        </p>
      )}

      {(validationError || saveMutation.isError || restoreMutation.isError) && (
        <p role="alert" className="mb-4 rounded-md border border-tone-danger-fg bg-tone-danger-bg px-3 py-2 text-sm text-tone-danger-fg">
          {validationError ??
            errorMessage(
              saveMutation.error ?? restoreMutation.error,
              "Could not update lifecycle settings.",
            )}
          {isConflict && (
            <button
              type="button"
              onClick={() => void handleReload()}
              className="ml-3 rounded-sm border border-tone-danger-fg px-2 py-0.5 text-xs font-medium"
            >
              Reload latest configuration
            </button>
          )}
        </p>
      )}

      {reloadError && (
        <p role="alert" className="mb-4 rounded-md border border-tone-danger-fg bg-tone-danger-bg px-3 py-2 text-sm text-tone-danger-fg">
          {reloadError}
        </p>
      )}

      {serverChanged && !isConflict && (
        <p role="alert" className="mb-4 rounded-md border border-line-default bg-surface-panel px-3 py-2 text-sm text-fg-primary">
          Another administrator changed the lifecycle settings while you were editing. Your unsaved edits are kept;
          saving is disabled until you reload.
          <button
            type="button"
            onClick={() => void handleReload()}
            className="ml-3 rounded-sm border border-line-default px-2 py-0.5 text-xs font-medium"
          >
            Discard my edits and reload
          </button>
        </p>
      )}

      <form onSubmit={handleSave} className="space-y-4">
        <fieldset disabled={isMutating} className="space-y-4">
          {draftStates.map((stateItem, index) => {
            const mandatoryTargets = mandatoryMap[stateItem.status] ?? [];
            return (
              <div
                key={stateItem.status}
                data-testid={`lifecycle-state-card-${stateItem.status}`}
                className="rounded-lg border border-line-default bg-surface-panel p-4"
              >
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-line-subtle pb-3">
                  <div className="flex items-center gap-3">
                    <span className="rounded bg-surface-raised px-2 py-0.5 font-mono text-xs text-fg-secondary">
                      #{stateItem.display_order} · {stateItem.status}
                    </span>
                    <LifecycleStatus
                      value={stateItem.status}
                      labelOverride={stateItem.label}
                      toneOverride={stateItem.badge_color}
                      description={stateItem.description}
                    />
                  </div>

                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      disabled={index === 0 || isMutating}
                      onClick={() => moveOrder(index, -1)}
                      aria-label={`Move ${stateItem.status} up`}
                      className="rounded border border-line-default bg-surface-raised px-2 py-1 text-xs text-fg-primary hover:bg-surface-hover disabled:opacity-40"
                    >
                      Move up
                    </button>
                    <button
                      type="button"
                      disabled={index === draftStates.length - 1 || isMutating}
                      onClick={() => moveOrder(index, 1)}
                      aria-label={`Move ${stateItem.status} down`}
                      className="rounded border border-line-default bg-surface-raised px-2 py-1 text-xs text-fg-primary hover:bg-surface-hover disabled:opacity-40"
                    >
                      Move down
                    </button>
                  </div>
                </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div>
                  <label
                    htmlFor={`label-${stateItem.status}`}
                    className="mb-1 block text-xs font-medium text-fg-secondary"
                  >
                    Display Label
                  </label>
                  <input
                    id={`label-${stateItem.status}`}
                    aria-label={`Display label for ${stateItem.status}`}
                    value={stateItem.label}
                    onChange={(e) => updateStateField(stateItem.status, "label", e.target.value)}
                    maxLength={64}
                    className="w-full rounded border border-line-default bg-surface-input px-2.5 py-1.5 text-sm text-fg-primary"
                  />
                </div>

                <div>
                  <label
                    htmlFor={`color-${stateItem.status}`}
                    className="mb-1 block text-xs font-medium text-fg-secondary"
                  >
                    Badge Color
                  </label>
                  <select
                    id={`color-${stateItem.status}`}
                    aria-label={`Badge color for ${stateItem.status}`}
                    value={stateItem.badge_color}
                    onChange={(e) =>
                      updateStateField(
                        stateItem.status,
                        "badge_color",
                        e.target.value as LifecycleBadgeColor,
                      )
                    }
                    className="w-full rounded border border-line-default bg-surface-input px-2.5 py-1.5 text-sm text-fg-primary"
                  >
                    {LIFECYCLE_BADGE_COLORS.map((color) => (
                      <option key={color} value={color}>
                        {BADGE_COLOR_LABELS[color]}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label
                    htmlFor={`desc-${stateItem.status}`}
                    className="mb-1 block text-xs font-medium text-fg-secondary"
                  >
                    Description
                  </label>
                  <input
                    id={`desc-${stateItem.status}`}
                    aria-label={`Description for ${stateItem.status}`}
                    value={stateItem.description}
                    onChange={(e) => updateStateField(stateItem.status, "description", e.target.value)}
                    maxLength={500}
                    className="w-full rounded border border-line-default bg-surface-input px-2.5 py-1.5 text-sm text-fg-primary"
                  />
                </div>
              </div>

              <div className="mt-3 pt-2">
                <span className="mb-1.5 block text-xs font-medium text-fg-secondary">
                  Allowed Transitions from {stateItem.label} (constrained by mandatory backend rules)
                </span>
                {mandatoryTargets.length === 0 ? (
                  <p className="text-xs italic text-fg-secondary">
                    Terminal state — outgoing transitions are forbidden by mandatory backend restrictions.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-3">
                    {mandatoryTargets.map((targetStatus) => {
                      const targetState = draftStates.find((s) => s.status === targetStatus);
                      const checked = stateItem.allowed_transitions.includes(targetStatus);
                      return (
                        <label
                          key={targetStatus}
                          className="inline-flex items-center gap-1.5 text-xs text-fg-primary"
                        >
                          <input
                            type="checkbox"
                            aria-label={`Allow transition from ${stateItem.status} to ${targetStatus}`}
                            checked={checked}
                            onChange={() => toggleAllowedTransition(stateItem.status, targetStatus)}
                          />
                          <span>
                            {targetState?.label ?? targetStatus} (<code>{targetStatus}</code>)
                          </span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          );
        })}

        <div className="flex items-center gap-3 pt-2">
          <button
            type="submit"
            disabled={isMutating || serverChanged}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {saveMutation.isPending ? "Saving…" : "Save Lifecycle Settings"}
          </button>
        </div>
        </fieldset>
      </form>
    </div>
  );
}
