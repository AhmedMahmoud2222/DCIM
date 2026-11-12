/** Issue #328 — `Idempotency-Key` generation for mutating requests.
 *
 * Keys come from a CSPRNG only. `crypto.randomUUID()` exists in secure contexts (HTTPS,
 * localhost); `crypto.getRandomValues()` also exists on plain-HTTP origins, so it backs an
 * RFC 4122 version-4 UUID there. With neither primitive the caller gets
 * `SecureRandomUnavailableError` and must not send the request: a guessable or colliding
 * key could replay or block another submission on the same endpoint. */

export class SecureRandomUnavailableError extends Error {
  constructor() {
    super(
      "This browser cannot generate a secure request key, so the request was not sent. " +
        "Open DCIM over HTTPS or use a current browser, then try again.",
    );
    this.name = "SecureRandomUnavailableError";
  }
}

export function newIdempotencyKey(): string {
  const source: Partial<Pick<Crypto, "randomUUID" | "getRandomValues">> | undefined = globalThis.crypto;
  if (typeof source?.randomUUID === "function") return source.randomUUID();
  if (typeof source?.getRandomValues === "function") {
    const bytes = source.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  throw new SecureRandomUnavailableError();
}
