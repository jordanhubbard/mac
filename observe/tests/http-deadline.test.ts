// @vitest-environment node
import { createServer } from "node:http";
import { afterEach, expect, it } from "vitest";
import { createReadOnlyFetch, HubUnreachableError } from "../src/lib/http";

const servers: ReturnType<typeof createServer>[] = [];
afterEach(async () => {
  await Promise.all(
    servers.splice(0).map(
      (server) =>
        new Promise<void>((resolve) => {
          server.closeAllConnections();
          server.close(() => resolve());
        }),
    ),
  );
});

it.each([200, 503])(
  "bounds a stalled %s body and permits the next request",
  async (status) => {
    let first = true;
    const server = createServer((_req, res) => {
      res.writeHead(first ? status : 200, {
        "Content-Type": "application/json",
      });
      if (first) {
        first = false;
        res.flushHeaders();
        res.write("{");
      } else res.end('{"recovered":true}');
    });
    servers.push(server);
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", resolve),
    );
    const address = server.address();
    if (!address || typeof address === "string")
      throw new Error("missing port");
    const get = createReadOnlyFetch(() => "", fetch);
    const url = `http://127.0.0.1:${address.port}`;
    await expect(get(url, { timeoutMs: 100 })).rejects.toBeInstanceOf(
      HubUnreachableError,
    );
    expect(await (await get(url)).json()).toEqual({ recovered: true });
  },
);

it("keeps a stream incremental and cancellable after headers", async () => {
  const server = createServer((_req, res) => {
    res.writeHead(200);
    res.write("first\n");
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing port");
  const controller = new AbortController();
  const response = await createReadOnlyFetch(() => "", fetch)(
    `http://127.0.0.1:${address.port}`,
    {
      timeoutMs: 0,
      signal: controller.signal,
    },
  );
  const reader = response.body!.getReader();
  expect((await reader.read()).done).toBe(false);
  controller.abort();
  await expect(reader.read()).rejects.toThrow();
});
