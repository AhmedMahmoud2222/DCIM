import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useLifecycleNotice } from "@/features/managed-assets/useLifecycleNotice";

function NoticeHarness() {
  const { notice, noticeRef, noticeEventHandlers, showNotice, clearNotice } = useLifecycleNotice();
  return (
    <>
      <button type="button" onClick={() => showNotice("Rack 01 installation status saved.")}>Show notice</button>
      <button type="button" onClick={clearNotice}>Clear notice</button>
      {notice && <p key={notice.id} ref={noticeRef} tabIndex={-1} role="status" {...noticeEventHandlers}>{notice.message}</p>}
    </>
  );
}

afterEach(() => vi.useRealTimers());

describe("useLifecycleNotice", () => {
  it("re-announces identical messages and pauses the timeout while focused", async () => {
    vi.useFakeTimers();
    render(<NoticeHarness />);

    fireEvent.click(screen.getByRole("button", { name: "Show notice" }));
    const first = screen.getByRole("status");
    expect(first).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Show notice" }));
    const repeated = screen.getByRole("status");
    expect(repeated).not.toBe(first);
    expect(repeated).toHaveFocus();

    await act(async () => { vi.advanceTimersByTime(8_000); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    fireEvent.blur(repeated);
    await act(async () => { vi.advanceTimersByTime(7_999); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("clears an old notice when a new transition starts", async () => {
    const user = userEvent.setup();
    render(<NoticeHarness />);
    await user.click(screen.getByRole("button", { name: "Show notice" }));
    expect(screen.getByRole("status")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear notice" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
