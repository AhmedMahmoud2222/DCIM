import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Provided by the Vitest runtime; declared here because the project has no @types/node.
declare const __dirname: string;

const srcDir = path.resolve(__dirname, "..");
const read = (relative: string) => readFileSync(path.resolve(srcDir, relative), "utf8");
const css = read("index.css");

/** Control Room components that must use semantic tokens only, so a different theme can restyle them by
 * redefining tokens. Pages that still contain older markup (forms, buttons and panels in the Tailwind
 * slate scale) are deliberately not listed; they are tracked in the limitation register. */
const TOKEN_ONLY = [
  "components/data/DataTable.tsx",
  "components/data/QueryStatus.tsx",
  "components/layout/PageHeader.tsx",
  "components/feedback/AccessRestricted.tsx",
  "features/status/StatusBadges.tsx",
  "features/dashboard/DashboardSection.tsx",
  "features/dashboard/DashboardPage.tsx",
];

/** Components that sit on a dark panel and must never use the text shades below 4.5:1 on it. */
const READABLE_TEXT = [
  ...TOKEN_ONLY,
  "components/layout/AppShell.tsx",
  "features/telemetry/EventsPage.tsx",
  "features/telemetry/AlarmRulesPage.tsx",
  "features/integrations/CollectorsPage.tsx",
  "features/integrations/DiscoveryPage.tsx",
  "features/integrations/IntegrationsPage.tsx",
  "features/network/NeighborReviewPage.tsx",
  "features/access/UsersPage.tsx",
  "features/access/GroupsPage.tsx",
  "features/racks/RacksPage.tsx",
  "features/equipment/EquipmentPage.tsx",
];

const PALETTE = "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black";
const RAW_COLOR = new RegExp(`(?:^|[\\s"'\`:])(?:[a-z-]+:)*(?:bg|text|border|ring|divide|from|to|via|fill|stroke|outline|placeholder)-(?:${PALETTE})(?:-[0-9]+)?(?:/[0-9]+)?(?=[\\s"'\`]|$)`, "g");
const TOKEN_CLASS = /(?:^|[\s"'`:])(?:[a-z-]+:)*(?:bg|text|border|ring|fill|stroke|outline)-((?:surface|fg|line|tone|side|action|link|focus)[a-z-]*)(?:\/[0-9]+)?(?=[\s"'`]|$)/g;

describe("theme readiness of the Control Room components", () => {
  it.each(TOKEN_ONLY)("%s uses no raw palette, white or black colour classes", (file) => {
    const found = Array.from(read(file).matchAll(RAW_COLOR)).map((match) => match[0].trim());
    expect(found).toEqual([]);
  });

  it.each(TOKEN_ONLY)("%s only names colour tokens that index.css defines", (file) => {
    const undefinedTokens = Array.from(read(file).matchAll(TOKEN_CLASS))
      .map((match) => match[1])
      .filter((name) => !new RegExp(`--color-${name}\\s*:`).test(css));
    expect(Array.from(new Set(undefinedTokens))).toEqual([]);
  });

  it("defines the semantic tokens a theme must replace, in the single static theme block", () => {
    for (const name of ["surface-page", "surface-panel", "surface-hover", "surface-inset", "fg-primary", "fg-secondary", "line-subtle", "link", "focus", "side-selected-fg"]) {
      expect(css, name).toMatch(new RegExp(`--color-${name}\\s*:`));
    }
    expect(css).toMatch(/@theme\s+static\s*\{/);
  });
});

describe("text contrast guard", () => {
  // slate-500 and slate-600 are about 3.3 to 3.9:1 on the dark panels, below the 4.5:1 AA minimum for text.
  it.each(READABLE_TEXT)("%s does not use the low-contrast slate-500 or slate-600 text shades", (file) => {
    expect(read(file)).not.toMatch(/\btext-slate-(500|600)\b/);
  });
});

describe("scroll regions contain hidden screen-reader text", () => {
  // `sr-only` is absolutely positioned. In a scrolling region that is not itself positioned it escapes the
  // clipping and widens the whole page (a 41 px sideways scroll at 320 px on Equipment).
  it.each([
    ["components/data/DataTable.tsx", /role="region"[\s\S]{0,200}?className=\{`relative /],
    ["features/telemetry/EventsPage.tsx", /role="region" aria-label="Events table" tabIndex=\{0\} className="relative overflow-x-auto"/],
    ["features/telemetry/AlarmRulesPage.tsx", /role="region" aria-label="Alarm rules table" tabIndex=\{0\} className="relative overflow-x-auto"/],
  ])("%s", (file, pattern) => {
    expect(read(file)).toMatch(pattern);
  });
});

describe("narrow-screen layout guards", () => {
  it("lets the Events tab list wrap instead of widening the page", () => {
    expect(read("features/telemetry/EventsPage.tsx")).toMatch(/role="tablist" aria-label="Events view" className="flex flex-wrap gap-2"/);
  });

  it("gives Discovery a shrinkable single-column grid below the lg breakpoint", () => {
    const page = read("features/integrations/DiscoveryPage.tsx");
    expect(page).toContain('className="grid grid-cols-1 gap-6 lg:grid-cols-2"');
    expect((page.match(/<div className="min-w-0">/g) ?? []).length).toBe(2);
  });

  it("underlines the links the Dashboard places inside running text, so they do not rely on colour", () => {
    const page = read("features/dashboard/DashboardPage.tsx");
    expect((page.match(/className="text-link underline underline-offset-2"/g) ?? []).length).toBe(3);
    expect(page).not.toContain('className="text-link hover:underline"');
  });
});
