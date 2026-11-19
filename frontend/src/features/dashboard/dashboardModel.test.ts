import { describe, expect, it } from "vitest";

import {
  accessFor,
  alarmAssetTarget,
  collectorsNeedingAttention,
  connectivityCounts,
  DASHBOARD_PERMISSIONS,
  exceptionHref,
  exceptionPreview,
  isKnownSeverity,
  severityCounts,
  sortExceptionsBySeverity,
  toggleSeverity,
  unrecognisedAlarmCount,
} from "@/features/dashboard/dashboardModel";
import type { CurrentUser } from "@/lib/authStore";
import type { Collector, DashboardExceptionItem } from "@/types";

const user = (...permission_codes: string[]): CurrentUser => ({
  id: "u",
  email: "u@example.test",
  full_name: "U",
  role_names: [],
  permission_codes,
});

const exception = (code: string, severity: string, object_type = "rack"): DashboardExceptionItem =>
  ({ code, severity, object_type, object_id: code, message: code }) as DashboardExceptionItem;

const collector = (name: string, health: string): Collector =>
  ({ id: name, name, health, collector_type: "edge", site_id: null, status: "active", version_string: null, last_heartbeat_at: null, seconds_since_heartbeat: null }) as unknown as Collector;

describe("accessFor", () => {
  it("is unknown before the profile loads, never denied", () => {
    expect(accessFor(null, "alarm:read")).toBe("unknown");
  });

  it("is granted only for a code the user holds, and denied otherwise", () => {
    expect(accessFor(user("alarm:read"), "alarm:read")).toBe("granted");
    expect(accessFor(user("alarm:manage"), "alarm:read")).toBe("denied");
    expect(accessFor(user(), "alarm:read")).toBe("denied");
  });

  it("matches the exact code, not a prefix or a related manage code", () => {
    expect(accessFor(user("alarm:read_all"), "alarm:read")).toBe("denied");
    expect(accessFor(user("dashboard:manage"), "dashboard:read")).toBe("denied");
  });

  it("names the backend permission for each source, with geography behind location:read", () => {
    expect(DASHBOARD_PERMISSIONS).toEqual({
      alarms: "alarm:read",
      geography: "location:read",
      exceptions: "dashboard:read",
      collectors: "collector:read",
      inventory: "dashboard:read",
    });
  });
});

describe("alarm severity helpers", () => {
  it("knows exactly critical, warning and info", () => {
    expect(["critical", "warning", "info"].every(isKnownSeverity)).toBe(true);
    expect(isKnownSeverity("fatal")).toBe(false);
    expect(isKnownSeverity("CRITICAL")).toBe(false);
    expect(isKnownSeverity(null)).toBe(false);
  });

  it("treats the counter and the selector as one control: picking the active level returns to All", () => {
    expect(toggleSeverity("all", "critical")).toBe("critical");
    expect(toggleSeverity("critical", "critical")).toBe("all");
    expect(toggleSeverity("critical", "warning")).toBe("warning");
  });

  it("derives the unrecognised share only from the server total, never negative", () => {
    expect(unrecognisedAlarmCount({ critical: 1, warning: 2, info: 3, total: 8 })).toBe(2);
    expect(unrecognisedAlarmCount({ critical: 1, warning: 2, info: 3, total: 6 })).toBe(0);
    expect(unrecognisedAlarmCount({ critical: 5, warning: 0, info: 0, total: 3 })).toBe(0);
  });
});

describe("exceptions", () => {
  it("sorts critical, warning, info, then unknown severities, keeping API order within a severity", () => {
    const sorted = sortExceptionsBySeverity([
      exception("i1", "info"),
      exception("u1", "fatal"),
      exception("w1", "warning"),
      exception("c1", "critical"),
      exception("w2", "warning"),
      exception("c2", "critical"),
    ]);
    expect(sorted.map((e) => e.code)).toEqual(["c1", "c2", "w1", "w2", "i1", "u1"]);
  });

  it("does not treat Object.prototype keys or case variants as known severities", () => {
    const sorted = sortExceptionsBySeverity([exception("a", "toString"), exception("b", "CRITICAL"), exception("c", "info")]);
    expect(sorted.map((e) => e.code)).toEqual(["c", "a", "b"]);
  });

  it("counts only what was supplied, with unknown severities kept separate", () => {
    expect(severityCounts([exception("a", "critical"), exception("b", "info"), exception("c", "bogus")])).toEqual({
      critical: 1,
      warning: 0,
      info: 1,
      unrecognised: 1,
    });
    expect(severityCounts([])).toEqual({ critical: 0, warning: 0, info: 0, unrecognised: 0 });
  });

  it("previews the three most severe, counts from the whole list, and calls out hidden Critical", () => {
    const items = [exception("w1", "warning"), ...Array.from({ length: 5 }, (_, i) => exception(`c${i}`, "critical")), exception("i1", "info")];
    const preview = exceptionPreview(items);
    expect(preview.shown.map((e) => e.code)).toEqual(["c0", "c1", "c2"]);
    expect(preview.hidden).toBe(4);
    expect(preview.hiddenCritical).toBe(2);
    expect(preview.counts).toEqual({ critical: 5, warning: 1, info: 1, unrecognised: 0 });
  });

  it("hides nothing at three or fewer", () => {
    expect(exceptionPreview([exception("a", "info")])).toMatchObject({ hidden: 0, hiddenCritical: 0 });
  });

  it("drills equipment to its page and power nodes or unknown types to the power topology", () => {
    expect(exceptionHref({ object_type: "equipment", object_id: "eq 1" })).toBe("/equipment/eq%201");
    expect(exceptionHref({ object_type: "power_node", object_id: "pn-1" })).toBe("/power");
    expect(exceptionHref({ object_type: "rack", object_id: "r-1" })).toBe("/power");
  });
});

