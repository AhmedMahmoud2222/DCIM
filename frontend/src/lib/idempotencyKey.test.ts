import { afterEach, describe, expect, it, vi } from "vitest";

import { newIdempotencyKey, SecureRandomUnavailableError } from "@/lib/idempotencyKey";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("newIdempotencyKey (issue #328)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("returns crypto.randomUUID() when the browser provides it", () => {
    const randomUUID = vi.fn(() => "11111111-2222-4333-8444-555555555555");
    vi.stubGlobal("crypto", { randomUUID, getRandomValues: vi.fn() });

    expect(newIdempotencyKey()).toBe("11111111-2222-4333-8444-555555555555");
    expect(randomUUID).toHaveBeenCalledTimes(1);
  });

  it("builds an RFC 4122 v4 UUID from getRandomValues when randomUUID is unavailable", () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.fill(0xff);
      return bytes;
    });
    vi.stubGlobal("crypto", { getRandomValues });

    const key = newIdempotencyKey();

    expect(getRandomValues).toHaveBeenCalledTimes(1);
    expect(getRandomValues.mock.calls[0][0]).toHaveLength(16);
    // All-ones input shows the version and variant bits are forced and nothing else changes.
    expect(key).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
    expect(key).toMatch(UUID_V4);
  });

  it("keeps 122 random bits in the fallback: all-zero input changes only version and variant", () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(0) });

    expect(newIdempotencyKey()).toBe("00000000-0000-4000-8000-000000000000");
  });

  it("never consults Math.random, with or without randomUUID", () => {
    const mathRandom = vi.spyOn(Math, "random");
    const realGetRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);

    newIdempotencyKey();
    vi.stubGlobal("crypto", { getRandomValues: realGetRandomValues });
    newIdempotencyKey();

    expect(mathRandom).not.toHaveBeenCalled();
  });

  it("does not collide across 10,000 fallback keys drawn from the real CSPRNG", () => {
    const realGetRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    vi.stubGlobal("crypto", { getRandomValues: realGetRandomValues });

    const keys = new Set<string>();
    for (let i = 0; i < 10_000; i += 1) keys.add(newIdempotencyKey());

    expect(keys.size).toBe(10_000);
    for (const key of keys) expect(key).toMatch(UUID_V4);
  });

  it.each([
    ["crypto is undefined", undefined],
    ["crypto has neither primitive", {}],
  ])("fails closed when %s", (_label, cryptoValue) => {
    const mathRandom = vi.spyOn(Math, "random");
    vi.stubGlobal("crypto", cryptoValue);

    expect(() => newIdempotencyKey()).toThrow(SecureRandomUnavailableError);
    expect(mathRandom).not.toHaveBeenCalled();
  });
});
