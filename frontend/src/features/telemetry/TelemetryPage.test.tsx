import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "@/lib/apiClient";
import { renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { TelemetryPage } from "./TelemetryPage";
import { PerformanceChart } from "./PerformanceChart";
import { PerformanceSeries, performanceCsv } from "./performance";

vi.mock("@/lib/apiClient", () => ({ apiFetch: vi.fn() }));
const sample: PerformanceSeries = {
  series_key:"source", external_identifier:"=source", integration_id:"sensor", metric:"temperature", unit:"K",
  registry_version:null, sample_count:1, minimum:0, maximum:0, average:0, p50:0, p95:0, partial_days:0,
  points:[{start:"2026-01-01T00:00:00Z",end:"2026-01-01T01:00:00Z",minimum:0,maximum:0,average:0,sample_count:1,resolution:"raw"}],
};
beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(async path => path.startsWith("/telemetry/performance") ? {series:[sample]} : []);
});
it("loads historical-only equipment, preserves zero and sends the custom UTC boundaries", async () => {
  const user = userEvent.setup();
  renderWithProviders(<TelemetryPage/>, {route:"/telemetry?equipment=asset&metric=temperature",user:{...VIEWER_TEST_USER,permission_codes:["telemetry:read"]}});
  expect(await screen.findByText("No live readings for this equipment.")).toBeInTheDocument();
  expect(vi.mocked(apiFetch).mock.calls.some(([path]) => path.startsWith("/telemetry/latest?") && new URL(path,"https://local.test").searchParams.get("per_series") === "true")).toBe(true);
  expect(await screen.findByRole("img", {name:/Time series of temperature/})).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Range"), "custom");
  const start = screen.getByLabelText("Start (UTC)");
  const end = screen.getByLabelText("End (UTC)");
  await user.clear(start);
  expect(screen.getByRole("button", {name:/Export chart/})).toBeDisabled();
  await user.type(start,"2026-01-01T00:00");
  await user.clear(end);
  await user.type(end,"2026-01-02T00:00");
  await waitFor(() => expect(vi.mocked(apiFetch).mock.calls.some(([path]) => {
    const url = new URL(path,"https://local.test");
    return url.pathname === "/telemetry/performance" && url.searchParams.get("start") === "2026-01-01T00:00:00.000Z" && url.searchParams.get("end") === "2026-01-02T00:00:00.000Z";
  })).toBe(true));
});
it("skips all requests for a viewer without telemetry read access", () => {
  renderWithProviders(<TelemetryPage/>, {user:VIEWER_TEST_USER});
  expect(screen.getByRole("alert")).toHaveTextContent("Telemetry read access is required");
  expect(apiFetch).not.toHaveBeenCalled();
});
it("exports observed UTC buckets with unit, resolution, zeros and escaped source identifiers", () => {
  const csv = performanceCsv([sample],"asset","site","start","end");
  expect(csv).toContain('"asset","site","start","end","sensor","source","\'=source"');
  expect(csv).toContain('"K","2026-01-01T00:00:00Z","2026-01-01T01:00:00Z","raw",1,0,0,0,0,"observed"');
});
it.each(["=SUM(1,2)", "+formula", "-formula", "@formula", "\tvalue", "\rvalue", '  ="quoted"'])("escapes spreadsheet text %j and includes empty series quality", source => {
  const csv = performanceCsv([{...sample, external_identifier:source, points:[], partial_days:2}], "asset","site","start","end");
  expect(csv).toContain('"' + "'" + source.replace(/"/g, '""') + '"');
  expect(csv).toContain('"","","",0,"","","",2,"no eligible samples"');
});
it.each([
  ["threshold_high", 1, 1, "No breach in eligible samples"],
  ["threshold_high", 1, 2, "Breached in window"],
  ["threshold_low", 1, 1, "No breach in eligible samples"],
  ["threshold_low", 0, 1, "Breached in window"],
])("evaluates %s against %s–%s", async (type,minimum,maximum,expected) => {
  vi.mocked(apiFetch).mockResolvedValue([{ id:"rule", name:"Rule", enabled:true, metric:sample.metric, unit:sample.unit, registry_version:null, rule_type:type, threshold:1 }]);
  renderWithProviders(<PerformanceChart series={{...sample,minimum:Number(minimum),maximum:Number(maximum),partial_days:2}} start="2026-01-01T00:00:00Z" end="2026-01-02T00:00:00Z"/>, {
    user:{...VIEWER_TEST_USER,permission_codes:["alarm:read"]},
  });
  const verdict = await screen.findByText(/Rule ·/);
  expect(verdict).toHaveTextContent(String(expected));
  if (expected === "No breach in eligible samples") expect(verdict).toHaveTextContent("2 partial UTC days excluded");
});
it("connects a skipped raw bucket within cadence and leaves a longer outage disconnected", () => {
  const points = [0, 576, 1440].map(second => {
    const time = Date.parse("2026-01-01T00:00:00Z") + second*1000;
    return {...sample.points[0], start:new Date(time).toISOString(), end:new Date(time+288_000).toISOString()};
  });
  renderWithProviders(<PerformanceChart series={{...sample,points}} start="2026-01-01T00:00:00Z" end="2026-01-02T00:00:00Z" pollSeconds={300}/>);
  expect(screen.getByRole("img").querySelectorAll("line")).toHaveLength(1);
  expect(screen.getByRole("img").querySelectorAll("circle")).toHaveLength(3);
});
