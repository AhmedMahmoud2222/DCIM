import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import { sha256Hex } from "@/features/dashboard/basemap/sha256";

const text = (value: string) => new TextEncoder().encode(value);

describe("sha256Hex", () => {
  it.each([
    ["", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    ["abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
    ["abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"],
  ])("matches the NIST vector for %j", (message, digest) => {
    expect(sha256Hex(text(message))).toBe(digest);
  });

  it("matches the NIST vector for one million 'a'", () => {
    expect(sha256Hex(new Uint8Array(1_000_000).fill(0x61))).toBe("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
  });

  it("agrees with Node's crypto across block boundaries and arbitrary bytes", () => {
    let seed = 7;
    for (const length of [1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 1000, 4097]) {
      const bytes = new Uint8Array(length);
      for (let i = 0; i < length; i += 1) {
        seed = (seed * 1103515245 + 12345) >>> 0;
        bytes[i] = seed >>> 24;
      }
      expect(sha256Hex(bytes), `length ${length}`).toBe(createHash("sha256").update(bytes).digest("hex"));
    }
  });
});
