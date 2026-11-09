import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { AuthBootstrap } from "@/features/auth/AuthBootstrap";
import * as api from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER } from "@/test/renderWithProviders";

vi.mock("@/lib/apiClient", () => ({ apiFetch: vi.fn(), refreshAccessToken: vi.fn() }));

function renderProtected() {
  return render(
    <MemoryRouter initialEntries={["/private"]}>
      <AuthBootstrap />
      <Routes>
        <Route path="/login" element={<h1>Login page</h1>} />
        <Route element={<ProtectedRoute />}>
          <Route path="/private" element={<h1>Protected page</h1>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("AuthBootstrap", () => {
  beforeEach(() => {
    useAuthStore.setState({ accessToken: null, user: null, isInitialized: false });
    vi.mocked(api.apiFetch).mockReset();
    vi.mocked(api.refreshAccessToken).mockReset();
  });

  it("restores the user from the refresh cookie before showing a protected route", async () => {
    vi.mocked(api.refreshAccessToken).mockImplementation(async () => {
      useAuthStore.getState().setAccessToken("refreshed-token");
      return "refreshed-token";
    });
    vi.mocked(api.apiFetch).mockResolvedValue(ADMINISTRATOR_TEST_USER);

    renderProtected();

    expect(screen.getByRole("status")).toHaveTextContent("Restoring your session");
    expect(await screen.findByRole("heading", { name: "Protected page" })).toBeInTheDocument();
    expect(api.apiFetch).toHaveBeenCalledWith("/auth/me");
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "refreshed-token", user: ADMINISTRATOR_TEST_USER, isInitialized: true });
  });

  it("shows login when the refresh cookie is missing or expired", async () => {
    vi.mocked(api.refreshAccessToken).mockRejectedValue(new Error("Refresh failed"));

    renderProtected();

    expect(await screen.findByRole("heading", { name: "Login page" })).toBeInTheDocument();
    expect(useAuthStore.getState()).toMatchObject({ accessToken: null, user: null, isInitialized: true });
  });
});
