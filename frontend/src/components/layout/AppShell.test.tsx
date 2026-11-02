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

// Holds every permission that gates a link, so all 23 links render.
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
    "power:read",
    "user:read",
    "group:read",
    "catalog:manage",
  ],
};

// Checkpoint 2 menu: Dashboard, then five groups. Every route existed before; only the order, the
// grouping, six labels and the new Power & Forecast entry changed.
const EXPECTED_GROUPS: ReadonlyArray<[group: string, links: ReadonlyArray<[label: string, href: string]>]> = [
  [
    "Monitoring",
    [
      ["Events", "/events"],
      ["Alarm Rules", "/alarm-rules"],
      ["Power & Forecast", "/power/analytics"],
    ],
  ],
  [
    "Infrastructure",
    [
      ["Sites & Rooms", "/infrastructure"],
      ["Floor Plans", "/floor-plans"],
      ["3D Layout", "/floor-plans/3d-layout"],
      ["Racks", "/racks"],
      ["Equipment", "/equipment"],
      ["Managed Assets", "/managed-assets"],
      ["Power Topology", "/power"],
    ],
  ],
  [
    "Network",
    [
      ["Cables", "/cables"],
      ["Path Trace", "/topology/trace"],
      ["Pass-throughs", "/topology/pass-throughs"],
      ["Network Profiles", "/network/profiles"],
    ],
  ],
  [
    "Data Sources",
    [
      ["Discovery", "/discovery"],
      ["Neighbor Review", "/discovery/neighbors"],
      ["Collectors", "/collectors"],
      ["Integrations", "/integrations"],
    ],
  ],
  [
    "Administration",
    [
      ["Location Hierarchy", "/locations"],
      ["Asset Catalog", "/admin/catalog"],
      ["Users", "/admin/users"],
      ["Groups", "/admin/groups"],
    ],
  ],
];

const EXPECTED_LINKS: ReadonlyArray<[label: string, href: string]> = [
  ["Dashboard", "/"],
  ...EXPECTED_GROUPS.flatMap(([, links]) => links),
];

function viewerWith(...codes: string[]): CurrentUser {
  return { id: "viewer", email: "viewer@example.com", full_name: "Viewer", role_names: ["Operator"], permission_codes: codes };
}

function primaryNav(): HTMLElement {
  return screen.getByRole("navigation", { name: "Primary" });
}

function linkLabels(): string[] {
  return within(primaryNav())
    .getAllByRole("link")
    .map((link) => link.textContent ?? "");
}

function groupButtons(): string[] {
  return within(primaryNav())
    .queryAllByRole("button")
    .map((button) => button.textContent ?? "");
}

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

const NO_NETWORK_USER: CurrentUser = {
  id: "user-no-net",
  email: "viewer@example.com",
  full_name: "Plain Viewer",
  role_names: ["Viewer"],
  permission_codes: ["catalog:read"],
};

