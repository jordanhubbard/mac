// The console's one write: a person posting to a task's board.
//
// Everything else in this console is read-only (src/lib/http.ts). Talking to a
// running agent is the exception the board exists for, so it lives here, in
// one small module, and it can do exactly one thing: append a message, a
// directive or an answer to one task's board. It cannot change a task's state,
// touch a lease, or reach any other route. tests/board.test.tsx holds it to
// that, and tests/readonly.test.ts allows no other module to write.

import { HubError, HubUnreachableError, type FetchLike } from "./http";

export const BOARD_POST_METHOD = "POST";
export const POSTABLE_KINDS = ["message", "directive", "answer"] as const;
export type PostableKind = (typeof POSTABLE_KINDS)[number];

const BOARD_PATH = /^\/tasks\/[A-Za-z0-9_.-]+\/messages$/;

export class BoardWriteRefused extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "BoardWriteRefused";
  }
}

export interface BoardPost {
  kind: PostableKind;
  body: string;
  reply_to?: number;
}

export function boardPath(taskId: string): string {
  return `/tasks/${encodeURIComponent(taskId)}/messages`;
}

export function createBoardPoster(
  tokenProvider: () => string,
  fetchImpl: FetchLike = (path, init) => fetch(path, init),
) {
  return async function postToBoard(
    taskId: string,
    post: BoardPost,
  ): Promise<unknown> {
    const path = boardPath(taskId);
    if (!BOARD_PATH.test(path)) throw new BoardWriteRefused(`not a task board: ${path}`);
    if (!(POSTABLE_KINDS as readonly string[]).includes(post.kind))
      throw new BoardWriteRefused(`a person cannot post a ${post.kind}`);
    const body = post.body.trim();
    if (!body) throw new BoardWriteRefused("empty message");
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": "application/json",
    };
    const token = tokenProvider();
    if (token) headers.Authorization = `Bearer ${token}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("timeout")), 15_000);
    let response: Response;
    try {
      response = await fetchImpl(path, {
        method: BOARD_POST_METHOD,
        headers,
        body: JSON.stringify({
          kind: post.kind,
          body,
          ...(post.reply_to !== undefined ? { reply_to: post.reply_to } : {}),
        }),
        signal: controller.signal,
        cache: "no-store",
      });
    } catch (err) {
      throw new HubUnreachableError(
        `cannot reach hub: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`.trim();
      try {
        const parsed = (await response.json()) as { detail?: string };
        if (parsed && typeof parsed.detail === "string")
          detail = `${response.status} ${parsed.detail}`;
      } catch {
        /* the status line is what we have */
      }
      throw new HubError(response.status, detail);
    }
    return response.json();
  };
}
