import { UseQueryResult, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { useHasPermission } from "@/features/auth/useAuthorization";
import {
  Building,
  City,
  Country,
  Floor,
  Organization,
  Room,
  Site,
  createBuilding,
  createCity,
  createCountry,
  createFloor,
  createOrganization,
  createRoom,
  createSite,
  listBuildings,
  listCities,
  listCountries,
  listFloors,
  listOrganizations,
  listRooms,
  listSites,
  updateRoom,
} from "@/features/locations/api";
import { ApiError } from "@/lib/apiClient";
import { Page } from "@/types";

/** Location hierarchy administration: Organization → Country → City → Site → Building →
 * Floor → Room, backed only by the existing `app/api/v1/locations.py` endpoints (create and
 * list for every level; Room update via its If-Match optimistic-concurrency endpoint — there
 * is no delete in the API, so none is offered). A cascade: each level appears once its parent
 * is selected, so a parent is always chosen from the authoritative list rather than typed as
 * a raw id. All create affordances are gated on the `location:manage` permission that the
 * backend's `require_permission("location:manage")` actually enforces, and the lists on
 * `location:read`; the backend remains the authority on every request either way. */

interface FieldSpec {
  name: string;
  label: string;
  placeholder?: string;
  type?: "text" | "number";
  required?: boolean;
  defaultValue?: string;
  help?: string;
}

function createErrorMessage(error: unknown, noun: string): string {
  if (error instanceof ApiError) {
    // 409 is the backend's IntegrityError translation (unique constraint / conflict); the
    // specific constraint is not disclosed by the API, so phrase it as a likely duplicate
    // and keep the server's own detail alongside. A 404 is a missing/removed parent.
    if (error.status === 409) return `A ${noun} with these details already exists. ${error.detail}`;
    return error.detail;
  }
  return `Could not save the ${noun}.`;
}

function EntityForm({
  noun,
  fields,
  submitLabel,
  initialValues,
  resetOnSuccess = false,
  onSubmit,
  onSuccess,
}: {
  noun: string;
  fields: FieldSpec[];
  submitLabel: string;
  initialValues?: Record<string, string>;
  resetOnSuccess?: boolean;
  onSubmit: (values: Record<string, string>) => Promise<unknown>;
  onSuccess?: () => void;
}) {
  const defaults = (): Record<string, string> =>
    Object.fromEntries(fields.map((f) => [f.name, initialValues?.[f.name] ?? f.defaultValue ?? ""]));
  const [values, setValues] = useState<Record<string, string>>(defaults);
  const [localError, setLocalError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => onSubmit(values),
    onSuccess: () => {
      setLocalError(null);
      if (resetOnSuccess) setValues(defaults());
      onSuccess?.();
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    for (const field of fields) {
      if (field.required === false) continue;
      if (!(values[field.name] ?? "").trim()) {
        setLocalError(`${field.label} is required.`);
        return;
      }
    }
    setLocalError(null);
    mutation.mutate();
  }

  const message = localError ?? (mutation.isError ? createErrorMessage(mutation.error, noun) : null);

  return (
    <form onSubmit={handleSubmit} className="mb-4 flex flex-wrap items-end gap-2 rounded-sm border border-slate-800 bg-slate-950 p-3">
      {fields.map((field) => (
        <label key={field.name} className="flex flex-col text-xs text-slate-400">
          {field.label}
          <input
            type={field.type ?? "text"}
            value={values[field.name] ?? ""}
            placeholder={field.placeholder}
            aria-label={field.label}
            aria-invalid={Boolean(message)}
            onChange={(e) => setValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
            className="mt-1 w-44 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          {field.help && <span className="mt-0.5 text-[10px] text-slate-600">{field.help}</span>}
        </label>
      ))}
      <button
        type="submit"
        disabled={mutation.isPending}
        className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
      >
        {mutation.isPending ? "Saving…" : submitLabel}
      </button>
      {message && (
        <p role="alert" className="w-full text-sm text-red-400">
          {message}
        </p>
      )}
    </form>
  );
}

function Section({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-1 text-sm font-semibold text-slate-200">{title}</h2>
      {description && <p className="mb-3 text-xs text-slate-500">{description}</p>}
      {children}
    </section>
  );
}

function HierarchyItem({
  selected,
  primary,
  secondary,
  onSelect,
}: {
  selected: boolean;
  primary: string;
  secondary?: string;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={`flex w-full items-baseline justify-between gap-3 rounded-sm border px-3 py-1.5 text-left text-sm ${
          selected
            ? "border-blue-600 bg-blue-600/20 text-slate-100"
            : "border-slate-800 bg-slate-950 text-slate-300 hover:border-slate-600"
        }`}
      >
        <span className="font-medium">{primary}</span>
        {secondary && <span className="text-xs text-slate-500">{secondary}</span>}
      </button>
    </li>
  );
}

function LevelList<T extends { id: string }>({
  query,
  selectedId,
  onSelect,
  renderPrimary,
  renderSecondary,
  emptyLabel,
}: {
  query: UseQueryResult<Page<T>, Error>;
  selectedId: string;
  onSelect: (id: string) => void;
  renderPrimary: (item: T) => string;
  renderSecondary?: (item: T) => string | undefined;
  emptyLabel: string;
}) {
  if (query.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (query.isError) return <p className="text-sm text-red-400">{(query.error as Error).message}</p>;
  const items = query.data?.items ?? [];
  if (items.length === 0) return <p className="text-sm text-slate-500">{emptyLabel}</p>;
  return (
    <ul className="space-y-1">
      {items.map((item) => (
        <HierarchyItem
          key={item.id}
          selected={item.id === selectedId}
          primary={renderPrimary(item)}
          secondary={renderSecondary?.(item)}
          onSelect={() => onSelect(item.id)}
        />
      ))}
    </ul>
  );
}

export function LocationsPage() {
  const queryClient = useQueryClient();
  const canReadOrgs = useHasPermission("organization:read");
  const canRead = useHasPermission("location:read");
  const canManage = useHasPermission("location:manage");
  const canUpdate = useHasPermission("location:update");

  const [orgId, setOrgId] = useState("");
  const [countryId, setCountryId] = useState("");
  const [cityId, setCityId] = useState("");
  const [siteId, setSiteId] = useState("");
  const [buildingId, setBuildingId] = useState("");
  const [floorId, setFloorId] = useState("");
  const [roomId, setRoomId] = useState("");

  function selectOrganization(id: string) {
    setOrgId(id);
    setCountryId("");
    setCityId("");
    setSiteId("");
    setBuildingId("");
    setFloorId("");
    setRoomId("");
  }
  function selectCountry(id: string) {
    setCountryId(id);
    setCityId("");
    setSiteId("");
    setBuildingId("");
    setFloorId("");
    setRoomId("");
  }
  function selectCity(id: string) {
    setCityId(id);
    setSiteId("");
    setBuildingId("");
    setFloorId("");
    setRoomId("");
  }
  function selectSite(id: string) {
    setSiteId(id);
    setBuildingId("");
    setFloorId("");
    setRoomId("");
  }
  function selectBuilding(id: string) {
    setBuildingId(id);
    setFloorId("");
    setRoomId("");
  }
  function selectFloor(id: string) {
    setFloorId(id);
    setRoomId("");
  }
  function selectRoom(id: string) {
    setRoomId(id === roomId ? "" : id);
  }

  const organizations = useQuery({
    queryKey: ["locations", "organizations"],
    queryFn: listOrganizations,
    enabled: canReadOrgs,
  });
  const countries = useQuery({
    queryKey: ["locations", "countries", orgId],
    queryFn: () => listCountries(orgId),
    enabled: canRead && !!orgId,
  });
  const cities = useQuery({
    queryKey: ["locations", "cities", countryId],
    queryFn: () => listCities(countryId),
    enabled: canRead && !!countryId,
  });
  const sites = useQuery({
    queryKey: ["locations", "sites", cityId],
    queryFn: () => listSites(cityId),
    enabled: canRead && !!cityId,
  });
  const buildings = useQuery({
    queryKey: ["locations", "buildings", siteId],
    queryFn: () => listBuildings(siteId),
    enabled: canRead && !!siteId,
  });
  const floors = useQuery({
    queryKey: ["locations", "floors", buildingId],
    queryFn: () => listFloors(buildingId),
    enabled: canRead && !!buildingId,
  });
  const rooms = useQuery({
    queryKey: ["locations", "rooms", floorId],
    queryFn: () => listRooms(floorId),
    enabled: canRead && !!floorId,
  });

  const invalidate = (key: unknown[]) => queryClient.invalidateQueries({ queryKey: key });

  const selectedOrg = organizations.data?.items.find((o) => o.id === orgId);
  const selectedCountry = countries.data?.items.find((c) => c.id === countryId);
  const selectedCity = cities.data?.items.find((c) => c.id === cityId);
  const selectedSite = sites.data?.items.find((s) => s.id === siteId);
  const selectedBuilding = buildings.data?.items.find((b) => b.id === buildingId);
  const selectedFloor = floors.data?.items.find((f) => f.id === floorId);
  const selectedRoom = rooms.data?.items.find((r) => r.id === roomId);

  const breadcrumb = [
    selectedOrg && { label: selectedOrg.name, onClick: () => selectOrganization(selectedOrg.id) },
    selectedCountry && { label: selectedCountry.name, onClick: () => selectCountry(selectedCountry.id) },
    selectedCity && { label: selectedCity.name, onClick: () => selectCity(selectedCity.id) },
    selectedSite && { label: selectedSite.name, onClick: () => selectSite(selectedSite.id) },
    selectedBuilding && { label: selectedBuilding.name, onClick: () => selectBuilding(selectedBuilding.id) },
    selectedFloor && { label: selectedFloor.name, onClick: () => selectFloor(selectedFloor.id) },
  ].filter(Boolean) as { label: string; onClick: () => void }[];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Location Administration</h1>
        <p className="mt-1 text-sm text-slate-400">
          Create and browse the full location hierarchy: Organization → Country → City → Site → Building → Floor → Room.
          Select a level to reveal the next. Rack and equipment bulk imports resolve sites by <code>site_code</code>, so the
          site must exist here first.
        </p>
        <p className="mt-1 text-xs text-slate-500">
          Looking for an operational view instead?{" "}
          <Link to="/infrastructure" className="text-blue-400 hover:underline">
            Infrastructure
          </Link>
          .
        </p>
      </div>

      {breadcrumb.length > 0 && (
        <nav aria-label="Location path" className="flex flex-wrap items-center gap-1 text-xs text-slate-400">
          <span className="text-slate-500">Path:</span>
          {breadcrumb.map((crumb, i) => (
            <span key={`${crumb.label}-${i}`} className="flex items-center gap-1">
              {i > 0 && <span className="text-slate-600">/</span>}
              <button type="button" onClick={crumb.onClick} className="text-blue-400 hover:underline">
                {crumb.label}
              </button>
            </span>
          ))}
        </nav>
      )}

      {!canReadOrgs && !canRead && (
        <p role="alert" className="rounded-sm border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
          You do not have permission to view location data.
        </p>
      )}

      {canReadOrgs && (
        <Section title="Organizations" description="The tenant root of every hierarchy.">
          {canManage && (
            <EntityForm
              noun="organization"
              fields={[{ name: "name", label: "Organization name", placeholder: "e.g. Acme Corp" }]}
              submitLabel="Create organization"
              resetOnSuccess
              onSubmit={(v) => createOrganization({ name: v.name.trim() })}
              onSuccess={() => invalidate(["locations", "organizations"])}
            />
          )}
          <LevelList
            query={organizations}
            selectedId={orgId}
            onSelect={selectOrganization}
            renderPrimary={(o: Organization) => o.name}
            renderSecondary={(o: Organization) => new Date(o.created_at).toLocaleDateString()}
            emptyLabel="No organizations yet. Create one to begin."
          />
        </Section>
      )}

      {orgId && (
        <Section title="Countries" description={`Countries in ${selectedOrg?.name ?? "the selected organization"}.`}>
          {canManage && (
            <EntityForm
              noun="country"
              fields={[
                { name: "name", label: "Country name", placeholder: "e.g. United Arab Emirates" },
                { name: "iso_code", label: "ISO code", placeholder: "AE", required: false, help: "Optional, 2 letters." },
              ]}
              submitLabel="Create country"
              resetOnSuccess
              onSubmit={(v) =>
                createCountry({ organization_id: orgId, name: v.name.trim(), iso_code: v.iso_code.trim() || null })
              }
              onSuccess={() => invalidate(["locations", "countries", orgId])}
            />
          )}
          <LevelList
            query={countries}
            selectedId={countryId}
            onSelect={selectCountry}
            renderPrimary={(c: Country) => c.name}
            renderSecondary={(c: Country) => c.iso_code ?? undefined}
            emptyLabel="No countries in this organization yet."
          />
        </Section>
      )}

      {countryId && (
        <Section title="Cities" description={`Cities in ${selectedCountry?.name ?? "the selected country"}.`}>
          {canManage && (
            <EntityForm
              noun="city"
              fields={[{ name: "name", label: "City name", placeholder: "e.g. Dubai" }]}
              submitLabel="Create city"
              resetOnSuccess
              onSubmit={(v) => createCity({ country_id: countryId, name: v.name.trim() })}
              onSuccess={() => invalidate(["locations", "cities", countryId])}
            />
          )}
          <LevelList
            query={cities}
            selectedId={cityId}
            onSelect={selectCity}
            renderPrimary={(c: City) => c.name}
            emptyLabel="No cities in this country yet."
          />
        </Section>
      )}

      {cityId && (
        <Section title="Sites" description={`Sites in ${selectedCity?.name ?? "the selected city"}. The site code is the value bulk imports resolve.`}>
          {canManage && (
            <EntityForm
              noun="site"
              fields={[
                { name: "code", label: "Site code", placeholder: "e.g. SITE1" },
                { name: "name", label: "Site name", placeholder: "e.g. Dubai Data Centre" },
                { name: "timezone", label: "Timezone", placeholder: "UTC", required: false, defaultValue: "UTC" },
              ]}
              submitLabel="Create site"
              resetOnSuccess
              onSubmit={(v) =>
                createSite({ city_id: cityId, code: v.code.trim(), name: v.name.trim(), timezone: v.timezone.trim() || "UTC" })
              }
              onSuccess={() => invalidate(["locations", "sites", cityId])}
            />
          )}
          <LevelList
            query={sites}
            selectedId={siteId}
            onSelect={selectSite}
            renderPrimary={(s: Site) => s.name}
            renderSecondary={(s: Site) => `${s.code} · ${s.timezone}`}
            emptyLabel="No sites in this city yet."
          />
        </Section>
      )}

      {siteId && (
        <Section title="Buildings" description={`Buildings in ${selectedSite?.name ?? "the selected site"}.`}>
          {canManage && (
            <EntityForm
              noun="building"
              fields={[
                { name: "code", label: "Building code", placeholder: "e.g. A" },
                { name: "name", label: "Building name", placeholder: "e.g. Building A" },
              ]}
              submitLabel="Create building"
              resetOnSuccess
              onSubmit={(v) => createBuilding({ site_id: siteId, code: v.code.trim(), name: v.name.trim() })}
              onSuccess={() => invalidate(["locations", "buildings", siteId])}
            />
          )}
          <LevelList
            query={buildings}
            selectedId={buildingId}
            onSelect={selectBuilding}
            renderPrimary={(b: Building) => b.name}
            renderSecondary={(b: Building) => b.code}
            emptyLabel="No buildings in this site yet."
          />
        </Section>
      )}

      {buildingId && (
        <Section title="Floors" description={`Floors in ${selectedBuilding?.name ?? "the selected building"}.`}>
          {canManage && (
            <EntityForm
              noun="floor"
              fields={[
                { name: "name", label: "Floor name", placeholder: "e.g. Ground Floor" },
                { name: "level_number", label: "Level number", placeholder: "0", type: "number" },
              ]}
              submitLabel="Create floor"
              resetOnSuccess
              onSubmit={(v) =>
                createFloor({ building_id: buildingId, name: v.name.trim(), level_number: Number(v.level_number) })
              }
              onSuccess={() => invalidate(["locations", "floors", buildingId])}
            />
          )}
          <LevelList
            query={floors}
            selectedId={floorId}
            onSelect={selectFloor}
            renderPrimary={(f: Floor) => f.name}
            renderSecondary={(f: Floor) => `level ${f.level_number}`}
            emptyLabel="No floors in this building yet."
          />
        </Section>
      )}

      {floorId && (
        <Section title="Rooms" description={`Rooms on ${selectedFloor?.name ?? "the selected floor"}. Racks are placed in rooms.`}>
          {canManage && (
            <EntityForm
              noun="room"
              fields={[
                { name: "code", label: "Room code", placeholder: "e.g. R101" },
                { name: "name", label: "Room name", placeholder: "e.g. Data Hall 1" },
                { name: "room_type", label: "Room type", defaultValue: "data_hall", required: false },
              ]}
              submitLabel="Create room"
              resetOnSuccess
              onSubmit={(v) =>
                createRoom({
                  floor_id: floorId,
                  code: v.code.trim(),
                  name: v.name.trim(),
                  room_type: v.room_type.trim() || "data_hall",
                })
              }
              onSuccess={() => {
                invalidate(["locations", "rooms", floorId]);
                invalidate(["rooms"]);
              }}
            />
          )}
          <LevelList
            query={rooms}
            selectedId={roomId}
            onSelect={selectRoom}
            renderPrimary={(r: Room) => r.name}
            renderSecondary={(r: Room) => `${r.code} · ${r.room_type}`}
            emptyLabel="No rooms on this floor yet."
          />
          {selectedRoom && (
            <div className="mt-4 border-t border-slate-800 pt-3">
              <h3 className="mb-1 text-xs font-semibold text-slate-300">Edit {selectedRoom.name}</h3>
              {canUpdate ? (
                <EntityForm
                  key={`${selectedRoom.id}-${selectedRoom.version}`}
                  noun="room"
                  fields={[
                    { name: "name", label: "Edit room name" },
                    { name: "room_type", label: "Edit room type" },
                  ]}
                  submitLabel="Save room"
                  initialValues={{ name: selectedRoom.name, room_type: selectedRoom.room_type }}
                  onSubmit={(v) =>
                    updateRoom(
                      selectedRoom.id,
                      { name: v.name.trim(), room_type: v.room_type.trim() || "data_hall" },
                      selectedRoom.version,
                    )
                  }
                  onSuccess={() => {
                    invalidate(["locations", "rooms", floorId]);
                    invalidate(["rooms"]);
                  }}
                />
              ) : (
                <p className="text-xs text-slate-500">
                  You need the <code>location:update</code> permission to edit rooms.
                </p>
              )}
            </div>
          )}
        </Section>
      )}
    </div>
  );
}
