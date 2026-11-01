import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DataTable } from "@/components/data/DataTable";
import { QueryStatus } from "@/components/data/QueryStatus";
import { PageHeader } from "@/components/layout/PageHeader";
import { ApiError } from "@/lib/apiClient";

const query = (over: Partial<Parameters<typeof QueryStatus>[0]["query"]> = {}) => ({
  isLoading: false,
  error: null as unknown,
  data: undefined as unknown,
  fetchStatus: "idle",
  refetch: vi.fn(),
  ...over,
});

describe("QueryStatus", () => {
  it("shows a polite loading status on the first load and nothing once there is data", () => {
    const { rerender } = render(<QueryStatus query={query({ isLoading: true })} what="cables" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent("Loading cables…");

    rerender(<QueryStatus query={query({ data: { items: [] } })} what="cables" />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("treats a first request paused for the network as loading, not as an empty list", () => {
    render(<QueryStatus query={query({ fetchStatus: "paused" })} what="cables" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading cables…");
  });

  it("shows nothing for an idle query with no data and no error (the page decides)", () => {
    const { container } = render(<QueryStatus query={query()} what="cables" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("announces a failure with its message and retries on request, never as an empty list", async () => {
    const refetch = vi.fn();
    const user = userEvent.setup();
    render(<QueryStatus query={query({ error: new Error("Upstream timed out"), refetch })} what="cables" />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load cables.");
    expect(alert).toHaveTextContent("Upstream timed out");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("explains a 403 as Access restricted, naming the permission when known", () => {
    render(<QueryStatus query={query({ error: new ApiError(403, "Forbidden", "no", null) })} what="cables" permission="cable:read" />);

    expect(screen.getByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("cable:read")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("still reports a failed refresh when older data is on screen", () => {
    render(<QueryStatus query={query({ data: { items: [1] }, error: new Error("refresh failed") })} what="cables" />);
    expect(screen.getByRole("alert")).toHaveTextContent("refresh failed");
  });
});

describe("DataTable dense option", () => {
  it("uses the smaller text only when asked", () => {
    const { rerender } = render(<DataTable label="A"><tbody /></DataTable>);
    expect(screen.getByRole("table", { name: "A" }).className).toContain("text-sm");
    rerender(<DataTable label="A" dense><tbody /></DataTable>);
    expect(screen.getByRole("table", { name: "A" }).className).toContain("text-xs");
  });
});

describe("PageHeader", () => {
  it("renders one h1, the description and the actions", () => {
    render(<PageHeader title="Cables" description="Physical links." actions={<button type="button">New cable</button>} />);

    expect(screen.getByRole("heading", { level: 1, name: "Cables" })).toBeInTheDocument();
    expect(screen.getByText("Physical links.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New cable" })).toBeInTheDocument();
  });

  it("omits the description and actions when not given", () => {
    const { container } = render(<PageHeader title="Racks" />);
    expect(container.querySelector("p")).toBeNull();
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });
});
