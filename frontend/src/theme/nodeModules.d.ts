// Minimal typings for the two Node modules that palette.test.ts needs to read index.css.
// The project has no @types/node, and adding it would change package.json (and with it the CI
// route) for a single test file, so only the exact functions used are declared here.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
}

declare module "node:path" {
  const path: { resolve(...segments: string[]): string };
  export default path;
}
