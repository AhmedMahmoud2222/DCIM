import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { CurrentUser, useAuthStore } from "@/lib/authStore";

export const ADMINISTRATOR_TEST_USER: CurrentUser = {
  id: "user-admin",
  email: "admin@example.com",
  full_name: "Test Administrator",
  role_names: ["Administrator"],
  permission_codes: [
    "catalog:read",
    "catalog:read_draft",
    "catalog:manage",
    "catalog:publish",
    "catalog:retire",
  ],
};

export const VIEWER_TEST_USER: CurrentUser = {
  id: "user-viewer",
  email: "viewer@example.com",
  full_name: "Test Viewer",
  role_names: ["Viewer"],
  permission_codes: ["catalog:read"],
};

export function renderWithProviders(
  ui: ReactElement,
  { route = "/", path, user = ADMINISTRATOR_TEST_USER }: { route?: string; path?: string; user?: CurrentUser | null } = {},
) {
  useAuthStore.getState().setSession("test-access-token", user);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        {path ? (
          <Routes>
            <Route path={path} element={ui} />
          </Routes>
        ) : (
          ui
        )}
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}