describe("collector connectivity", () => {
  const collectors = [collector("c", "healthy"), collector("b", "stale"), collector("a", "offline"), collector("d", "degraded"), collector("e", "offline")];

  it("counts the backend health classification", () => {
    expect(connectivityCounts(collectors)).toEqual({ offline: 2, stale: 1, healthy: 1, unrecognised: 1 });
  });

  it("lists non-healthy collectors only, offline first, then stale, then unrecognised, by name", () => {
    expect(collectorsNeedingAttention(collectors).map((c) => c.name)).toEqual(["a", "e", "b", "d"]);
  });

  it("lists nothing when every collector is healthy", () => {
    expect(collectorsNeedingAttention([collector("a", "healthy")])).toEqual([]);
  });
});

describe("alarmAssetTarget", () => {
  const ALL = ["equipment:read", "rack:read"];
  const asset = (managed_asset_type: string | null | undefined, managed_asset_id: string | null = "1a2b3c4d-5e6f-7a8b-9c0d-112233445566") => ({
    managed_asset_id,
    managed_asset_type,
  });

  it("links equipment to /equipment/<id> for a user with equipment:read", () => {
    expect(alarmAssetTarget(asset("equipment", "eq-1"), ALL)).toEqual({ kind: "link", to: "/equipment/eq-1", label: "Equipment" });
  });

  it("links a rack to /racks/<id> for a user with rack:read, never to /equipment", () => {
    expect(alarmAssetTarget(asset("rack", "rk-1"), ALL)).toEqual({ kind: "link", to: "/racks/rk-1", label: "Rack" });
  });

  it("renders nothing for an alarm without a managed asset", () => {
    expect(alarmAssetTarget(asset("equipment", null), ALL)).toBeNull();
    expect(alarmAssetTarget(asset(null, null), ALL)).toBeNull();
  });

  it("never guesses a link when the type is missing", () => {
    expect(alarmAssetTarget(asset(null), ALL)).toEqual({ kind: "text", label: "Asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset(undefined), ALL)).toEqual({ kind: "text", label: "Asset 1a2b3c4d" });
  });

  it("is a plain reference for types without a detail page, naming known types only", () => {
    expect(alarmAssetTarget(asset("pdu"), ALL)).toEqual({ kind: "text", label: "PDU asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("cable"), ALL)).toEqual({ kind: "text", label: "Cable asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("garbage<script>"), ALL)).toEqual({ kind: "text", label: "Asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("constructor"), ALL)).toEqual({ kind: "text", label: "Asset 1a2b3c4d" });
  });

  it("is a plain reference when the user lacks the detail page's permission or is not loaded", () => {
    expect(alarmAssetTarget(asset("equipment"), ["rack:read"])).toEqual({ kind: "text", label: "Equipment asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("rack"), ["equipment:read"])).toEqual({ kind: "text", label: "Rack asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("rack"), user("rack:read_all").permission_codes)).toEqual({ kind: "text", label: "Rack asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("rack"), null)).toEqual({ kind: "text", label: "Rack asset 1a2b3c4d" });
    expect(alarmAssetTarget(asset("rack"), undefined)).toEqual({ kind: "text", label: "Rack asset 1a2b3c4d" });
  });

  it("encodes the id into the path", () => {
    expect(alarmAssetTarget(asset("rack", "a/b?c"), ALL)).toEqual({ kind: "link", to: "/racks/a%2Fb%3Fc", label: "Rack" });
  });
});
