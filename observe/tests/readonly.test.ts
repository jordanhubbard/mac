import { describe, expect, it, vi } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  ALLOWED_METHODS,
  HubError,
  HubUnreachableError,
  MutationAttemptError,
  createReadOnlyFetch,
} from "../src/lib/http";

function okResponse(body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("read-only guarantee", () => {
  it("only permits GET and HEAD", () => {
    expect([...ALLOWED_METHODS]).toEqual(["GET", "HEAD"]);
  });

  it.each(["POST", "PUT", "PATCH", "DELETE", "post", "delete"])(
    "refuses %s before any network call happens",
    async (method) => {
      const spy = vi.fn(async () => okResponse());
      const fetchOnce = createReadOnlyFetch(() => "", spy);
      await expect(fetchOnce("/dashboard/observe", { method })).rejects.toThrow(
        MutationAttemptError,
      );
      expect(spy).not.toHaveBeenCalled();
    },
  );

  it("sends GET with the bearer token and no body", async () => {
    const spy = vi.fn(async () => okResponse({ ok: true }));
    const fetchOnce = createReadOnlyFetch(() => "tok-123", spy);
    await fetchOnce("/dashboard/observe");
    const init = (spy.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer tok-123",
    );
  });

  it("omits Authorization when there is no token", async () => {
    const spy = vi.fn(async () => okResponse());
    await createReadOnlyFetch(() => "", spy)("/dashboard/observe");
    const init = (spy.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("reports an unreachable hub rather than resolving with empty data", async () => {
    const fetchOnce = createReadOnlyFetch(
      () => "",
      async () => {
        throw new TypeError("Failed to fetch");
      },
    );
    await expect(fetchOnce("/dashboard/observe")).rejects.toThrow(
      HubUnreachableError,
    );
  });

  it("surfaces the hub's status code and detail on an error response", async () => {
    const fetchOnce = createReadOnlyFetch(
      () => "",
      async () =>
        new Response(JSON.stringify({ detail: "token missing scope" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
    );
    await expect(fetchOnce("/dashboard/observe")).rejects.toMatchObject({
      name: "HubError",
      status: 403,
      message: "403 token missing scope",
    });
    await expect(fetchOnce("/dashboard/observe")).rejects.toBeInstanceOf(HubError);
  });

  it("aborts rather than hanging forever", async () => {
    const fetchOnce = createReadOnlyFetch(
      () => "",
      (_path, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new Error("aborted")),
          );
        }),
    );
    await expect(
      fetchOnce("/dashboard/observe", { timeoutMs: 5 }),
    ).rejects.toThrow(/no response from hub within/);
  });
});

// ---------------------------------------------------------------------------
// Source-level guard. The runtime check above only protects calls that go
// through readOnlyFetch; this asserts nothing bypasses it.
// ---------------------------------------------------------------------------

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

describe("no mutation anywhere in the source tree", () => {
  const srcRoot = resolve(__dirname, "..", "src");
  const files = sourceFiles(srcRoot);

  it("finds source files to check", () => {
    expect(files.length).toBeGreaterThan(5);
  });

  it("routes every network call through src/lib/http.ts", () => {
    const offenders = files
      .filter((f) => !f.endsWith(join("lib", "http.ts")))
      .filter((f) => /\b(fetch|XMLHttpRequest|EventSource|navigator\.sendBeacon)\s*\(/.test(
        readFileSync(f, "utf8"),
      ));
    expect(offenders).toEqual([]);
  });

  it("contains no mutating HTTP verbs", () => {
    const offenders: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      // Match the verbs only where they'd be used as an HTTP method, i.e.
      // as a quoted string. Prose in comments is fine.
      if (/["'`](POST|PUT|PATCH|DELETE)["'`]/.test(text)) offenders.push(file);
    }
    expect(offenders).toEqual([]);
  });
});
