import { describe, expect, it } from "vitest";

import {
  accessFor,
  ALARM_LIST_LIMIT,
  collectorsNeedingAttention,
  connectivityCounts,
  countLabel,
  DASHBOARD_PERMISSIONS,
  severityCounts,
  sortAlarmsNewestFirst,
  sortExceptionsBySeverity,
} from "@/features/dashboard/dashboardModel";
import type { Alarm } from "@/features/telemetry/api";
import type { CurrentUser } from "@/lib/authStore";
import type { Collector, DashboardExceptionItem } from "@/types";

const user = (...permission_codes: string[]): CurrentUser => ({
  id: "u",
  email: "u@example.test",
  full_name: "U",
  role_names: [],
  permission_codes,
});

const exception = (code: string, severity: string): DashboardExceptionItem =>
  ({ code, severity, object_type: "rack", object_id: code, message: code }) as DashboardExceptionItem;

const collector = (name: string, health: string): Collector =>
  ({ id: name, name, health, collector_type: "edge", site_id: null, status: "active", version_string: null, last_heartbeat_at: null, seconds_since_heartbeat: null }) as unknown as Collector;

const alarm = (id: string, opened_at: string) => ({ id, opened_at, status: "ACTIVE" }) as unknown as Alarm;

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

  it("names the backend permission for each source", () => {
    expect(DASHBOARD_PERMISSIONS).toEqual({
      alarms: "alarm:read",
      exceptions: "dashboard:read",
      collectors: "collector:read",
      inventory: "dashboard:read",
    });
  });
});

describe("countLabel", () => {
  it("is exact below the request limit and says 'or more' at it", () => {
    expect(countLabel(0, ALARM_LIST_LIMIT)).toBe("0");
    expect(countLabel(99, ALARM_LIST_LIMIT)).toBe("99");
    expect(countLabel(100, ALARM_LIST_LIMIT)).toBe("100 or more");
    expect(countLabel(250, ALARM_LIST_LIMIT)).toBe("100 or more");
  });
});

describe("sortAlarmsNewestFirst", () => {
  it("orders by opened time, newest first, without mutating the input", () => {
    const input = [alarm("old", "2026-01-01T00:00:00Z"), alarm("new", "2026-03-01T00:00:00Z"), alarm("mid", "2026-02-01T00:00:00Z")];
    expect(sortAlarmsNewestFirst(input).map((a) => a.id)).toEqual(["new", "mid", "old"]);
    expect(input.map((a) => a.id)).toEqual(["old", "new", "mid"]);
  });

  it("keeps an unparseable time in place instead of throwing", () => {
    const result = sortAlarmsNewestFirst([alarm("a", "not-a-date"), alarm("b", "2026-01-01T00:00:00Z")]);
    expect(result).toHaveLength(2);
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
