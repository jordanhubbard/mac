// Read-only HTTP client.
//
// This console is an observability surface. It must never be the thing that
// breaks the fleet, so mutation is not merely "not implemented" here — it is
// structurally impossible: `readOnlyFetch` refuses any method other than GET
// or HEAD, and it is the ONLY way the app reaches the network.
//
// tests/readonly.test.ts asserts this, and tests/no-mutation-source.test.ts
// asserts no other module reaches for `fetch` directly.

export const ALLOWED_METHODS = ["GET", "HEAD"] as const;
export type AllowedMethod = (typeof ALLOWED_METHODS)[number];

export class MutationAttemptError extends Error {
  constructor(method: string) {
    super(
      `mac observability console is read-only: refused ${method.toUpperCase()} request`,
    );
    this.name = "MutationAttemptError";
  }
}

/** Raised when the hub answered, but not with success. Carries the status. */
export class HubError extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "HubError";
    this.status = status;
  }
}

/** Raised when the hub could not be reached at all, or did not answer in time. */
export class HubUnreachableError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "HubUnreachableError";
  }
}

export interface ReadOnlyRequest {
  method?: string;
  signal?: AbortSignal;
  accept?: string;
  timeoutMs?: number;
}

export type TokenProvider = () => string;

function assertReadOnly(method: string): AllowedMethod {
  const upper = String(method || "GET").toUpperCase();
  if (upper !== "GET" && upper !== "HEAD")
    throw new MutationAttemptError(upper);
  return upper;
}

export interface FetchLike {
  (input: string, init?: RequestInit): Promise<Response>;
}

export function createReadOnlyFetch(
  tokenProvider: TokenProvider,
  fetchImpl: FetchLike,
) {
  return async function readOnlyFetch(
    path: string,
    request: ReadOnlyRequest = {},
  ): Promise<Response> {
    const method = assertReadOnly(request.method ?? "GET");
    const headers: Record<string, string> = {
      Accept: request.accept ?? "application/json",
    };
    const token = tokenProvider();
    if (token) headers.Authorization = `Bearer ${token}`;

    // A timeout is mandatory. The endpoint this console replaced could hang
    // for minutes; a hung fetch renders as a frozen dashboard, which is the
    // one failure mode we are explicitly trying to remove.
    const controller = new AbortController();
    const timeoutMs = request.timeoutMs ?? 15_000;
    const timer =
      timeoutMs > 0
        ? setTimeout(() => controller.abort(new Error("timeout")), timeoutMs)
        : undefined;
    const abort = () => controller.abort(request.signal?.reason);
    if (request.signal?.aborted) abort();
    else request.signal?.addEventListener("abort", abort, { once: true });

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers,
        signal: controller.signal,
        cache: "no-store",
      });
      // A finite request owns its body deadline too. Returning a buffered
      // response keeps callers' JSON parsing inside that bound. timeout=0
      // is reserved for the incremental subscription stream.
      if (timeoutMs > 0) {
        const body = await response.arrayBuffer();
        response = new Response(
          method === "HEAD" || [204, 205, 304].includes(response.status)
            ? null
            : body,
          {
            status: response.status,
            statusText: response.statusText,
            headers: response.headers,
          },
        );
      }
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      throw new HubUnreachableError(
        timeoutMs > 0 && controller.signal.aborted && !request.signal?.aborted
          ? `no response from hub within ${Math.round(timeoutMs / 1000)}s`
          : `cannot reach hub: ${reason}`,
      );
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      // The caller must still be able to cancel a streaming response body.
      if (timeoutMs > 0 || controller.signal.aborted)
        request.signal?.removeEventListener("abort", abort);
    }

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`.trim();
      try {
        const body = (await response.clone().json()) as { detail?: string };
        if (body && typeof body.detail === "string") {
          detail = `${response.status} ${body.detail}`;
        }
      } catch {
        /* non-JSON error body; the status line is what we have */
      }
      throw new HubError(response.status, detail);
    }
    return response;
  };
}

export type ReadOnlyFetch = ReturnType<typeof createReadOnlyFetch>;
