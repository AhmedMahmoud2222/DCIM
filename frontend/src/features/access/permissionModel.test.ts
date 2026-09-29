import { describe, expect, it } from "vitest";

import { applyPermissionState, groupByResource, permissionState, toggleSite } from "./permissionModel";

describe("permissionModel", () => {
  it("keeps allow and deny disjoint", () => {
    const first = applyPermissionState(new Set(), new Set(), "rack:read", "allow");
    expect([...first.allow]).toEqual(["rack:read"]);
    const second = applyPermissionState(first.allow, first.deny, "rack:read", "deny");
    expect(second.allow.size).toBe(0);
    expect([...second.deny]).toEqual(["rack:read"]);
    const third = applyPermissionState(second.allow, second.deny, "rack:read", "none");
    expect(third.allow.size + third.deny.size).toBe(0);
  });

  it("reports the tri-state, deny first", () => {
    expect(permissionState(new Set(["a:b"]), new Set(), "a:b")).toBe("allow");
    expect(permissionState(new Set(["a:b"]), new Set(["a:b"]), "a:b")).toBe("deny");
    expect(permissionState(new Set(), new Set(), "a:b")).toBe("none");
  });

  it("groups the catalog by resource, sorted", () => {
    const grouped = groupByResource([
      { code: "rack:manage", resource: "rack", action: "manage", description: null, site_scoped: true },
      { code: "alarm:read", resource: "alarm", action: "read", description: null, site_scoped: false },
      { code: "rack:read", resource: "rack", action: "read", description: null, site_scoped: true },
    ]);
    expect(grouped.map(([r]) => r)).toEqual(["alarm", "rack"]);
    expect(grouped[1][1].map((p) => p.action)).toEqual(["manage", "read"]);
  });

  it("adds new site grants with no racks selected", () => {
    const make = () => ({ site_id: "s1", rack_scope: "selected" as const, rack_ids: [] as string[] });
    const added = toggleSite([], "s1", make);
    expect(added).toEqual([{ site_id: "s1", rack_scope: "selected", rack_ids: [] }]);
    expect(toggleSite(added, "s1", make)).toEqual([]);
  });
});
