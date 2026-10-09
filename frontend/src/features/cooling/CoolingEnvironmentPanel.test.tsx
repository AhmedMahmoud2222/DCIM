import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/cooling/api";
import { CoolingEnvironmentPanel } from "@/features/cooling/CoolingEnvironmentPanel";
import { airflow, capacity, exceptions, heatMap, layout, partialMap, spatialView, unavailableMap, zone } from "@/features/cooling/fixtures";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/cooling/api");

function mockAll(over: { map?: ReturnType<typeof heatMap>; cap?: ReturnType<typeof capacity>; exc?: ReturnType<typeof exceptions> } = {}) {
  vi.mocked(api.getHeatMap).mockResolvedValue(over.map ?? heatMap());
  vi.mocked(api.getCoolingLayout).mockResolvedValue(layout);
  vi.mocked(api.getRoomAirflow).mockResolvedValue(airflow);
  vi.mocked(api.getRoomCapacity).mockResolvedValue(over.cap ?? capacity());
  vi.mocked(api.getRoomExceptions).mockResolvedValue(over.exc ?? exceptions());
}
const renderPanel = () => renderWithProviders(<CoolingEnvironmentPanel roomId="room-1" view={spatialView} />);

describe("CoolingEnvironmentPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockAll();
  });

  it("separates measured sensor markers from interpolated cells and says so", async () => {
    renderPanel();
    expect(await screen.findByTestId("map-state-badge")).toHaveTextContent("Temperature map: Healthy");
    const cells = screen.getByTestId("heat-cells");
    expect(cells).toHaveAttribute("data-provenance", "interpolated");
    expect(cells.querySelectorAll("rect[data-cell-index]").length).toBeGreaterThan(50);
    expect(Array.from(cells.querySelectorAll("rect[data-cell-index]")).every((r) => r.getAttribute("data-provenance") === "interpolated")).toBe(true);
    const sensors = screen.getByTestId("sensor-layer");
    expect(sensors).toHaveAttribute("data-provenance", "measured");
    await waitFor(() => expect(sensors.querySelectorAll("[data-sensor-id]")).toHaveLength(4));
    expect(screen.getByTestId("map-disclaimer")).toHaveTextContent(/not validated CFD, not a measured field/);
    expect(screen.getByTestId("legend-interpolated")).toHaveTextContent("interpolated from sensors, not measured");
  });

  it("shows the legend range and the alarm-rule threshold in text", async () => {
    renderPanel();
    expect(await screen.findByTestId("legend-range")).toHaveTextContent("Minimum 20 degC, maximum 29.9 degC");
    expect(screen.getByTestId("legend-ticks").querySelectorAll("li")).toHaveLength(5);
    expect(screen.getByTestId("legend-thresholds")).toHaveTextContent("High alarm threshold 27 degC (High inlet)");
  });

  it("never calls a map with stale or missing sensors healthy, and makes each sensor state visibly distinct", async () => {
    mockAll({ map: partialMap() });
    renderPanel();
    expect(await screen.findByTestId("map-state-badge")).toHaveTextContent("Partial");
    expect(screen.getByTestId("map-quality")).toHaveAttribute("data-map-state", "partial");
    expect(screen.getByTestId("count-fresh")).toHaveTextContent("2");
    expect(screen.getByTestId("count-stale")).toHaveTextContent("1");
    expect(screen.getByTestId("count-missing")).toHaveTextContent("1");
    expect(screen.getByTestId("map-reasons")).toHaveTextContent(/stale, missing, invalid or unlocated/);
    const layer = screen.getByTestId("sensor-layer");
    await waitFor(() => expect(layer.querySelectorAll("[data-sensor-id]")).toHaveLength(4));
    const stale = layer.querySelector('[data-state="measured_stale"]') as Element;
    const missing = layer.querySelector('[data-state="missing"]') as Element;
    const fresh = layer.querySelector('[data-state="measured_fresh"]') as Element;
    expect(stale.textContent).toContain("◌");
    expect(missing.textContent).toContain("✕");
    expect(fresh.getAttribute("data-used-in-field")).toBe("true");
    expect(stale.getAttribute("data-used-in-field")).toBe("false");
    expect(stale.getAttribute("aria-label")).toMatch(/Measured, stale.*15 min ago.*not used in the field \(stale\)/);
    expect(missing.getAttribute("aria-label")).toMatch(/Missing, no value, never/);
    // stale / missing text is shown, not blank
    expect(within(stale as HTMLElement).getAllByText("24 degC").length).toBeGreaterThan(0);
    expect(within(missing as HTMLElement).getAllByText("Missing").length).toBeGreaterThan(0);
  });

  it("draws no field when there are too few fresh sensors, and explains why", async () => {
    mockAll({ map: unavailableMap() });
    renderPanel();
    expect(await screen.findByTestId("map-state-badge")).toHaveTextContent("Unavailable");
    expect(screen.queryByTestId("heat-cells")).not.toBeInTheDocument();
    expect(screen.getByTestId("map-reasons")).toHaveTextContent("Fewer than three fresh, located sensors");
    expect(screen.getByTestId("coverage")).toHaveTextContent("0% (none)");
    expect(screen.getAllByTestId(/^legend-/).length).toBeGreaterThan(0);
  });

  it("explains a time-skew degradation", async () => {
    mockAll({ map: heatMap({ state: "degraded", state_reasons: ["age_skew_exceeded"], grid: null }) });
    renderPanel();
    expect(await screen.findByTestId("map-state-badge")).toHaveTextContent("Degraded");
    expect(screen.getByTestId("map-reasons")).toHaveTextContent("too far apart in time");
  });

  it("selecting a sensor shows its measured provenance, timestamp, age and field membership", async () => {
    mockAll({ map: partialMap() });
    renderPanel();
    const user = userEvent.setup();
    const layer = await screen.findByTestId("sensor-layer");
    await waitFor(() => expect(layer.querySelector('[data-state="measured_stale"]')).toBeTruthy());
    await user.click(layer.querySelector('[data-state="measured_stale"]') as Element);
    const detail = await screen.findByTestId("detail-sensor");
    expect(within(detail).getByTestId("detail-provenance")).toHaveTextContent("Measured");
    expect(within(detail).getByTestId("detail-state")).toHaveTextContent("Measured, stale");
    expect(within(detail).getByTestId("detail-value")).toHaveTextContent("24 degC (stale: not current)");
    expect(within(detail).getByTestId("detail-used")).toHaveTextContent("No (stale)");
    expect(detail).toHaveTextContent("15 min ago");
    expect(detail).toHaveTextContent("60 s");
  });

  it("selecting a cell with the keyboard reports it as interpolated, never as a reading", async () => {
    renderPanel();
    const field = await screen.findByTestId("heat-cells");
    expect(field).toHaveAttribute("tabindex", "0");
    expect(field).toHaveAccessibleName(/interpolated from sensors, not measured/);
    field.focus();
    fireEvent.keyDown(field, { key: "ArrowRight" });
    const detail = await screen.findByTestId("detail-cell");
    expect(within(detail).getByTestId("detail-provenance")).toHaveTextContent("Interpolated (not measured)");
    expect(within(detail).getByTestId("detail-value")).toHaveTextContent(/^≈ /);
    expect(detail).toHaveTextContent("never a reading");
    expect(screen.getByTestId("cell-cursor")).toBeInTheDocument();
    fireEvent.keyDown(field, { key: "ArrowLeft" });
    expect(screen.getByTestId("cell-cursor")).toBeInTheDocument();
  });

  it("a cell with no coverage says nothing is estimated there", async () => {
    renderPanel();
    const field = await screen.findByTestId("heat-cells");
    field.focus();
    // the last column of the fixture grid is empty: walk right until it is reached
    for (let i = 0; i < 12; i += 1) fireEvent.keyDown(field, { key: "ArrowRight" });
    expect(await screen.findByTestId("detail-cell-empty")).toHaveTextContent("Nothing is estimated here");
  });

  it("hot and cold aisles, containment boundaries and openings are labelled in text", async () => {
    renderPanel();
    const zones = await screen.findByTestId("zone-layer");
    await waitFor(() => expect(zones.querySelector('[data-zone-kind="hot_aisle"]')).toBeTruthy());
    expect(zones.querySelector('[data-zone-kind="hot_aisle"]')).toHaveAttribute("data-containment", "contained");
    expect(zones).toHaveTextContent("Hot aisle · contained");
    expect(zones).toHaveTextContent("Cold aisle");
    expect(zones.querySelector('[data-containment-element="boundary"]')).toBeTruthy();
    expect(zones.querySelector('[data-containment-element="opening"]')).toBeTruthy();
    expect(screen.getByTestId("legend-zones")).toHaveTextContent("contained aisle");
  });

  it("cooling units show type and status text, and admit that their footprint is not modelled", async () => {
    renderPanel();
    const units = await screen.findByTestId("unit-layer");
    await waitFor(() => expect(units.querySelectorAll("[data-unit-id]")).toHaveLength(2));
    const fault = units.querySelector('[data-status="fault"]') as Element;
    expect(fault.getAttribute("aria-label")).toMatch(/CRAC CRAC-2, fault, rated capacity unknown\. Footprint is not modelled/);
    await userEvent.setup().click(units.querySelector('[data-unit-id="u1"]') as Element);
    const detail = await screen.findByTestId("detail-unit");
    expect(detail).toHaveTextContent("80 kW");
    expect(detail).toHaveTextContent("270° (configured)");
    expect(detail).toHaveTextContent("Not modelled; the marker is not to scale");
  });

  it("airflow arrows carry measured vs configured provenance and only drawable elements are drawn", async () => {
    renderPanel();
    const layer = await screen.findByTestId("airflow-layer");
    await waitFor(() => expect(layer.querySelectorAll("[data-airflow-id]").length).toBe(2));
    expect(layer.querySelector('[data-airflow-id="f1"]')).toHaveAttribute("data-provenance", "measured");
    expect(layer.querySelector('[data-airflow-id="u1"]')).toHaveAttribute("data-provenance", "configured");
    expect(layer.querySelector('[data-airflow-id="u2"]')).toBeNull(); // no configured direction => no arrow invented
    expect(layer).toHaveTextContent("configured design 4.5 m³/s");
    await userEvent.setup().click(layer.querySelector('[data-airflow-id="f1"]') as Element);
    const detail = await screen.findByTestId("detail-airflow");
    expect(detail).toHaveTextContent("Measured");
    expect(detail).toHaveTextContent("Modelled");
    expect(detail).toHaveTextContent("No. This is not a CFD result.");
    expect(screen.getByTestId("airflow-note")).toHaveTextContent("1 measured, 1 configured design, 0 modelled; 1 not drawn");
  });

  it("layer toggles remove the matching SVG layers", async () => {
    renderPanel();
    const user = userEvent.setup();
    await screen.findByTestId("heat-cells");
    await user.click(screen.getByRole("checkbox", { name: "Show heat map" }));
    expect(screen.queryByTestId("heat-cells")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Show sensors" }));
    expect(screen.queryByTestId("sensor-layer")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Show airflow" }));
    expect(screen.queryByTestId("airflow-layer")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Show aisles and containment" }));
    expect(screen.queryByTestId("zone-layer")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Show cooling units" }));
    expect(screen.queryByTestId("unit-layer")).not.toBeInTheDocument();
  });

  it("switches to the humidity map with its own request and ramp", async () => {
    renderPanel();
    const user = userEvent.setup();
    await screen.findByTestId("heat-cells");
    vi.mocked(api.getHeatMap).mockResolvedValue(heatMap({ metric: "humidity_percent", unit: "%", presentation_unit: "%" }));
    await user.click(screen.getByRole("button", { name: "Humidity" }));
    await waitFor(() => expect(api.getHeatMap).toHaveBeenCalledWith("room-1", "humidity_percent"));
    expect(await screen.findByText(/Humidity map: Healthy/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Humidity" })).toHaveAttribute("aria-pressed", "true");
  });

  it("lists capacity, headroom and redundancy, and never fills in an unknown", async () => {
    mockAll({
      cap: capacity([
        zone(),
        zone({ zone_id: "sz2", name: "Unrated", installed_rated_kw: null, available_kw: 40, available_complete: false, thermal_load: { ...zone().thermal_load, state: "unknown", thermal_kw: null, quality: null }, headroom_kw: null, utilization_pct: null, level: "unknown", redundancy: { state: "redundant_unverified", pools: [] } }),
        zone({ zone_id: "sz3", name: "Hot", headroom_kw: -10, utilization_pct: 112, level: "critical", redundancy: { state: "degraded", pools: [] } }),
      ]),
    });
    renderPanel();
    const table = await screen.findByTestId("capacity-table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Hall");
    expect(rows[0]).toHaveTextContent("100 kW (37.5% used)");
    expect(rows[0]).toHaveTextContent("redundant");
    expect(within(rows[1]).getByTestId("headroom-cell")).toHaveTextContent("not calculable");
    expect(within(rows[1]).getByTestId("load-cell")).toHaveTextContent("unknown (unknown)");
    expect(rows[1]).toHaveTextContent("40 kW (some unknown)");
    expect(rows[1]).toHaveTextContent("redundant unverified");
    expect(rows[2]).toHaveTextContent("-10 kW (112% used)");
    expect(rows[2]).toHaveTextContent("✕ critical");
    expect(screen.getByTestId("capacity-assumption")).toHaveTextContent("No COP, PUE or efficiency factor");
  });

  it("explains an empty capacity model instead of showing zero", async () => {
    mockAll({ cap: capacity([]) });
    renderPanel();
    expect(await screen.findByTestId("capacity-empty")).toHaveTextContent("No served zone is defined");
  });

  it("lists exceptions with severity glyph, text and source", async () => {
    mockAll({
      exc: exceptions([
        { type: "high_temperature", severity: "critical", subject_type: "sensor", subject_id: "s2", message: "Inlet too hot: alarm active", source: "alarm" },
        { type: "stale_sensor", severity: "warning", subject_type: "sensor", subject_id: "s3", message: "Stale 3: last reading is 900s old.", source: "derived" },
      ]),
    });
    renderPanel();
    const list = await screen.findByTestId("exception-list");
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("✕ critical");
    expect(items[0]).toHaveTextContent("[alarm]");
    expect(items[1]).toHaveTextContent("! warning");
    expect(items[1]).toHaveTextContent("[derived]");
    expect(screen.getByTestId("exception-counts")).toHaveTextContent("2 exception(s): 1 critical, 1 warning");
  });

  it("shows an empty exception list as such", async () => {
    renderPanel();
    expect(await screen.findByTestId("exception-counts")).toHaveTextContent("No exceptions.");
  });

  it("offers a text alternative table of every sensor, keyboard operable", async () => {
    mockAll({ map: partialMap() });
    renderPanel();
    const table = await screen.findByTestId("sensor-table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(4);
    expect(rows[2]).toHaveTextContent("◌ Measured, stale");
    expect(rows[2]).toHaveTextContent("15 min ago");
    expect(rows[3]).toHaveTextContent("✕ Missing");
    expect(rows[3]).toHaveTextContent("missing"); // exclusion reason
    await userEvent.setup().click(within(rows[0]).getByRole("button"));
    expect(await screen.findByTestId("detail-sensor")).toHaveTextContent("Inlet 1");
  });

  it("every marker is keyboard reachable and operable with Enter", async () => {
    renderPanel();
    const layer = await screen.findByTestId("sensor-layer");
    await waitFor(() => expect(layer.querySelector("[data-sensor-id]")).toBeTruthy());
    const marker = layer.querySelector("[data-sensor-id]") as SVGElement;
    expect(marker).toHaveAttribute("tabindex", "0");
    expect(marker).toHaveAttribute("role", "button");
    fireEvent.keyDown(marker, { key: "Enter" });
    expect(await screen.findByTestId("detail-sensor")).toBeInTheDocument();
  });

  it("surfaces an authorization failure as an alert instead of an empty map", async () => {
    vi.mocked(api.getHeatMap).mockRejectedValue(new ApiError(403, "Forbidden", "Missing required permission: telemetry:read", null));
    vi.mocked(api.getRoomExceptions).mockRejectedValue(new ApiError(500, "Error", "boom", null));
    renderPanel();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => a.textContent?.includes("Missing required permission: telemetry:read"))).toBe(true);
    expect(alerts.some((a) => a.textContent?.includes("boom"))).toBe(true);
    expect(screen.queryByTestId("map-state-badge")).not.toBeInTheDocument();
  });

  it("shows loading status while requests are pending", () => {
    vi.mocked(api.getHeatMap).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.getRoomCapacity).mockReturnValue(new Promise(() => {}));
    renderPanel();
    expect(screen.getAllByRole("status").map((s) => s.textContent).join(" ")).toMatch(/Loading temperature map/);
    expect(screen.getAllByRole("status").map((s) => s.textContent).join(" ")).toMatch(/Loading cooling capacity/);
  });

  it("reports no sensors placed in the room", async () => {
    mockAll({ map: heatMap({ sensors: [], grid: null, state: "unavailable", state_reasons: ["insufficient_fresh_sensors"] }) });
    renderPanel();
    expect(await screen.findByTestId("sensors-empty")).toHaveTextContent("No temperature sensor is placed in this room.");
  });

  it("never produces duplicate React keys when many cells, ticks and sensors share the same value", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const base = heatMap();
    const flat = { ...base.grid!, min: 20, max: 20, values: base.grid!.values.map((v) => (v == null ? null : 20)) };
    mockAll({
      map: heatMap({
        grid: flat,
        sensors: [1, 2, 3, 4].map((n) => ({ ...base.sensors[0], sensor_id: `same-${n}`, name: `Same ${n}`, x_mm: 1000 * n, value: 20, presentation_value: 20 })),
      }),
    });
    renderPanel();
    expect(await screen.findByTestId("legend-ticks")).toBeInTheDocument();
    expect(screen.getByTestId("legend-ticks").querySelectorAll("li")).toHaveLength(5); // five ticks, all showing 20
    await waitFor(() => expect(screen.getByTestId("sensor-layer").querySelectorAll("[data-sensor-id]")).toHaveLength(4));
    const keyWarnings = errors.mock.calls.filter((call) => String(call[0]).includes("same key"));
    errors.mockRestore();
    expect(keyWarnings).toEqual([]);
  });

  it("has no autoplay animation: airflow arrows are static, so reduced-motion is respected by construction", async () => {
    renderPanel();
    const layer = await screen.findByTestId("airflow-layer");
    expect(layer.querySelectorAll("animate, animateTransform, animateMotion, style")).toHaveLength(0);
  });
  it("shows an incomplete load as unknown with a labelled lower bound, never as a total", async () => {
    mockAll({
      cap: capacity([
        zone({
          thermal_load: { ...zone().thermal_load, state: "incomplete", load_complete: false, electrical_kw: null, thermal_kw: null, thermal_kw_lower_bound: 30, electrical_kw_lower_bound: 30, quality: "mixed", missing_load_rack_count: 8, placed_equipment_count: 10, modelled_equipment_count: 2, unknown_demand_equipment_count: 8, unmodelled_equipment_count: 8 },
          headroom_kw: null, headroom_verified: false, headroom_upper_bound_kw: 130, utilization_pct: null, level: "unknown", redundancy: { state: "redundant_unverified", pools: [{ scope: "zone_units", reason: null, n_plus_1_verified: null, verification_unavailable_reason: "incomplete_load" }] },
        }),
      ]),
    });
    renderPanel();
    const table = await screen.findByTestId("capacity-table");
    const row = within(table).getAllByRole("row")[1];
    expect(within(row).getByTestId("load-cell")).toHaveTextContent("unknown (incomplete), 8 of 10 placed equipment without a known demand, at least 30 kW known, not a total");
    expect(within(row).getByTestId("load-cell")).not.toHaveTextContent(/^30 kW \(/);
    expect(within(row).getByTestId("headroom-cell")).toHaveTextContent("not calculable (unverified), at most 130 kW");
    expect(within(row).getByTestId("headroom-cell")).not.toHaveTextContent(/^\d+(\.\d+)? kW \(\d/);
    expect(within(row).getByTestId("redundancy-cell")).toHaveTextContent("redundant unverified (N+1 not verified: load incomplete)");
  });

  it("a complete load keeps the plain verified presentation", async () => {
    mockAll({ cap: capacity([zone()]) });
    renderPanel();
    const row = within(await screen.findByTestId("capacity-table")).getAllByRole("row")[1];
    expect(within(row).getByTestId("load-cell")).toHaveTextContent("60 kW (measured)");
    expect(within(row).getByTestId("headroom-cell")).toHaveTextContent("100 kW (37.5% used)");
    expect(within(row).getByTestId("redundancy-cell")).toHaveTextContent(/^redundant$/);
  });

  it("renders without any alarm information when the caller lacks alarm:read", async () => {
    const withheld = heatMap({ thresholds: [], thresholds_withheld: true });
    for (const s of withheld.sensors) delete (s as { active_alarm_count?: number }).active_alarm_count;
    mockAll({ map: withheld });
    renderPanel();
    expect(await screen.findByTestId("map-state-badge")).toHaveTextContent("Temperature map: Healthy");
    expect(screen.queryByTestId("legend-thresholds")).not.toBeInTheDocument();
    expect(screen.queryByText(/active alarm|Alarms/i)).not.toBeInTheDocument();
  });

  it("keys threshold rows by rule, value and unit so legacy-unit rules never collide", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mockAll({
      map: heatMap({
        thresholds: [
          { rule_type: "threshold_high", threshold: 80, unit: "degF", name: "High F" },
          { rule_type: "threshold_high", threshold: 80, unit: "degC", name: "High C" },
        ],
      }),
    });
    renderPanel();
    const list = await screen.findByTestId("legend-thresholds");
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(errors.mock.calls.filter((c) => String(c[0]).includes("same key"))).toHaveLength(0);
    errors.mockRestore();
  });
});
