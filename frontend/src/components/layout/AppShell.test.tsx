import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/layout/AppShell";
import { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";

const NETWORK_READER_USER: CurrentUser = {
  id: "user-net-reader",
  email: "net-reader@example.com",
  full_name: "Network Reader",
  role_names: ["Operator"],
  permission_codes: ["network_profile:read"],
};

// Holds every permission that gates a link, so all 22 links render.
const ALL_LINKS_USER: CurrentUser = {
  id: "user-all-links",
  email: "all-links@example.com",
  full_name: "All Links",
  role_names: ["Administrator"],
  permission_codes: [
    "location:read",
    "alarm:read",
    "discovery:read",
    "network_profile:read",
    "cable:read",
    "user:read",
    "group:read",
    "catalog:manage",
  ],
};

// The menu as it was before the mobile drawer: same labels, same order, same routes. Checkpoint 1
// must not change any of it.
const EXPECTED_LINKS: ReadonlyArray<[label: string, href: string]> = [
  ["Dashboard", "/"],
  ["Infrastructure", "/infrastructure"],
  ["Locations", "/locations"],
  ["Managed Assets", "/managed-assets"],
  ["Racks", "/racks"],
  ["Equipment", "/equipment"],
  ["Floor Plans", "/floor-plans"],
  ["3D Layout", "/floor-plans/3d-layout"],
  ["Power Topology", "/power"],
  ["Events", "/events"],
  ["Alarm Rules", "/alarm-rules"],
  ["Collectors", "/collectors"],
  ["Integrations", "/integrations"],
  ["Discovery", "/discovery"],
  ["Neighbors", "/discovery/neighbors"],
  ["Device Profiles", "/network/profiles"],
  ["Cables", "/cables"],
  ["Pass-throughs", "/topology/pass-throughs"],
  ["Trace", "/topology/trace"],
  ["Asset Catalog", "/admin/catalog"],
  ["Users", "/admin/users"],
  ["Groups", "/admin/groups"],
];

/** Stubs window.matchMedia. `desktop` is the result for the (min-width: 40rem) query. */
function stubViewport(desktop: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: desktop,
    media: "(min-width: 40rem)",
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
  };
  vi.stubGlobal("matchMedia", () => query);
  return {
    setDesktop(next: boolean) {
      query.matches = next;
      act(() => listeners.forEach((listener) => listener()));
    },
  };
}

function sidebar(): HTMLElement {
  const element = document.getElementById("app-sidebar");
  if (!element) throw new Error("sidebar not found");
  return element;
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.style.overflow = "";
});

describe("AppShell navigation terminology", () => {
  it("renders Device Profiles in primary navigation for network_profile:read users", async () => {
    renderWithProviders(<AppShell />, { user: NETWORK_READER_USER });

    const navLink = screen.getByRole("link", { name: "Device Profiles" });
    expect(navLink).toBeInTheDocument();
    expect(navLink).toHaveAttribute("href", "/network/profiles");
  });
});

describe("AppShell menu content is unchanged", () => {
  it("renders the same 22 links, in the same order, with the same routes", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    const links = within(screen.getByRole("navigation", { name: "Primary" })).getAllByRole("link");
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual(EXPECTED_LINKS);
  });

  it("keeps the Admin heading and hides permission-gated links the user lacks", () => {
    renderWithProviders(<AppShell />, { user: NETWORK_READER_USER });

    expect(screen.queryByText("Admin")).not.toBeInTheDocument();
    for (const hidden of ["Locations", "Alarm Rules", "Neighbors", "Cables", "Trace", "Users", "Groups", "Asset Catalog"]) {
      expect(screen.queryByRole("link", { name: hidden })).not.toBeInTheDocument();
    }
  });
});

describe("AppShell on a wide screen", () => {
  it("keeps an always-visible landmark sidebar and shows no menu button", () => {
    stubViewport(true);
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    expect(sidebar().tagName).toBe("ASIDE");
    expect(sidebar()).not.toHaveAttribute("inert");
    expect(screen.queryByRole("button", { name: "Open main menu" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("AppShell mobile navigation drawer", () => {
  it("starts closed: a menu button is shown and the drawer is inert", () => {
    stubViewport(false);
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    const menuButton = screen.getByRole("button", { name: "Open main menu" });
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
    expect(menuButton).toHaveAttribute("aria-controls", "app-sidebar");
    expect(sidebar()).toHaveAttribute("inert");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByTestId("menu-scrim")).not.toBeInTheDocument();
  });

  it("keeps the skip link as the first keyboard stop", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveFocus();
  });

  it("opens as a modal dialog, moves focus inside, and locks page scroll", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    await user.click(screen.getByRole("button", { name: "Open main menu" }));

    const dialog = screen.getByRole("dialog", { name: "Main menu" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).not.toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "Open main menu" })).toHaveAttribute("aria-expanded", "true");
    await waitFor(() => expect(screen.getByRole("button", { name: "Close menu" })).toHaveFocus());
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("closes on Escape, returns focus to the menu button, and unlocks scroll", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    await user.click(screen.getByRole("button", { name: "Open main menu" }));
    await screen.findByRole("dialog", { name: "Main menu" });

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(sidebar()).toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "Open main menu" })).toHaveFocus();
    expect(document.body.style.overflow).toBe("");
  });

  it("keeps Tab and Shift+Tab inside the open drawer", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    await user.click(screen.getByRole("button", { name: "Open main menu" }));
    await screen.findByRole("dialog", { name: "Main menu" });

    const closeButton = screen.getByRole("button", { name: "Close menu" });
    const lastLink = screen.getByRole("link", { name: "Groups" });

    lastLink.focus();
    await user.tab();
    expect(closeButton).toHaveFocus();

    await user.tab({ shift: true });
    expect(lastLink).toHaveFocus();
  });

  it("closes from the scrim and from the Close menu button, returning focus to the menu button", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    await user.click(screen.getByRole("button", { name: "Open main menu" }));
    await user.click(screen.getByTestId("menu-scrim"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open main menu" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Open main menu" }));
    await user.click(screen.getByRole("button", { name: "Close menu" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open main menu" })).toHaveFocus();
  });

  it("closes after a navigation link is chosen", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    await user.click(screen.getByRole("button", { name: "Open main menu" }));

    await user.click(screen.getByRole("link", { name: "Racks" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(sidebar()).toHaveAttribute("inert");
  });

  it("closes and switches to the wide layout when the screen grows while it is open", async () => {
    const viewport = stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    await user.click(screen.getByRole("button", { name: "Open main menu" }));
    await screen.findByRole("dialog", { name: "Main menu" });

    viewport.setDesktop(true);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open main menu" })).not.toBeInTheDocument();
    expect(sidebar().tagName).toBe("ASIDE");
    expect(sidebar()).not.toHaveAttribute("inert");
    expect(document.body.style.overflow).toBe("");
  });
});
