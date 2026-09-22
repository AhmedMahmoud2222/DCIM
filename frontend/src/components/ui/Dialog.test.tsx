import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Dialog } from "./Dialog";

function TestHarness({ busy = false, onClose = vi.fn() }: { busy?: boolean; onClose?: () => void }) {
  return (
    <>
      <button>Outside trigger</button>
      <Dialog titleId="t" descriptionId="d" title="Disconnect?" description="Are you sure?" onClose={onClose} busy={busy}>
        <button data-dialog-initial-focus>Cancel</button>
        <button>Confirm</button>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("has an accessible title and description", () => {
    render(<TestHarness />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Disconnect?");
    expect(dialog).toHaveAccessibleDescription("Are you sure?");
  });

  it("moves initial focus to the data-dialog-initial-focus element", () => {
    render(<TestHarness />);
    expect(screen.getByText("Cancel")).toHaveFocus();
  });

  it("calls onClose on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<TestHarness onClose={onClose} />);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("suppresses Escape while busy (pending-state protection)", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<TestHarness onClose={onClose} busy />);
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("contains Tab focus within the dialog", async () => {
    const user = userEvent.setup();
    render(<TestHarness />);
    const cancel = screen.getByText("Cancel");
    const confirm = screen.getByText("Confirm");
    expect(cancel).toHaveFocus();
    await user.tab();
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus(); // wraps back, never escapes to "Outside trigger"
  });

  it("restores focus to the trigger on unmount", () => {
    function Wrapper() {
      return <TestHarness />;
    }
    const trigger = document.createElement("button");
    trigger.textContent = "Open";
    document.body.appendChild(trigger);
    trigger.focus();
    expect(trigger).toHaveFocus();

    const { unmount } = render(<Wrapper />);
    expect(trigger).not.toHaveFocus();
    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });
});
