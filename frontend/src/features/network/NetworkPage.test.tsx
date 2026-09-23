import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";
import * as api from "./api";
import { NetworkPage } from "./NetworkPage";
import type { NetworkTopology } from "./api";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof api>("./api");
  return {
    ...actual,
    getNetworkTopology: vi.fn(),
    getNetworkTrace: vi.fn(),
    createNetworkConnection: vi.fn(),
    disconnectNetworkConnection: vi.fn(),
  };
});

const mockedApi = vi.mocked(api);

const topology: NetworkTopology = {
  devices: [
    { id: "dev-a", asset_tag: "A-1", lifecycle_status: "active", name: "Switch A", device_type: "access_switch", source: "operator", last_observed_at: null },
    { id: "dev-b", asset_tag: "B-1", lifecycle_status: "active", name: "Switch B", device_type: "access_switch", source: "operator", last_observed_at: null },
    { id: "dev-c", asset_tag: "C-1", lifecycle_status: "active", name: "Server C", device_type: "endpoint", source: "operator", last_observed_at: null },
  ],
  interfaces: [
    { id: "if-a1", device_id: "dev-a", name: "Gi0/1", interface_type: "physical", description: null, mac_address: null, role: "data", admin_status: "up", oper_status: "up", speed_mbps: 1000, duplex: null, mtu: null, native_vlan: null, ip_address: null, source: "operator", last_observed_at: null },
    { id: "if-b1", device_id: "dev-b", name: "Gi0/1", interface_type: "physical", description: null, mac_address: null, role: "data", admin_status: "up", oper_status: "up", speed_mbps: 1000, duplex: null, mtu: null, native_vlan: null, ip_address: null, source: "operator", last_observed_at: null },
    { id: "if-c1", device_id: "dev-c", name: "eth0", interface_type: "physical", description: null, mac_address: null, role: "server", admin_status: "up", oper_status: "up", speed_mbps: 1000, duplex: null, mtu: null, native_vlan: null, ip_address: null, source: "operator", last_observed_at: null },
  ],
  connections: [
    { id: "conn-authoritative", interface_a_id: "if-a1", interface_b_id: "if-b1", cable_label: "CAB-1", source: "operator", is_authoritative: true },
  ],
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  return render(<NetworkPage />, { wrapper });
}

/** The topology graph draws each link as an SVG <line> with a nested <title> (a
 * tooltip, not a `title` attribute) — jsdom/Testing Library's *ByTitle queries don't
 * reach into that, so tests select the line itself directly. */
async function findTopologyLine(container: HTMLElement): Promise<SVGLineElement> {
  return waitFor(() => {
    const line = container.querySelector<SVGLineElement>("svg line");
    if (!line) throw new Error("topology line not rendered yet");
    return line;
  });
}

function setSession(permissions: string[]) {
  useAuthStore.getState().setSession("token", { id: "u1", email: "u@example.com", full_name: "U", permissions });
}

beforeEach(() => {
  mockedApi.getNetworkTopology.mockResolvedValue(topology);
  mockedApi.getNetworkTrace.mockResolvedValue({ state: "unknown", statement: "n/a", source_device_id: "dev-a", hops: [] });
});

afterEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().clear();
});

