import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { AuthBootstrap } from "@/features/auth/AuthBootstrap";
import * as api from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER } from "@/test/renderWithProviders";

vi.mock("@/lib/apiClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/apiClient")>()),
  apiFetch: vi.fn(),
  refreshAccessToken: vi.fn(),
}));

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
    expect(api.apiFetch).toHaveBeenCalledWith("/auth/me", { clearOnRefreshFailure: false });
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "refreshed-token", user: ADMINISTRATOR_TEST_USER, isInitialized: true });
  });

  it("shows login when the refresh cookie is missing or expired", async () => {
    vi.mocked(api.refreshAccessToken).mockRejectedValue(new api.ApiError(401, "Unauthorized", "Refresh failed", null));

    renderProtected();

    expect(await screen.findByRole("heading", { name: "Login page" })).toBeInTheDocument();
    expect(useAuthStore.getState()).toMatchObject({ accessToken: null, user: null, isInitialized: true });
  });

  it("keeps the protected route waiting during a temporary failure and offers retry", async () => {
    vi.mocked(api.refreshAccessToken)
      .mockRejectedValueOnce(new api.ApiError(503, "Unavailable", "Temporary failure", null))
      .mockImplementationOnce(async () => {
        useAuthStore.getState().setAccessToken("restored-on-retry");
        return "restored-on-retry";
      });
    vi.mocked(api.apiFetch).mockResolvedValue(ADMINISTRATOR_TEST_USER);

    const user = userEvent.setup();
    renderProtected();

    expect(await screen.findByRole("alert")).toHaveTextContent("Your session could not be restored");
    expect(api.refreshAccessToken).toHaveBeenNthCalledWith(1, { clearOnFailure: false });
    expect(screen.queryByRole("heading", { name: "Login page" })).not.toBeInTheDocument();
    expect(useAuthStore.getState().isInitialized).toBe(false);
    await user.click(screen.getByRole("button", { name: "Retry session restore" }));

    expect(await screen.findByRole("heading", { name: "Protected page" })).toBeInTheDocument();
  });

  it("keeps bootstrap alive when /auth/me receives 401 and its retry refresh is temporarily unavailable", async () => {
    vi.mocked(api.refreshAccessToken).mockImplementation(async () => {
      useAuthStore.getState().setAccessToken("refreshed-token");
      return "refreshed-token";
    });
    vi.mocked(api.apiFetch).mockRejectedValue(new api.ApiError(503, "Unavailable", "Retry later", null));

    renderProtected();

    expect(await screen.findByRole("alert")).toHaveTextContent("Your session could not be restored");
    expect(api.apiFetch).toHaveBeenCalledWith("/auth/me", { clearOnRefreshFailure: false });
    expect(screen.queryByRole("heading", { name: "Login page" })).not.toBeInTheDocument();
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "refreshed-token", user: null, isInitialized: false });
  });
});
