import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { useAuthStore } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER } from "@/test/renderWithProviders";

function renderAt(route: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/login" element={<h1>Login page</h1>} />
        <Route element={<ProtectedRoute />}>
          <Route path="/admin/catalog" element={<h1>Catalog destination</h1>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("ProtectedRoute catalog designer guard", () => {
  beforeEach(() => {
    useAuthStore.getState().clear();
  });

  it("redirects an unauthenticated visitor from /admin/catalog to /login", () => {
    renderAt("/admin/catalog");
    expect(screen.getByRole("heading", { name: "Login page" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Catalog destination" })).not.toBeInTheDocument();
  });

  it("renders /admin/catalog for an authenticated session", () => {
    useAuthStore.getState().setSession("in-memory-token", ADMINISTRATOR_TEST_USER);
    renderAt("/admin/catalog");
    expect(screen.getByRole("heading", { name: "Catalog destination" })).toBeInTheDocument();
  });
});
