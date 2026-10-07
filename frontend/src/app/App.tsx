import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { CatalogHomePage } from "@/features/catalog-designer/CatalogHomePage";
import { ManufacturerDetailPage } from "@/features/catalog-designer/ManufacturerDetailPage";
import { ModelDetailPage } from "@/features/catalog-designer/ModelDetailPage";
import { RevisionCompareView } from "@/features/catalog-designer/RevisionCompareView";
import { RevisionEditorPage } from "@/features/catalog-designer/RevisionEditorPage";
import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { EquipmentDetailPage } from "@/features/equipment/EquipmentDetailPage";
import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import { InstantiatePage } from "@/features/equipment/InstantiatePage";
import { GroupsPage } from "@/features/access/GroupsPage";
import { UsersPage } from "@/features/access/UsersPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { RoomFloorPlanPage } from "@/features/floor-plans/RoomFloorPlanPage";
import { FloorPlansPage } from "@/features/floor-plans/FloorPlansPage";
import { CollectorsPage } from "@/features/integrations/CollectorsPage";
import { DiscoveryPage } from "@/features/integrations/DiscoveryPage";
import { IntegrationsPage } from "@/features/integrations/IntegrationsPage";
import { LocationsPage } from "@/features/locations/LocationsPage";
import { CablesPage } from "@/features/network/CablesPage";
import { PassThroughsPage } from "@/features/network/PassThroughsPage";
import { NeighborReviewPage } from "@/features/network/NeighborReviewPage";
import { ProfilesPage } from "@/features/network/ProfilesPage";
import { TracePage } from "@/features/network/TracePage";
import { InfrastructurePage } from "@/features/locations/InfrastructurePage";
import { SiteDetailPage } from "@/features/locations/SiteDetailPage";
import { ManagedAssetsPage } from "@/features/managed-assets/ManagedAssetsPage";
import { PowerTopologyPage } from "@/features/power/PowerTopologyPage";
import { RackDetailPage } from "@/features/racks/RackDetailPage";
import { RacksPage } from "@/features/racks/RacksPage";
import { Layout3DPage } from "@/features/spatial3d/Layout3DPage";
import { EventsPage } from "@/features/telemetry/EventsPage";

import { queryClient } from "./queryClient";

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<ProtectedRoute />}>
            <Route element={<AppShell />}>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/locations" element={<LocationsPage />} />
              <Route path="/infrastructure" element={<InfrastructurePage />} />
              <Route path="/sites/:siteId" element={<SiteDetailPage />} />
              <Route path="/managed-assets" element={<ManagedAssetsPage />} />
              <Route path="/racks" element={<RacksPage />} />
              <Route path="/racks/:rackId" element={<RackDetailPage />} />
              <Route path="/equipment" element={<EquipmentPage />} />
              <Route path="/equipment/instantiate" element={<InstantiatePage />} />
              <Route path="/equipment/:equipmentId" element={<EquipmentDetailPage />} />
              <Route path="/floor-plans" element={<FloorPlansPage />} />
              <Route path="/floor-plans/room/:roomId" element={<RoomFloorPlanPage />} />
              <Route path="/floor-plans/3d-layout" element={<Layout3DPage />} />
              <Route path="/events" element={<EventsPage />} />
              <Route path="/power" element={<PowerTopologyPage />} />
              <Route path="/collectors" element={<CollectorsPage />} />
              <Route path="/integrations" element={<IntegrationsPage />} />
              <Route path="/discovery" element={<DiscoveryPage />} />
              <Route path="/discovery/neighbors" element={<NeighborReviewPage />} />
              <Route path="/network/profiles" element={<ProfilesPage />} />
              <Route path="/cables" element={<CablesPage />} />
              <Route path="/topology/pass-throughs" element={<PassThroughsPage />} />
              <Route path="/topology/trace" element={<TracePage />} />
              <Route path="/admin/users" element={<UsersPage />} />
              <Route path="/admin/groups" element={<GroupsPage />} />
              <Route path="/admin/catalog" element={<CatalogHomePage />} />
              <Route path="/admin/catalog/manufacturers/:manufacturerId" element={<ManufacturerDetailPage />} />
              <Route path="/admin/catalog/models/:modelId" element={<ModelDetailPage />} />
              <Route path="/admin/catalog/revisions/compare" element={<RevisionCompareView />} />
              <Route path="/admin/catalog/revisions/:revisionId" element={<RevisionEditorPage />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
