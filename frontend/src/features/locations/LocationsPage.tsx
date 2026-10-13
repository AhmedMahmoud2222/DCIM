import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import {
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
import { Room } from "@/types";

type LevelTab = "organizations" | "countries" | "cities" | "sites" | "buildings" | "floors" | "rooms";

function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return "An unexpected error occurred.";
}

export function LocationsPage() {
  const queryClient = useQueryClient();
  const canManage = useHasPermission("location:manage");
  const canUpdate = useHasPermission("location:update");

  const [activeTab, setActiveTab] = useState<LevelTab>("sites");

  // Filter selections for drill-down hierarchy
  const [selectedOrgId, setSelectedOrgId] = useState<string>("");
  const [selectedCountryId, setSelectedCountryId] = useState<string>("");
  const [selectedCityId, setSelectedCityId] = useState<string>("");
  const [selectedSiteId, setSelectedSiteId] = useState<string>("");
  const [selectedBuildingId, setSelectedBuildingId] = useState<string>("");
  const [selectedFloorId, setSelectedFloorId] = useState<string>("");

  // Form states
  // Organization
  const [orgName, setOrgName] = useState("");

  // Country
  const [countryOrgId, setCountryOrgId] = useState("");
  const [countryName, setCountryName] = useState("");
  const [countryIsoCode, setCountryIsoCode] = useState("");

  // City
  const [cityCountryId, setCityCountryId] = useState("");
  const [cityName, setCityName] = useState("");

  // Site
  const [siteCityId, setSiteCityId] = useState("");
  const [siteCode, setSiteCode] = useState("");
  const [siteName, setSiteName] = useState("");
  const [siteAddress, setSiteAddress] = useState("");
  const [siteTimezone, setSiteTimezone] = useState("UTC");

  // Building
  const [buildingSiteId, setBuildingSiteId] = useState("");
  const [buildingCode, setBuildingCode] = useState("");
  const [buildingName, setBuildingName] = useState("");

  // Floor
  const [floorBuildingId, setFloorBuildingId] = useState("");
  const [floorName, setFloorName] = useState("");
  const [floorLevelNumber, setFloorLevelNumber] = useState("0");

  // Room
  const [roomFloorId, setRoomFloorId] = useState("");
  const [roomCode, setRoomCode] = useState("");
  const [roomName, setRoomName] = useState("");
  const [roomType, setRoomType] = useState("data_hall");

  // Room update modal
  const [editingRoom, setEditingRoom] = useState<Room | null>(null);
  const [editRoomName, setEditRoomName] = useState("");
  const [editRoomType, setEditRoomType] = useState("data_hall");

  // Queries
  const orgsQuery = useQuery({ queryKey: ["organizations"], queryFn: listOrganizations });
  const countriesQuery = useQuery({
    queryKey: ["countries", selectedOrgId],
    queryFn: () => listCountries(selectedOrgId || undefined),
  });
  const citiesQuery = useQuery({
    queryKey: ["cities", selectedCountryId],
    queryFn: () => listCities(selectedCountryId || undefined),
  });
  const sitesQuery = useQuery({
    queryKey: ["sites", selectedCityId],
    queryFn: () => listSites(selectedCityId || undefined),
  });
  const buildingsQuery = useQuery({
    queryKey: ["buildings", selectedSiteId],
    queryFn: () => listBuildings(selectedSiteId || undefined),
  });
  const floorsQuery = useQuery({
    queryKey: ["floors", selectedBuildingId],
    queryFn: () => listFloors(selectedBuildingId || undefined),
  });
  const roomsQuery = useQuery({
    queryKey: ["rooms", selectedFloorId, selectedSiteId],
    queryFn: () =>
      listRooms({
        floor_id: selectedFloorId || undefined,
        site_id: selectedSiteId || undefined,
      }),
  });

  // Mutations
  const createOrgMutation = useMutation({
    mutationFn: () => createOrganization({ name: orgName.trim() }),
    onSuccess: () => {
      setOrgName("");
      queryClient.invalidateQueries({ queryKey: ["organizations"] });
    },
  });

  const createCountryMutation = useMutation({
    mutationFn: () =>
      createCountry({
        organization_id: countryOrgId || selectedOrgId,
        name: countryName.trim(),
        iso_code: countryIsoCode.trim() || undefined,
      }),
    onSuccess: () => {
      setCountryName("");
      setCountryIsoCode("");
      queryClient.invalidateQueries({ queryKey: ["countries"] });
    },
  });

  const createCityMutation = useMutation({
    mutationFn: () =>
      createCity({
        country_id: cityCountryId || selectedCountryId,
        name: cityName.trim(),
      }),
    onSuccess: () => {
      setCityName("");
      queryClient.invalidateQueries({ queryKey: ["cities"] });
    },
  });

  const createSiteMutation = useMutation({
    mutationFn: () =>
      createSite({
        city_id: siteCityId || selectedCityId,
        code: siteCode.trim(),
        name: siteName.trim(),
        address: siteAddress.trim() || undefined,
        timezone: siteTimezone.trim() || "UTC",
      }),
    onSuccess: () => {
      setSiteCode("");
      setSiteName("");
      setSiteAddress("");
      queryClient.invalidateQueries({ queryKey: ["sites"] });
    },
  });

  const createBuildingMutation = useMutation({
    mutationFn: () =>
      createBuilding({
        site_id: buildingSiteId || selectedSiteId,
        code: buildingCode.trim(),
        name: buildingName.trim(),
      }),
    onSuccess: () => {
      setBuildingCode("");
      setBuildingName("");
      queryClient.invalidateQueries({ queryKey: ["buildings"] });
    },
  });

  const createFloorMutation = useMutation({
    mutationFn: () =>
      createFloor({
        building_id: floorBuildingId || selectedBuildingId,
        name: floorName.trim(),
        level_number: Number(floorLevelNumber),
      }),
    onSuccess: () => {
      setFloorName("");
      setFloorLevelNumber("0");
      queryClient.invalidateQueries({ queryKey: ["floors"] });
    },
  });

  const createRoomMutation = useMutation({
    mutationFn: () =>
      createRoom({
        floor_id: roomFloorId || selectedFloorId,
        code: roomCode.trim(),
        name: roomName.trim(),
        room_type: roomType,
      }),
    onSuccess: () => {
      setRoomCode("");
      setRoomName("");
      queryClient.invalidateQueries({ queryKey: ["rooms"] });
    },
  });

  const updateRoomMutation = useMutation({
    mutationFn: () =>
      updateRoom(
        editingRoom!.id,
        {
          name: editRoomName.trim(),
          room_type: editRoomType,
        },
        editingRoom!.version,
      ),
    onSuccess: () => {
      setEditingRoom(null);
      queryClient.invalidateQueries({ queryKey: ["rooms"] });
    },
  });

  return (
    <div className="space-y-6" data-testid="locations-management-page">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">Location Management</h1>
        <p className="text-sm text-slate-400">
          Manage the full physical hierarchy: Organization → Country → City → Site → Building → Floor → Room.
        </p>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-slate-800 text-sm font-medium">
        {(
          [
            ["organizations", "1. Organizations"],
            ["countries", "2. Countries"],
            ["cities", "3. Cities"],
            ["sites", "4. Sites"],
            ["buildings", "5. Buildings"],
            ["floors", "6. Floors"],
            ["rooms", "7. Rooms"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setActiveTab(key)}
            className={`border-b-2 px-4 py-2 hover:text-slate-100 ${
              activeTab === key
                ? "border-blue-500 text-blue-400 font-semibold"
                : "border-transparent text-slate-400"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Level 1: Organizations */}
      {activeTab === "organizations" && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold text-slate-200">Organizations</h2>
          </div>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (orgName.trim()) createOrgMutation.mutate();
              }}
              className="flex items-center gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4"
            >
              <input
                data-testid="create-org-name"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                placeholder="Organization name (e.g. Acme Corp)"
                className="w-72 rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <button
                type="submit"
                data-testid="create-org-submit"
                disabled={createOrgMutation.isPending || !orgName.trim()}
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createOrgMutation.isPending ? "Creating…" : "Create Organization"}
              </button>
            </form>
          )}
          {createOrgMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createOrgMutation.error)}</p>
          )}

          {orgsQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading organizations…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">ID</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2">Created At</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {orgsQuery.data?.items.map((org) => (
                  <tr key={org.id} className="border-b border-slate-900">
                    <td className="py-2 font-mono text-xs text-slate-500">{org.id.slice(0, 8)}…</td>
                    <td className="py-2 font-medium">{org.name}</td>
                    <td className="py-2 text-slate-400">{new Date(org.created_at).toLocaleString()}</td>
                    <td className="py-2">
                      <button
                        onClick={() => {
                          setSelectedOrgId(org.id);
                          setActiveTab("countries");
                        }}
                        className="text-xs text-blue-400 hover:underline"
                      >
                        View Countries →
                      </button>
                    </td>
                  </tr>
                ))}
                {orgsQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-slate-500">
                      No organizations created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Level 2: Countries */}
      {activeTab === "countries" && (
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-slate-200">Countries</h2>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if ((countryOrgId || selectedOrgId) && countryName.trim()) createCountryMutation.mutate();
              }}
              className="grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-4"
            >
              <select
                data-testid="create-country-org-select"
                value={countryOrgId || selectedOrgId}
                onChange={(e) => {
                  setCountryOrgId(e.target.value);
                  setSelectedOrgId(e.target.value);
                }}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select Organization…</option>
                {orgsQuery.data?.items.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
              <input
                data-testid="create-country-name"
                value={countryName}
                onChange={(e) => setCountryName(e.target.value)}
                placeholder="Country name (e.g. United States)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="create-country-iso"
                value={countryIsoCode}
                onChange={(e) => setCountryIsoCode(e.target.value)}
                placeholder="ISO code (e.g. US)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <button
                type="submit"
                data-testid="create-country-submit"
                disabled={createCountryMutation.isPending || !(countryOrgId || selectedOrgId) || !countryName.trim()}
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createCountryMutation.isPending ? "Creating…" : "Create Country"}
              </button>
            </form>
          )}
          {createCountryMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createCountryMutation.error)}</p>
          )}

          {countriesQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading countries…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">Name</th>
                  <th className="pb-2">ISO Code</th>
                  <th className="pb-2">Organization ID</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {countriesQuery.data?.items.map((c) => (
                  <tr key={c.id} className="border-b border-slate-900">
                    <td className="py-2 font-medium">{c.name}</td>
                    <td className="py-2 text-slate-400">{c.iso_code || "—"}</td>
                    <td className="py-2 font-mono text-xs text-slate-500">{c.organization_id.slice(0, 8)}…</td>
                    <td className="py-2">
                      <button
                        onClick={() => {
                          setSelectedCountryId(c.id);
                          setActiveTab("cities");
                        }}
                        className="text-xs text-blue-400 hover:underline"
                      >
                        View Cities →
                      </button>
                    </td>
                  </tr>
                ))}
                {countriesQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-slate-500">
                      No countries created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Level 3: Cities */}
      {activeTab === "cities" && (
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-slate-200">Cities</h2>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if ((cityCountryId || selectedCountryId) && cityName.trim()) createCityMutation.mutate();
              }}
              className="grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-3"
            >
              <select
                data-testid="create-city-country-select"
                value={cityCountryId || selectedCountryId}
                onChange={(e) => {
                  setCityCountryId(e.target.value);
                  setSelectedCountryId(e.target.value);
                }}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select Country…</option>
                {countriesQuery.data?.items.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <input
                data-testid="create-city-name"
                value={cityName}
                onChange={(e) => setCityName(e.target.value)}
                placeholder="City name (e.g. New York)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <button
                type="submit"
                data-testid="create-city-submit"
                disabled={createCityMutation.isPending || !(cityCountryId || selectedCountryId) || !cityName.trim()}
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createCityMutation.isPending ? "Creating…" : "Create City"}
              </button>
            </form>
          )}
          {createCityMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createCityMutation.error)}</p>
          )}

          {citiesQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading cities…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">Name</th>
                  <th className="pb-2">Country ID</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {citiesQuery.data?.items.map((city) => (
                  <tr key={city.id} className="border-b border-slate-900">
                    <td className="py-2 font-medium">{city.name}</td>
                    <td className="py-2 font-mono text-xs text-slate-500">{city.country_id.slice(0, 8)}…</td>
                    <td className="py-2">
                      <button
                        onClick={() => {
                          setSelectedCityId(city.id);
                          setActiveTab("sites");
                        }}
                        className="text-xs text-blue-400 hover:underline"
                      >
                        View Sites →
                      </button>
                    </td>
                  </tr>
                ))}
                {citiesQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={3} className="py-4 text-center text-slate-500">
                      No cities created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Level 4: Sites */}
      {activeTab === "sites" && (
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-slate-200">Sites</h2>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if ((siteCityId || selectedCityId) && siteCode.trim() && siteName.trim()) createSiteMutation.mutate();
              }}
              className="grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-3"
            >
              <select
                data-testid="create-site-city-select"
                value={siteCityId || selectedCityId}
                onChange={(e) => {
                  setSiteCityId(e.target.value);
                  setSelectedCityId(e.target.value);
                }}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select City…</option>
                {citiesQuery.data?.items.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <input
                data-testid="create-site-code"
                value={siteCode}
                onChange={(e) => setSiteCode(e.target.value)}
                placeholder="Site Code (e.g. SITE1)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="create-site-name"
                value={siteName}
                onChange={(e) => setSiteName(e.target.value)}
                placeholder="Site Name (e.g. Primary Data Center)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="create-site-address"
                value={siteAddress}
                onChange={(e) => setSiteAddress(e.target.value)}
                placeholder="Address (optional)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="create-site-timezone"
                value={siteTimezone}
                onChange={(e) => setSiteTimezone(e.target.value)}
                placeholder="Timezone (default UTC)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <button
                type="submit"
                data-testid="create-site-submit"
                disabled={
                  createSiteMutation.isPending ||
                  !(siteCityId || selectedCityId) ||
                  !siteCode.trim() ||
                  !siteName.trim()
                }
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createSiteMutation.isPending ? "Creating…" : "Create Site"}
              </button>
            </form>
          )}
          {createSiteMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createSiteMutation.error)}</p>
          )}

          {sitesQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading sites…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">Code</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2">Timezone</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {sitesQuery.data?.items.map((site) => (
                  <tr key={site.id} className="border-b border-slate-900">
                    <td className="py-2 font-mono text-xs font-semibold text-blue-400">{site.code}</td>
                    <td className="py-2 font-medium">{site.name}</td>
                    <td className="py-2 text-slate-400">{site.timezone}</td>
                    <td className="py-2">
                      <button
                        onClick={() => {
                          setSelectedSiteId(site.id);
                          setActiveTab("buildings");
                        }}
                        className="text-xs text-blue-400 hover:underline"
                      >
                        View Buildings →
                      </button>
                    </td>
                  </tr>
                ))}
                {sitesQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-slate-500">
                      No sites created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Level 5: Buildings */}
      {activeTab === "buildings" && (
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-slate-200">Buildings</h2>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if ((buildingSiteId || selectedSiteId) && buildingCode.trim() && buildingName.trim())
                  createBuildingMutation.mutate();
              }}
              className="grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-4"
            >
              <select
                data-testid="create-building-site-select"
                value={buildingSiteId || selectedSiteId}
                onChange={(e) => {
                  setBuildingSiteId(e.target.value);
                  setSelectedSiteId(e.target.value);
                }}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select Site…</option>
                {sitesQuery.data?.items.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
              <input
                data-testid="create-building-code"
                value={buildingCode}
                onChange={(e) => setBuildingCode(e.target.value)}
                placeholder="Building Code (e.g. A)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="create-building-name"
                value={buildingName}
                onChange={(e) => setBuildingName(e.target.value)}
                placeholder="Building Name (e.g. Building A)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <button
                type="submit"
                data-testid="create-building-submit"
                disabled={
                  createBuildingMutation.isPending ||
                  !(buildingSiteId || selectedSiteId) ||
                  !buildingCode.trim() ||
                  !buildingName.trim()
                }
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createBuildingMutation.isPending ? "Creating…" : "Create Building"}
              </button>
            </form>
          )}
          {createBuildingMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createBuildingMutation.error)}</p>
          )}

          {buildingsQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading buildings…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">Code</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2">Site ID</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {buildingsQuery.data?.items.map((b) => (
                  <tr key={b.id} className="border-b border-slate-900">
                    <td className="py-2 font-mono text-xs font-semibold text-blue-400">{b.code}</td>
                    <td className="py-2 font-medium">{b.name}</td>
                    <td className="py-2 font-mono text-xs text-slate-500">{b.site_id.slice(0, 8)}…</td>
                    <td className="py-2">
                      <button
                        onClick={() => {
                          setSelectedBuildingId(b.id);
                          setActiveTab("floors");
                        }}
                        className="text-xs text-blue-400 hover:underline"
                      >
                        View Floors →
                      </button>
                    </td>
                  </tr>
                ))}
                {buildingsQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-slate-500">
                      No buildings created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Level 6: Floors */}
      {activeTab === "floors" && (
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-slate-200">Floors</h2>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if ((floorBuildingId || selectedBuildingId) && floorName.trim()) createFloorMutation.mutate();
              }}
              className="grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-4"
            >
              <select
                data-testid="create-floor-building-select"
                value={floorBuildingId || selectedBuildingId}
                onChange={(e) => {
                  setFloorBuildingId(e.target.value);
                  setSelectedBuildingId(e.target.value);
                }}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select Building…</option>
                {buildingsQuery.data?.items.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name} ({b.code})
                  </option>
                ))}
              </select>
              <input
                data-testid="create-floor-name"
                value={floorName}
                onChange={(e) => setFloorName(e.target.value)}
                placeholder="Floor Name (e.g. First Floor)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                type="number"
                data-testid="create-floor-level"
                value={floorLevelNumber}
                onChange={(e) => setFloorLevelNumber(e.target.value)}
                placeholder="Level Number (e.g. 1)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <button
                type="submit"
                data-testid="create-floor-submit"
                disabled={
                  createFloorMutation.isPending || !(floorBuildingId || selectedBuildingId) || !floorName.trim()
                }
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createFloorMutation.isPending ? "Creating…" : "Create Floor"}
              </button>
            </form>
          )}
          {createFloorMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createFloorMutation.error)}</p>
          )}

          {floorsQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading floors…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">Level</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2">Building ID</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {floorsQuery.data?.items.map((f) => (
                  <tr key={f.id} className="border-b border-slate-900">
                    <td className="py-2 font-mono text-xs font-semibold text-blue-400">{f.level_number}</td>
                    <td className="py-2 font-medium">{f.name}</td>
                    <td className="py-2 font-mono text-xs text-slate-500">{f.building_id.slice(0, 8)}…</td>
                    <td className="py-2">
                      <button
                        onClick={() => {
                          setSelectedFloorId(f.id);
                          setActiveTab("rooms");
                        }}
                        className="text-xs text-blue-400 hover:underline"
                      >
                        View Rooms →
                      </button>
                    </td>
                  </tr>
                ))}
                {floorsQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-slate-500">
                      No floors created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Level 7: Rooms */}
      {activeTab === "rooms" && (
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-slate-200">Rooms</h2>

          {canManage && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if ((roomFloorId || selectedFloorId) && roomCode.trim() && roomName.trim()) createRoomMutation.mutate();
              }}
              className="grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-5"
            >
              <select
                data-testid="create-room-floor-select"
                value={roomFloorId || selectedFloorId}
                onChange={(e) => {
                  setRoomFloorId(e.target.value);
                  setSelectedFloorId(e.target.value);
                }}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select Floor…</option>
                {floorsQuery.data?.items.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name} (Level {f.level_number})
                  </option>
                ))}
              </select>
              <input
                data-testid="create-room-code"
                value={roomCode}
                onChange={(e) => setRoomCode(e.target.value)}
                placeholder="Room Code (e.g. R101)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <input
                data-testid="create-room-name"
                value={roomName}
                onChange={(e) => setRoomName(e.target.value)}
                placeholder="Room Name (e.g. Server Room A)"
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              />
              <select
                data-testid="create-room-type-select"
                value={roomType}
                onChange={(e) => setRoomType(e.target.value)}
                className="rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="data_hall">Data Hall</option>
                <option value="meet_me_room">Meet Me Room</option>
                <option value="pdu_room">PDU Room</option>
                <option value="storage">Storage</option>
                <option value="office">Office</option>
              </select>
              <button
                type="submit"
                data-testid="create-room-submit"
                disabled={
                  createRoomMutation.isPending ||
                  !(roomFloorId || selectedFloorId) ||
                  !roomCode.trim() ||
                  !roomName.trim()
                }
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {createRoomMutation.isPending ? "Creating…" : "Create Room"}
              </button>
            </form>
          )}
          {createRoomMutation.isError && (
            <p className="text-sm text-red-400">{getErrorMessage(createRoomMutation.error)}</p>
          )}

          {roomsQuery.isLoading ? (
            <p className="text-sm text-slate-400">Loading rooms…</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-left text-slate-400">
                  <th className="pb-2">Code</th>
                  <th className="pb-2">Name</th>
                  <th className="pb-2">Type</th>
                  <th className="pb-2">Version</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {roomsQuery.data?.items.map((room) => (
                  <tr key={room.id} className="border-b border-slate-900">
                    <td className="py-2 font-mono text-xs font-semibold text-blue-400">{room.code}</td>
                    <td className="py-2 font-medium">{room.name}</td>
                    <td className="py-2 text-slate-400">{room.room_type}</td>
                    <td className="py-2 font-mono text-xs text-slate-500">v{room.version}</td>
                    <td className="py-2">
                      {canUpdate && (
                        <button
                          onClick={() => {
                            setEditingRoom(room);
                            setEditRoomName(room.name);
                            setEditRoomType(room.room_type);
                          }}
                          className="text-xs text-blue-400 hover:underline"
                        >
                          Edit Room
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {roomsQuery.data?.items.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-4 text-center text-slate-500">
                      No rooms created yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Edit Room Modal */}
      {editingRoom && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-sm border border-slate-700 bg-slate-900 p-5 space-y-4">
            <h3 className="text-base font-semibold text-slate-100">Edit Room: {editingRoom.code}</h3>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (editRoomName.trim()) updateRoomMutation.mutate();
              }}
              className="space-y-3"
            >
              <div>
                <label className="block text-xs uppercase tracking-wide text-slate-400 mb-1">Room Name</label>
                <input
                  value={editRoomName}
                  onChange={(e) => setEditRoomName(e.target.value)}
                  className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                />
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wide text-slate-400 mb-1">Room Type</label>
                <select
                  value={editRoomType}
                  onChange={(e) => setEditRoomType(e.target.value)}
                  className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                >
                  <option value="data_hall">Data Hall</option>
                  <option value="meet_me_room">Meet Me Room</option>
                  <option value="pdu_room">PDU Room</option>
                  <option value="storage">Storage</option>
                  <option value="office">Office</option>
                </select>
              </div>

              {updateRoomMutation.isError && (
                <p className="text-sm text-red-400">{getErrorMessage(updateRoomMutation.error)}</p>
              )}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setEditingRoom(null)}
                  className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-300 hover:bg-slate-700"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={updateRoomMutation.isPending || !editRoomName.trim()}
                  className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                >
                  {updateRoomMutation.isPending ? "Saving…" : "Save Changes"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
