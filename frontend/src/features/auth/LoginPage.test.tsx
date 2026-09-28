import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LoginPage } from "@/features/auth/LoginPage";
import * as auth from "@/features/auth/useAuth";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/auth/useAuth");

describe("LoginPage accessibility", () => {
  it("associates labels and announces a failed keyboard submission", async () => {
    const login = vi.fn().mockRejectedValue(new Error("invalid"));
    vi.mocked(auth.useAuth).mockReturnValue({ login, accessToken: null, user: null, isAuthenticated: false, logout: vi.fn() });
    const user = userEvent.setup();
    renderWithProviders(<LoginPage />);

    await user.tab();
    expect(screen.getByRole("textbox", { name: "Email" })).toHaveFocus();
    await user.type(screen.getByRole("textbox", { name: "Email" }), "tester@example.com");
    await user.tab();
    expect(screen.getByLabelText("Password")).toHaveFocus();
    await user.type(screen.getByLabelText("Password"), "password");
    await user.keyboard("{Enter}");

    await waitFor(() => expect(login).toHaveBeenCalledWith("tester@example.com", "password"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Login failed. Please try again.");
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
  });
});
