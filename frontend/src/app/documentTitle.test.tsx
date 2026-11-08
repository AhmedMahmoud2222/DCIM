import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import { AppShell } from "@/components/layout/AppShell";
import { APP_TITLE } from "@/lib/appTitle";
import { apiFetch } from "@/lib/apiClient";
import { CurrentUser, useAuthStore } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/lib/apiClient", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/apiClient")>()), apiFetch: vi.fn() }));

const ME: CurrentUser = {
  id: "u1",
  email: "ops@example.test",
  full_name: "Sample Operator",
  role_names: ["Operator"],
  permission_codes: ["alarm:read", "dashboard:read", "collector:read"],
};

const SUMMARY = {
  site_summary: { sites: 1, buildings: 1, floors: 1, rooms: 1, racks: 1, equipment: 1, power_nodes: 1 },
  rack_summary: { total_racks: 1, occupied_racks: 1, available_racks: 0 },
  capacity_summary: { total_configured_kw: 10, nodes_with_known_capacity: 1, nodes_with_unknown_capacity: 0 },
  power_summary: { overloaded_nodes: 0, near_capacity_nodes: 0, redundancy_degraded_equipment: 0 },
};

function answer(path: string): unknown {
  if (path.startsWith("/auth/login")) return { access_token: "token" };
  if (path.startsWith("/auth/me")) return ME;
  if (path.startsWith("/auth/logout")) return {};
  if (path.startsWith("/dashboard/summary")) return SUMMARY;
  if (path.startsWith("/dashboard/exceptions") || path.startsWith("/alarms") || path.startsWith("/collectors")) return [];
  return { items: [], total: 0, limit: 50, offset: 0 };
}

function signedIn(route: string) {
  useAuthStore.getState().setSession("token", ME);
  window.history.pushState({}, "", route);
}

describe("document title across sign-in and sign-out", () => {
  beforeEach(() => {
    document.title = APP_TITLE;
    useAuthStore.getState().clear();
    vi.mocked(apiFetch).mockImplementation((async (path: string) => answer(path)) as typeof apiFetch);
  });
  afterEach(() => {
    cleanup();
    useAuthStore.getState().clear();
    window.history.pushState({}, "", "/");
  });

  it("titles the public sign-in page when it is opened directly, and keeps no protected page name", async () => {
    window.history.pushState({}, "", "/login");
    render(<App />);

    expect(await screen.findByRole("textbox", { name: "Email" })).toBeInTheDocument();
    expect(document.title).toBe("Sign in · DCIM Platform");
    expect(document.title).not.toMatch(/Events|Dashboard/);
  });

  it("does not show a protected page's title to a visitor who is sent to /login", async () => {
    window.history.pushState({}, "", "/events");
    render(<App />);

    expect(await screen.findByRole("textbox", { name: "Email" })).toBeInTheDocument();
    expect(document.title).toBe("Sign in · DCIM Platform");
  });

  it("returns to the sign-in title after signing out from /events, then follows a later sign-in and navigation", async () => {
    signedIn("/events");
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(document.title).toBe("Events · DCIM Platform"));

    // Sign out from the Events page: the SPA navigates to /login and must not keep "Events".
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("textbox", { name: "Email" })).toBeInTheDocument();
    expect(document.title).toBe("Sign in · DCIM Platform");

    // Sign in again: the shell takes over, titled for the page it lands on.
    await user.type(screen.getByRole("textbox", { name: "Email" }), "ops@example.test");
    await user.type(screen.getByLabelText("Password"), "a-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(document.title).toBe("Dashboard · DCIM Platform"));

    // ...and follows client-side navigation again.
    await user.click(await screen.findByRole("link", { name: "Events" }));
    await waitFor(() => expect(document.title).toBe("Events · DCIM Platform"));

    // A second sign-out behaves the same.
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("textbox", { name: "Email" })).toBeInTheDocument();
    expect(document.title).toBe("Sign in · DCIM Platform");
  });

  it("hands the title back to the product name when the shell unmounts", () => {
    const { unmount } = renderWithProviders(<AppShell />, { user: ME, route: "/events" });
    expect(document.title).toBe("Events · DCIM Platform");

    unmount();

    expect(document.title).toBe(APP_TITLE);
  });

  it("changes the title page to page without leaving a stale name behind", async () => {
    signedIn("/events");
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(document.title).toBe("Events · DCIM Platform"));
    await user.click(screen.getByRole("link", { name: "Dashboard" }));
    await waitFor(() => expect(document.title).toBe("Dashboard · DCIM Platform"));
  });
});
