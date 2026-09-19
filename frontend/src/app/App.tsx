import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { EquipmentDetailPage } from "@/features/equipment/EquipmentDetailPage";
import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { RoomFloorPlanPage } from "@/features/floor-plans/RoomFloorPlanPage";
import { FloorPlansPage } from "@/features/floor-plans/FloorPlansPage";
import { CollectorsPage } from "@/features/integrations/CollectorsPage";
import { DiscoveryPage } from "@/features/integrations/DiscoveryPage";
import { IntegrationsPage } from "@/features/integrations/IntegrationsPage";
import { LocationsPage } from "@/features/locations/LocationsPage";
import { InfrastructurePage } from "@/features/locations/InfrastructurePage";
import { SiteDetailPage } from "@/features/locations/SiteDetailPage";
import { ManagedAssetsPage } from "@/features/managed-assets/ManagedAssetsPage";
import { PowerTopologyPage } from "@/features/power/PowerTopologyPage";
import { RackDetailPage } from "@/features/racks/RackDetailPage";
import { RacksPage } from "@/features/racks/RacksPage";

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
              <Route path="/equipment/:equipmentId" element={<EquipmentDetailPage />} />
              <Route path="/floor-plans" element={<FloorPlansPage />} />
              <Route path="/floor-plans/room/:roomId" element={<RoomFloorPlanPage />} />
              <Route path="/power" element={<PowerTopologyPage />} />
              <Route path="/collectors" element={<CollectorsPage />} />
              <Route path="/integrations" element={<IntegrationsPage />} />
              <Route path="/discovery" element={<DiscoveryPage />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
