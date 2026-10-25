// Minimal typings for the two Node modules that palette.test.ts and typography.test.ts need to read index.css.
// The project has no @types/node, and adding it would change package.json (and with it the CI
// route) for a single test file, so only the exact functions used are declared here.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
  export function existsSync(path: string): boolean;
}

declare module "node:path" {
  const path: { resolve(...segments: string[]): string };
  export default path;
}