describe("NetworkPage permission-aware rendering", () => {
  it("shows the create-connection form for a writable session (network:manage)", async () => {
    setSession(["network:read", "network:manage"]);
    renderPage();
    expect(await screen.findByRole("heading", { name: "Create authoritative connection" })).toBeInTheDocument();
  });

  it("hides the create-connection form for a read-only session and shows a read-only notice instead", async () => {
    setSession(["network:read"]);
    renderPage();
    expect(await screen.findByRole("heading", { name: "Read-only topology access" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Create authoritative connection" })).not.toBeInTheDocument();
  });

  it("never shows a Disconnect control to a read-only session, even for an authoritative link", async () => {
    setSession(["network:read"]);
    const user = userEvent.setup();
    const { container } = renderPage();
    const line = await findTopologyLine(container);
    await user.click(line);
    expect(await screen.findByText("Selected physical link")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect link" })).not.toBeInTheDocument();
  });
});

describe("NetworkPage observed-link protection", () => {
  it("shows a read-only explanation instead of a Disconnect control for an observed link", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue({
      ...topology,
      connections: [{ id: "conn-observed", interface_a_id: "if-a1", interface_b_id: "if-b1", cable_label: null, source: "collector", is_authoritative: false }],
    });
    const user = userEvent.setup();
    const { container } = renderPage();
    const line = await findTopologyLine(container);
    await user.click(line);
    expect(await screen.findByText(/recorded from collector observation/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect link" })).not.toBeInTheDocument();
  });

  it("treats a non-operator link flagged is_authoritative=true as still protected (mirrors the backend predicate)", async () => {
    // Regression for the gap the backend correction closed: is_authoritative alone is
    // not sufficient. A collector/import/demo row can in principle carry
    // is_authoritative=true, and the UI must not present it as operator-disconnectable.
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue({
      ...topology,
      connections: [{ id: "conn-flagged", interface_a_id: "if-a1", interface_b_id: "if-b1", cable_label: null, source: "collector", is_authoritative: true }],
    });
    const user = userEvent.setup();
    const { container } = renderPage();
    const line = await findTopologyLine(container);
    await user.click(line);
    expect(await screen.findByText(/recorded from collector observation/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect link" })).not.toBeInTheDocument();
  });
});

describe("NetworkPage connection creation", () => {
  it("creates a connection between chosen source/destination interfaces", async () => {
    setSession(["network:read", "network:manage"]);
    // Both endpoints must be free (unused) ports — the base fixture's if-a1/if-b1 are
    // already connected, so this test uses its own topology with two open interfaces.
    mockedApi.getNetworkTopology.mockResolvedValue({ ...topology, connections: [] });
    mockedApi.createNetworkConnection.mockResolvedValue({ id: "new-conn", interface_a_id: "if-a1", interface_b_id: "if-c1", cable_label: null, source: "operator", is_authoritative: true });
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "Create authoritative connection" });
    // The panel renders before the topology query resolves (empty option lists while
    // loading); wait for the actual device options rather than racing the fetch.
    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");
    await within(screen.getByLabelText("Source interface")).findByRole("option", { name: /Gi0\/1/ });
    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a1");
    await user.selectOptions(screen.getByLabelText("Destination device"), "dev-c");
    await within(screen.getByLabelText("Destination interface")).findByRole("option", { name: /eth0/ });
    await user.selectOptions(screen.getByLabelText("Destination interface"), "if-c1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(mockedApi.createNetworkConnection.mock.calls[0]?.[0]).toEqual({ interface_a_id: "if-a1", interface_b_id: "if-c1", cable_label: null }));
  });
});

describe("NetworkPage same-device connections", () => {
  // The backend explicitly permits a physical link between two distinct ports on one
  // device (app/api/v1/network.py: only interface_a_id == interface_b_id is rejected);
  // the UI must offer that pairing, not silently exclude it.
  function twoFreePortsOnDeviceA(): NetworkTopology {
    return {
      ...topology,
      interfaces: [
        ...topology.interfaces,
        { id: "if-a2", device_id: "dev-a", name: "Gi0/2", interface_type: "physical", description: null, mac_address: null, role: "data", admin_status: "up", oper_status: "up", speed_mbps: 1000, duplex: null, mtu: null, native_vlan: null, ip_address: null, source: "operator", last_observed_at: null },
      ],
      connections: [],
    };
  }

  it("keeps the selected source device available as a destination device", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue(twoFreePortsOnDeviceA());
    const user = userEvent.setup();
    renderPage();

    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");

    expect(within(screen.getByLabelText("Destination device")).getByRole("option", { name: "Switch A" })).toBeInTheDocument();
  });

  it("creates a connection between two distinct free interfaces on the same device", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue(twoFreePortsOnDeviceA());
    mockedApi.createNetworkConnection.mockResolvedValue({ id: "new-conn", interface_a_id: "if-a1", interface_b_id: "if-a2", cable_label: null, source: "operator", is_authoritative: true });
    const user = userEvent.setup();
    renderPage();

    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");
    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a1");
    await user.selectOptions(screen.getByLabelText("Destination device"), "dev-a");
    await within(screen.getByLabelText("Destination interface")).findByRole("option", { name: /Gi0\/2/ });
    await user.selectOptions(screen.getByLabelText("Destination interface"), "if-a2");

    expect(screen.getByRole("button", { name: "Create connection" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(mockedApi.createNetworkConnection.mock.calls[0]?.[0]).toEqual({ interface_a_id: "if-a1", interface_b_id: "if-a2", cable_label: null }));
  });

  it("never offers the selected source interface as a destination-interface option", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue(twoFreePortsOnDeviceA());
    const user = userEvent.setup();
    renderPage();

    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");
    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a1");
    await user.selectOptions(screen.getByLabelText("Destination device"), "dev-a");

    await within(screen.getByLabelText("Destination interface")).findByRole("option", { name: /Gi0\/2/ });
    expect(within(screen.getByLabelText("Destination interface")).queryByRole("option", { name: /^Gi0\/1/ })).not.toBeInTheDocument();
  });

  it("keeps an occupied interface unavailable on both ends, even on the same device", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue({
      ...twoFreePortsOnDeviceA(),
      // if-a1 is already wired to if-b1 — it must not appear as a selectable port.
      connections: [{ id: "conn-existing", interface_a_id: "if-a1", interface_b_id: "if-b1", cable_label: null, source: "operator", is_authoritative: true }],
    });
    const user = userEvent.setup();
    renderPage();

    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");
    expect(within(screen.getByLabelText("Source interface")).queryByRole("option", { name: /Gi0\/1 /i })).not.toBeInTheDocument();
    expect(within(screen.getByLabelText("Source interface")).getByRole("option", { name: /Gi0\/2/ })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a2");
    await user.selectOptions(screen.getByLabelText("Destination device"), "dev-a");
    expect(within(screen.getByLabelText("Destination interface")).queryByRole("option", { name: /Gi0\/1 /i })).not.toBeInTheDocument();
  });

  it("clears an invalid destination-interface selection when the source interface changes to match it", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue(twoFreePortsOnDeviceA());
    const user = userEvent.setup();
    renderPage();

    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");
    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a1");
    await user.selectOptions(screen.getByLabelText("Destination device"), "dev-a");
    await user.selectOptions(screen.getByLabelText("Destination interface"), "if-a2");
    expect(screen.getByLabelText("Destination interface")).toHaveValue("if-a2");

    // Re-pointing the source interface at the port currently selected as the
    // destination must clear that now-invalid destination choice, never leave the same
    // interface selected at both ends.
    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a2");

    expect(screen.getByLabelText("Destination interface")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Create connection" })).toBeDisabled();
  });

  it("still supports cross-device connections unchanged", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.getNetworkTopology.mockResolvedValue({ ...topology, connections: [] });
    mockedApi.createNetworkConnection.mockResolvedValue({ id: "new-conn", interface_a_id: "if-a1", interface_b_id: "if-c1", cable_label: null, source: "operator", is_authoritative: true });
    const user = userEvent.setup();
    renderPage();

    await within(screen.getByLabelText("Source device")).findByRole("option", { name: "Switch A" });
    await user.selectOptions(screen.getByLabelText("Source device"), "dev-a");
    await user.selectOptions(screen.getByLabelText("Source interface"), "if-a1");
    await user.selectOptions(screen.getByLabelText("Destination device"), "dev-c");
    await within(screen.getByLabelText("Destination interface")).findByRole("option", { name: /eth0/ });
    await user.selectOptions(screen.getByLabelText("Destination interface"), "if-c1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(mockedApi.createNetworkConnection.mock.calls[0]?.[0]).toEqual({ interface_a_id: "if-a1", interface_b_id: "if-c1", cable_label: null }));
  });
});

describe("NetworkPage disconnect confirmation", () => {
  async function selectAuthoritativeLink() {
    const user = userEvent.setup();
    const { container } = renderPage();
    const line = await findTopologyLine(container);
    await user.click(line);
    await user.click(await screen.findByRole("button", { name: "Disconnect link" }));
    return user;
  }

  it("opens a confirmation dialog before disconnecting, and topology refreshes on success", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.disconnectNetworkConnection.mockResolvedValue(undefined);
    const user = await selectAuthoritativeLink();

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Disconnect physical link?")).toBeInTheDocument();

    mockedApi.getNetworkTopology.mockClear();
    await user.click(within(dialog).getByRole("button", { name: "Disconnect link" }));

    await waitFor(() => expect(mockedApi.disconnectNetworkConnection.mock.calls[0]?.[0]).toBe("conn-authoritative"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // Success invalidates the topology query, which triggers a refetch.
    await waitFor(() => expect(mockedApi.getNetworkTopology).toHaveBeenCalled());
  });

  it("shows the server's conflict message and keeps the dialog open on failure (e.g. a protected observed link)", async () => {
    setSession(["network:read", "network:manage"]);
    mockedApi.disconnectNetworkConnection.mockRejectedValue(new ApiError(409, "Conflict", "This link was recorded from collector observation and is protected.", null));
    const user = await selectAuthoritativeLink();

    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Disconnect link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This link was recorded from collector observation and is protected.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("cancels without calling the API", async () => {
    setSession(["network:read", "network:manage"]);
    const user = await selectAuthoritativeLink();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockedApi.disconnectNetworkConnection).not.toHaveBeenCalled();
  });
});
