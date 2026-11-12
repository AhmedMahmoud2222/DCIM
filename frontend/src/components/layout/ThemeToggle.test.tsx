import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { applyTheme, THEME_STORAGE_KEY } from "@/lib/theme";

import { ThemeToggle } from "./ThemeToggle";

describe("ThemeToggle", () => {
  beforeEach(() => {
    window.localStorage.clear();
    applyTheme("light");
  });
  afterEach(() => document.documentElement.removeAttribute("data-theme"));

  it("offers Normal and Dark as a radio group with the current mode checked", () => {
    render(<ThemeToggle />);
    expect(screen.getByRole("radiogroup", { name: "Color mode" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Normal" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "false");
  });

  it("switches the document theme and remembers the choice", async () => {
    render(<ThemeToggle />);
    await userEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "true");

    await userEvent.click(screen.getByRole("radio", { name: "Normal" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });

  it("moves between modes with the arrow keys", async () => {
    render(<ThemeToggle />);
    screen.getByRole("radio", { name: "Normal" }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
