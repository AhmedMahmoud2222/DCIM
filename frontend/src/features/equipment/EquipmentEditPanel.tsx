import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Dispatch, FormEvent, SetStateAction, useEffect, useRef, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { updateEquipment } from "@/features/equipment/api";
import { LifecycleTransitionAction } from "@/features/managed-assets/LifecycleTransitionAction";
import { useLifecycleSettings } from "@/features/managed-assets/useLifecycleSettings";
import { LifecycleStatus } from "@/features/status/StatusBadges";
import { ApiError } from "@/lib/apiClient";
import { Equipment } from "@/types";

function errorMessage(err: unknown, fallback = "Request failed."): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

interface Props {
  equipment: Equipment;
  onClose: () => void;
  onNotice?: (message: string) => void;
}

export function EquipmentEditPanel({ equipment, onClose, onNotice }: Props) {
  const queryClient = useQueryClient();
  const canEditMetadata = useHasPermission("equipment:manage");
  const { stateByStatus } = useLifecycleSettings();
  const currentConfig = stateByStatus(equipment.lifecycle_status);

  const [hostname, setHostname] = useState(equipment.hostname ?? "");
  const [owner, setOwner] = useState(equipment.owner ?? "");
  const [service, setService] = useState(equipment.service ?? "");
  const [environment, setEnvironment] = useState(equipment.environment ?? "");
  const [notes, setNotes] = useState(equipment.notes ?? "");

  // Sync from the server without discarding unsaved edits: a field is overwritten only when it still
  // equals the previously synced server value (i.e. is not dirty) or when a different equipment is shown.
  const synced = useRef({ id: equipment.id, equipment });
  useEffect(() => {
    const prev = synced.current;
    const switched = prev.id !== equipment.id;
    const sync = (
      set: Dispatch<SetStateAction<string>>,
      key: "hostname" | "owner" | "service" | "environment" | "notes",
    ) => {
      const next = equipment[key] ?? "";
      if (switched) {
        set(next);
        return;
      }
      const old = prev.equipment[key] ?? "";
      set((current) => (current === old ? next : current));
    };
    sync(setHostname, "hostname");
    sync(setOwner, "owner");
    sync(setService, "service");
    sync(setEnvironment, "environment");
    sync(setNotes, "notes");
    synced.current = { id: equipment.id, equipment };
  }, [equipment]);

  const updateMutation = useMutation({
    mutationFn: () =>
      updateEquipment(
        equipment.id,
        {
          hostname: hostname.trim() || null,
          owner: owner.trim() || null,
          service: service.trim() || null,
          environment: environment.trim() || null,
          notes: notes.trim() || null,
        },
        equipment.version,
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["equipment"] }),
        queryClient.invalidateQueries({ queryKey: ["equipment", equipment.id] }),
        queryClient.invalidateQueries({ queryKey: ["managed-assets"] }),
      ]);
      onNotice?.(`${hostname.trim() || equipment.asset_tag} details saved.`);
      onClose();
    },
  });

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!canEditMetadata) return;
    updateMutation.mutate();
  };

  return (
    <section
      aria-label={`Edit equipment ${equipment.hostname ?? equipment.asset_tag}`}
      className="mb-6 rounded-lg border border-line-default bg-surface-panel p-5"
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-line-subtle pb-3">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wider text-fg-secondary">
            Edit Equipment — {equipment.hostname ?? equipment.asset_tag}
          </h2>
          <p className="text-xs text-fg-secondary">
            Asset Tag: <span className="font-mono text-fg-primary">{equipment.asset_tag}</span>
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded border border-line-default bg-surface-raised px-2.5 py-1 text-xs text-fg-secondary hover:bg-surface-hover hover:text-fg-primary"
        >
          Close edit
        </button>
      </div>

      {/* Lifecycle management inside Equipment Editing */}
      <div className="mb-4 rounded-md border border-line-subtle bg-surface-raised p-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1">
            <span className="block text-xs font-medium uppercase tracking-wider text-fg-secondary">
              Lifecycle State
            </span>
            <div className="flex flex-wrap items-center gap-2">
              <LifecycleStatus
                value={equipment.lifecycle_status}
                labelOverride={currentConfig?.label}
                toneOverride={currentConfig?.badge_color}
                description={currentConfig?.description}
                showLegacyNotice
              />
              {currentConfig?.description && (
                <span className="text-xs text-fg-secondary">{currentConfig.description}</span>
              )}
            </div>
          </div>

          <LifecycleTransitionAction
            assetId={equipment.id}
            assetName={equipment.hostname ?? equipment.asset_tag}
            status={equipment.lifecycle_status}
            assetLabel="equipment"
            onCompleted={onNotice}
          />
        </div>

        {equipment.lifecycle_status === "reserved" && (
          <p
            className="mt-2 rounded border border-tone-caution-fg bg-tone-caution-bg px-2.5 py-1.5 text-xs text-tone-caution-fg"
            data-testid="edit-reserved-legacy-warning"
          >
            This record uses the legacy <code>reserved</code> lifecycle status. Saving equipment details will preserve
            the existing status without converting it.
          </p>
        )}
      </div>

      {canEditMetadata && (
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label
                htmlFor={`edit-hostname-${equipment.id}`}
                className="mb-1 block text-xs font-medium text-fg-secondary"
              >
                Hostname
              </label>
              <input
                id={`edit-hostname-${equipment.id}`}
                aria-label="Edit hostname"
                value={hostname}
                onChange={(e) => setHostname(e.target.value)}
                className="w-full rounded border border-line-default bg-surface-input px-3 py-1.5 text-sm text-fg-primary"
              />
            </div>

            <div>
              <label
                htmlFor={`edit-owner-${equipment.id}`}
                className="mb-1 block text-xs font-medium text-fg-secondary"
              >
                Owner
              </label>
              <input
                id={`edit-owner-${equipment.id}`}
                aria-label="Edit owner"
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                className="w-full rounded border border-line-default bg-surface-input px-3 py-1.5 text-sm text-fg-primary"
              />
            </div>

            <div>
              <label
                htmlFor={`edit-service-${equipment.id}`}
                className="mb-1 block text-xs font-medium text-fg-secondary"
              >
                Service
              </label>
              <input
                id={`edit-service-${equipment.id}`}
                aria-label="Edit service"
                value={service}
                onChange={(e) => setService(e.target.value)}
                className="w-full rounded border border-line-default bg-surface-input px-3 py-1.5 text-sm text-fg-primary"
              />
            </div>

            <div>
              <label
                htmlFor={`edit-environment-${equipment.id}`}
                className="mb-1 block text-xs font-medium text-fg-secondary"
              >
                Environment
              </label>
              <input
                id={`edit-environment-${equipment.id}`}
                aria-label="Edit environment"
                value={environment}
                onChange={(e) => setEnvironment(e.target.value)}
                className="w-full rounded border border-line-default bg-surface-input px-3 py-1.5 text-sm text-fg-primary"
              />
            </div>
          </div>

          <div>
            <label
              htmlFor={`edit-notes-${equipment.id}`}
              className="mb-1 block text-xs font-medium text-fg-secondary"
            >
              Notes
            </label>
            <textarea
              id={`edit-notes-${equipment.id}`}
              aria-label="Edit notes"
              rows={2}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="w-full rounded border border-line-default bg-surface-input px-3 py-1.5 text-sm text-fg-primary"
            />
          </div>

          {updateMutation.isError && (
            <p role="alert" className="text-sm text-tone-danger-fg">
              {updateMutation.error instanceof ApiError && updateMutation.error.status === 409
                ? "Equipment was modified by another user — reload and try again."
                : errorMessage(updateMutation.error, "Could not save equipment changes.")}
            </p>
          )}

          <div className="flex items-center gap-2 pt-1">
            <button
              type="submit"
              disabled={updateMutation.isPending}
              className="rounded-md bg-accent px-4 py-1.5 text-sm font-medium text-fg-on-accent hover:bg-accent-hover disabled:opacity-50"
            >
              {updateMutation.isPending ? "Saving…" : "Save Equipment Changes"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-line-default bg-surface-raised px-3 py-1.5 text-sm text-fg-secondary hover:bg-surface-hover"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