describe("AppShell navigation terminology", () => {
  it("renders Network Profiles in primary navigation for network_profile:read users", async () => {
    renderWithProviders(<AppShell />, { user: NETWORK_READER_USER });

    const navLink = screen.getByRole("link", { name: "Network Profiles" });
    expect(navLink).toBeInTheDocument();
    expect(navLink).toHaveAttribute("href", "/network/profiles");
    expect(screen.queryByRole("link", { name: "Device Profiles" })).toBeNull();
  });

  it("hides profile navigation from users without network_profile:read", async () => {
    renderWithProviders(<AppShell />, { user: NO_NETWORK_USER });

    expect(screen.queryByRole("link", { name: "Network Profiles" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Device Profiles" })).toBeNull();
  });
});

describe("AppShell grouped navigation", () => {
  it("renders Dashboard and five groups, with all 23 links in the approved order and routes", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    const links = within(primaryNav()).getAllByRole("link");
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual(EXPECTED_LINKS);
    expect(groupButtons()).toEqual(EXPECTED_GROUPS.map(([group]) => group));
  });

  it("puts each link in its own group", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    for (const [group, links] of EXPECTED_GROUPS) {
      const button = screen.getByRole("button", { name: group });
      const list = document.getElementById(button.getAttribute("aria-controls") ?? "");
      expect(list).not.toBeNull();
      const labels = within(list as HTMLElement)
        .getAllByRole("link")
        .map((link) => link.textContent);
      expect(labels).toEqual(links.map(([label]) => label));
    }
  });

  it("no longer shows the old labels", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    for (const old of ["Infrastructure", "Locations", "Neighbors", "Trace", "Admin"]) {
      expect(screen.queryByRole("link", { name: old })).not.toBeInTheDocument();
    }
  });

  it("shows only the links a user may see, and drops a group whose links are all hidden", () => {
    renderWithProviders(<AppShell />, { user: NETWORK_READER_USER });

    expect(linkLabels()).toEqual([
      "Dashboard",
      "Events",
      "Sites & Rooms",
      "Floor Plans",
      "3D Layout",
      "Racks",
      "Equipment",
      "Managed Assets",
      "Network Profiles",
      "Discovery",
      "Collectors",
      "Integrations",
    ]);
    expect(groupButtons()).toEqual(["Monitoring", "Infrastructure", "Network", "Data Sources"]);
    expect(screen.queryByRole("button", { name: "Administration" })).not.toBeInTheDocument();
  });

  it("matches the gates for an Operator, a power analyst and a user with no extra permissions", () => {
    const operator = renderWithProviders(<AppShell />, { user: viewerWith("alarm:read", "cable:read", "location:read") });
    expect(linkLabels()).toHaveLength(16);
    expect(linkLabels()).not.toContain("Power & Forecast");
    expect(linkLabels()).toContain("Path Trace");
    expect(linkLabels()).toContain("Location Hierarchy");
    operator.unmount();

    const analyst = renderWithProviders(<AppShell />, { user: viewerWith("alarm:read", "power:read") });
    expect(linkLabels()).toHaveLength(14);
    expect(linkLabels()).toContain("Power & Forecast");
    analyst.unmount();

    renderWithProviders(<AppShell />, { user: viewerWith() });
    expect(linkLabels()).toHaveLength(11);
    expect(groupButtons()).toEqual(["Monitoring", "Infrastructure", "Data Sources"]);
  });

  it("shows both power links only with power:read", () => {
    // power:manage alone does not show them: the backend needs power:read for both pages.
    const without = renderWithProviders(<AppShell />, { user: viewerWith("power:manage") });
    expect(screen.queryByRole("link", { name: "Power & Forecast" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Power Topology" })).not.toBeInTheDocument();
    without.unmount();

    renderWithProviders(<AppShell />, { user: viewerWith("power:read") });
    expect(screen.getByRole("link", { name: "Power & Forecast" })).toHaveAttribute("href", "/power/analytics");
    expect(screen.getByRole("link", { name: "Power Topology" })).toHaveAttribute("href", "/power");
  });

  it("shows the Asset Catalog link only to catalog administrators", () => {
    const permissionOnly = renderWithProviders(<AppShell />, { user: viewerWith("catalog:manage") });
    expect(screen.queryByRole("link", { name: "Asset Catalog" })).not.toBeInTheDocument();
    permissionOnly.unmount();

    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    expect(screen.getByRole("link", { name: "Asset Catalog" })).toBeInTheDocument();
  });
});

describe("AppShell group disclosure", () => {
  it("starts with every group expanded and exposes aria-expanded and aria-controls", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    for (const [group] of EXPECTED_GROUPS) {
      const button = screen.getByRole("button", { name: group });
      expect(button).toHaveAttribute("aria-expanded", "true");
      expect(document.getElementById(button.getAttribute("aria-controls") ?? "")).not.toBeNull();
    }
  });

  it("collapses a group from the keyboard and removes its links from the tab order", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    const network = screen.getByRole("button", { name: "Network" });

    network.focus();
    await user.keyboard("{Enter}");

    expect(network).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "Cables" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Path Trace" })).not.toBeInTheDocument();
    await user.keyboard(" ");
    expect(network).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Cables" })).toBeInTheDocument();
  });

  it("marks the link that owns the current page, including a detail page", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER, route: "/sites/abc" });

    expect(screen.getByRole("link", { name: "Sites & Rooms" })).toHaveAttribute("aria-current", "page");
    expect(within(primaryNav()).getAllByRole("link").filter((link) => link.hasAttribute("aria-current"))).toHaveLength(1);
  });

  it("highlights 3D Layout and not Floor Plans on the 3D layout page", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER, route: "/floor-plans/3d-layout" });

    expect(screen.getByRole("link", { name: "3D Layout" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Floor Plans" })).not.toHaveAttribute("aria-current");
  });

  it("keeps a collapsed group collapsed until the user opens it, and keeps the focus trap on visible links", async () => {
    stubViewport(false);
    const user = userEvent.setup();
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });
    await user.click(screen.getByRole("button", { name: "Open main menu" }));
    await screen.findByRole("dialog", { name: "Main menu" });
    await user.click(screen.getByRole("button", { name: "Administration" }));

    // The last visible tab stop is now the Administration button; Tab from it wraps to Close menu.
    screen.getByRole("button", { name: "Administration" }).focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Close menu" })).toHaveFocus();
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

describe("AppShell page title and landmarks", () => {
  it.each([
    ["/", "Dashboard · DCIM Platform"],
    ["/events", "Events · DCIM Platform"],
    ["/sites/abc", "Sites & Rooms · DCIM Platform"],
    ["/floor-plans/3d-layout", "3D Layout · DCIM Platform"],
    ["/topology/trace", "Path Trace · DCIM Platform"],
  ])("names the page for %s in the document title", (route, title) => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER, route });

    expect(document.title).toBe(title);
  });

  it("falls back to the product name on a route that no link owns", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER, route: "/no-such-page" });

    expect(document.title).toBe("DCIM Platform");
  });

  it("gives the sidebar landmark a name, so it stays distinguishable from a page's own aside", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER, route: "/events" });

    const sidebar = document.getElementById("app-sidebar") as HTMLElement;
    expect(sidebar).toHaveAttribute("aria-label", "Application sidebar");
    expect(sidebar.tagName.toLowerCase()).toBe("aside");
  });

  it("has exactly one main landmark and one banner", () => {
    renderWithProviders(<AppShell />, { user: ALL_LINKS_USER });

    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getAllByRole("banner")).toHaveLength(1);
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });
});
