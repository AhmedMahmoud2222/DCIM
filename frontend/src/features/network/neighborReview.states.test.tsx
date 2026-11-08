import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/network/api";
import { neighbor, userWith } from "@/features/network/fixtures";
import { NeighborReviewPage } from "@/features/network/NeighborReviewPage";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api");

const page = (...items: ReturnType<typeof neighbor>[]) => ({ items, total: items.length, limit: 200, offset: 0 });
const never = () => new Promise<never>(() => undefined);
const READER = userWith("discovery:read");

beforeEach(() => {
  vi.resetAllMocks();
});

describe("Neighbor Review list states", () => {
  it("renders the evidence in a named, focusable table with scoped headers and one h1", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor()));
    renderWithProviders(<NeighborReviewPage />, { user: READER });

    const table = await screen.findByRole("table", { name: "Discovered neighbors" });
    expect(screen.getByRole("region", { name: "Discovered neighbors table" })).toHaveAttribute("tabindex", "0");
    for (const header of within(table).getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("announces loading and does not say 'No neighbors match.'", async () => {
    vi.mocked(api.listNeighbors).mockReturnValue(never() as never);
    renderWithProviders(<NeighborReviewPage />, { user: READER });

    expect(await screen.findByRole("status")).toHaveTextContent("Loading discovered neighbors…");
    expect(screen.queryByText("No neighbors match.")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says no neighbors match only after a successful empty response", async () => {
    vi.mocked(api.listNeighbors).mockResolvedValue(page());
    renderWithProviders(<NeighborReviewPage />, { user: READER });

    expect(await screen.findByText("No neighbors match.")).toBeInTheDocument();
  });

  it("shows a failure as an alert with a working retry, never as an empty list", async () => {
    vi.mocked(api.listNeighbors).mockRejectedValue(new Error("Upstream timed out"));
    const user = userEvent.setup();
    renderWithProviders(<NeighborReviewPage />, { user: READER });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load discovered neighbors.");
    expect(screen.queryByText("No neighbors match.")).not.toBeInTheDocument();

    vi.mocked(api.listNeighbors).mockResolvedValue(page(neighbor()));
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table", { name: "Discovered neighbors" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("shows Access restricted naming discovery:read for a 403", async () => {
    vi.mocked(api.listNeighbors).mockRejectedValue(new ApiError(403, "Forbidden", "no", null));
    renderWithProviders(<NeighborReviewPage />, { user: READER });

    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("discovery:read")).toBeInTheDocument();
    expect(screen.queryByText("No neighbors match.")).not.toBeInTheDocument();
  });
});
