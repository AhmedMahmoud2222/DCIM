import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { deriveFreshness } from "@/features/status/freshness";
import {
  alarmWorkflowModel,
  connectivityModel,
  exceptionSeverityModel,
  lifecycleModel,
} from "@/features/status/statusModels";
import {
  AlarmWorkflowStatus,
  CollectorConnectivity,
  ExceptionSeverity,
  LifecycleStatus,
  MeasurementFreshness,
} from "@/features/status/StatusBadges";

const NOW = Date.parse("2026-10-24T12:00:00.000Z");
const freshness = (ageMs: number | null, interval: number | null = 60) =>
  deriveFreshness({
    sourceType: "equipment_live_metric",
    measuredAt: ageMs == null ? null : new Date(NOW - ageMs).toISOString(),
    expectedIntervalSeconds: interval,
    now: NOW,
  });

/** Every chip must have a visible text value and an icon that is hidden from assistive technology. */
function expectTextAndIcon(container: HTMLElement, text: string) {
  expect(container).toHaveTextContent(text);
  const svg = container.querySelector("svg");
  expect(svg).not.toBeNull();
  expect(svg).toHaveAttribute("aria-hidden", "true");
}

describe("asset lifecycle", () => {
  it.each(["planned", "installed", "active", "maintenance", "decommissioned", "removed"])(
    "keeps the backend word %s and marks it as a lifecycle value",
    (value) => {
      const { container } = render(<LifecycleStatus value={value} />);
      expectTextAndIcon(container, value);
      expect(screen.getByText(value)).toBeInTheDocument();
      expect(screen.getByText("Lifecycle:", { exact: false })).toHaveClass("sr-only");
      expect(lifecycleModel(value).known).toBe(true);
    },
  );

  it("keeps the same hues the old per-page tables used", () => {
    expect(lifecycleModel("planned").tone).toBe("neutral");
    expect(lifecycleModel("installed").tone).toBe("info");
    expect(lifecycleModel("active").tone).toBe("ok");
    expect(lifecycleModel("maintenance").tone).toBe("caution");
    expect(lifecycleModel("decommissioned").tone).toBe("serious");
    expect(lifecycleModel("removed").tone).toBe("danger");
  });

  it.each(["quarantined", "ACTIVE", "toString", "__proto__", ""])(
    "shows an unrecognised value (%j) as itself with the neutral unrecognised look",
    (value) => {
      const model = lifecycleModel(value);
      expect(model.known).toBe(false);
      expect(model.tone).toBe("unknown");
      expect(model.variant).toBe("unrecognised");
      const { container } = render(<LifecycleStatus value={value} />);
      expectTextAndIcon(container, value === "" ? "unknown" : value);
    },
  );

  it("falls back for a missing value without throwing", () => {
    expect(lifecycleModel(null).label).toBe("unknown");
    expect(lifecycleModel(undefined).known).toBe(false);
  });
});

describe("alarm workflow", () => {
  it.each(["ACTIVE", "ACKNOWLEDGED", "CLEARED"])("shows %s as text with an icon", (status) => {
    const { container } = render(<AlarmWorkflowStatus status={status} />);
    expectTextAndIcon(container, status);
    expect(screen.getByText("Alarm status:", { exact: false })).toHaveClass("sr-only");
  });

  it("never uses the danger tone, so an ACTIVE alarm does not read as Critical", () => {
    for (const status of ["ACTIVE", "ACKNOWLEDGED", "CLEARED", "SOMETHING_NEW"]) {
      expect(alarmWorkflowModel(status).tone).not.toBe("danger");
    }
    expect(alarmWorkflowModel("ACTIVE").tone).toBe("attention");
  });

  it("does not say severity and says so in its tooltip", () => {
    const { container } = render(<AlarmWorkflowStatus status="ACTIVE" />);
    expect(container).not.toHaveTextContent(/critical|warning|severity/i);
    expect(container.firstElementChild).toHaveAttribute("title", expect.stringMatching(/does not indicate severity/i));
  });

  it("shows an unrecognised status as itself", () => {
    expect(alarmWorkflowModel("SUPPRESSED")).toMatchObject({ label: "SUPPRESSED", known: false, tone: "unknown" });
    expect(alarmWorkflowModel(null).label).toBe("UNKNOWN");
  });
});

