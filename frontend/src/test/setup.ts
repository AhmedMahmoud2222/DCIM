import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import "@testing-library/jest-dom/vitest";

// RTL's automatic afterEach(cleanup) only self-registers when `afterEach` is a global;
// this project runs Vitest without `test.globals`, so it's wired explicitly here once
// instead of importing `cleanup` in every test file.
afterEach(() => {
  cleanup();
});
