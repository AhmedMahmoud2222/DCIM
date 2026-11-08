import http from "node:http";
import { AddressInfo } from "node:net";

/** Deterministic local stand-in for a ServiceNow instance and a webhook receiver. No external tenant is used.
 * `failNextWebhooks(n)` makes the next n webhook posts answer 503 so the UI can show the retry state. */
export interface MockProvider {
  port: number;
  webhooks: { body: string; signature: string | null; deliveryId: string | null; timestamp: string | null }[];
  tickets: Record<string, Record<string, string>>;
  requests: { method: string; path: string }[];
  failNextWebhooks(n: number): void;
  close(): Promise<void>;
}

export async function startMockProvider(port: number): Promise<MockProvider> {
  let failing = 0;
  const state: MockProvider = {
    port, webhooks: [], tickets: {}, requests: [],
    failNextWebhooks(n) { failing = n; },
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString();
      const url = new URL(req.url ?? "/", `http://127.0.0.1:${port}`);
      state.requests.push({ method: req.method ?? "", path: url.pathname });
      const json = (status: number, doc: unknown) => {
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(doc));
      };
      if (url.pathname === "/hook") {
        if (failing > 0) { failing -= 1; return json(503, { error: "unavailable" }); }
        state.webhooks.push({ body, signature: req.headers["x-dcim-signature"] as string | null, deliveryId: req.headers["x-dcim-delivery-id"] as string | null, timestamp: req.headers["x-dcim-timestamp"] as string | null });
        return json(204, {});
      }
      if (!req.headers.authorization) return json(401, {});
      const view = (t: Record<string, string>) => ({ sys_id: t.sys_id, number: t.number, state: t.state });
      if (url.pathname === "/api/now/table/incident" && req.method === "GET") {
        const key = (url.searchParams.get("sysparm_query") ?? "").split("=")[1];
        const found = Object.values(state.tickets).filter((t) => t.correlation_id === key);
        return json(200, { result: found.map(view) });
      }
      if (url.pathname === "/api/now/table/incident" && req.method === "POST") {
        const sysId = [...Array(32)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
        const t = { sys_id: sysId, number: `INC${String(Object.keys(state.tickets).length + 1).padStart(7, "0")}`, state: "1", ...JSON.parse(body) };
        state.tickets[sysId] = t;
        return json(201, { result: view(t) });
      }
      const m = url.pathname.match(/^\/api\/now\/table\/incident\/([0-9a-f]{32})$/);
      if (m && req.method === "PATCH" && state.tickets[m[1]]) {
        Object.assign(state.tickets[m[1]], JSON.parse(body));
        return json(200, { result: view(state.tickets[m[1]]) });
      }
      return json(404, {});
    });
  });
  await new Promise<void>((resolve) => server.listen(port, "127.0.0.1", resolve));
  state.port = (server.address() as AddressInfo).port;
  return state;
}