describe("exception severity", () => {
  it.each([
    ["critical", "Critical", "danger"],
    ["warning", "Warning", "caution"],
    ["info", "Info", "info"],
  ])("labels backend severity %s as %s", (severity, label, tone) => {
    const { container } = render(<ExceptionSeverity severity={severity} size="sm" />);
    expectTextAndIcon(container, label);
    expect(exceptionSeverityModel(severity).tone).toBe(tone);
  });

  it("gives each severity a different icon shape, not just a different color", () => {
    const shapes = ["critical", "warning", "info"].map((severity) => {
      const { container, unmount } = render(<ExceptionSeverity severity={severity} />);
      const markup = container.querySelector("svg")!.innerHTML;
      unmount();
      return markup;
    });
    expect(new Set(shapes).size).toBe(3);
  });

  it.each([undefined, null, "", "fatal", "CRITICAL"])("never invents a severity for %j", (severity) => {
    const model = exceptionSeverityModel(severity);
    expect(model).toMatchObject({ label: "Unknown severity", known: false, tone: "unknown" });
    const { container } = render(<ExceptionSeverity severity={severity} />);
    expect(container).not.toHaveTextContent(/^(Critical|Warning|Info)$/);
  });
});

describe("collector connectivity", () => {
  it.each([
    ["healthy", "Healthy", "ok"],
    ["stale", "Stale", "caution"],
    ["offline", "Offline", "danger"],
  ])("keeps the backend classification %s", (health, label, tone) => {
    const { container } = render(<CollectorConnectivity health={health} />);
    expectTextAndIcon(container, label);
    expect(connectivityModel(health).tone).toBe(tone);
  });

  it("shows an unrecognised health value as itself", () => {
    expect(connectivityModel("degraded")).toMatchObject({ label: "degraded", known: false });
    expect(connectivityModel(undefined).label).toBe("Unknown");
  });

  it("is told apart from derived freshness for a screen reader, by tooltip and by icon", () => {
    const { container } = render(
      <div>
        <CollectorConnectivity health="stale" />
        <MeasurementFreshness result={freshness(150_000)} />
      </div>,
    );
    // Both read "Stale", but they announce different dimensions...
    expect(screen.getByText("Collector connectivity:", { exact: false })).toHaveClass("sr-only");
    expect(screen.getByText("Measurement freshness (derived):", { exact: false })).toHaveClass("sr-only");
    // ...explain themselves differently...
    const titles = Array.from(container.querySelectorAll("[title]")).map((el) => el.getAttribute("title"));
    expect(titles.some((t) => /heartbeat/i.test(t ?? ""))).toBe(true);
    expect(titles.some((t) => /derived in the browser/i.test(t ?? ""))).toBe(true);
    // ...and draw different shapes.
    const [connectivityIcon, freshnessIcon] = Array.from(container.querySelectorAll("svg")).map((svg) => svg.innerHTML);
    expect(connectivityIcon).not.toEqual(freshnessIcon);
  });
});

describe("measurement freshness", () => {
  it.each([
    [30_000, "Fresh", "measured 30 s ago"],
    [90_000, "Late", "measured 1 min ago"],
    [300_000, "Stale", "measured 5 min ago"],
  ])("a measurement %i ms old is %s with its real age", (ageMs, label, age) => {
    const { container } = render(<MeasurementFreshness result={freshness(ageMs)} />);
    expectTextAndIcon(container, label);
    expect(screen.getByText(age)).toBeInTheDocument();
  });

  it("says Unknown, with the reason and no age, for missing data", () => {
    render(<MeasurementFreshness result={freshness(null)} />);
    const chip = screen.getByTestId("measurement-freshness");
    expect(within(chip).getByText("Unknown")).toBeInTheDocument();
    expect(chip).toHaveTextContent("no measurement time");
    expect(chip).not.toHaveTextContent(/bFreshb|bagob|b0 sb/);
  });

  it("says Unknown for a missing interval and for a future time, never Fresh", () => {
    const { rerender } = render(<MeasurementFreshness result={freshness(10_000, null)} />);
    expect(screen.getByTestId("measurement-freshness")).toHaveTextContent("no expected poll interval");
    rerender(<MeasurementFreshness result={freshness(-5_000)} />);
    expect(screen.getByTestId("measurement-freshness")).toHaveTextContent("in the future");
    expect(screen.getByTestId("measurement-freshness")).not.toHaveTextContent(/bFreshb/);
  });

  it("uses a different icon for each state", () => {
    const markups = [30_000, 90_000, 300_000, null].map((age) => {
      const { container, unmount } = render(<MeasurementFreshness result={freshness(age)} />);
      const markup = container.querySelector("svg")!.innerHTML;
      unmount();
      return markup;
    });
    expect(new Set(markups).size).toBe(4);
  });
});
