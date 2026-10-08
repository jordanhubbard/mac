import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import {
  BOARD_POST_METHOD,
  BoardWriteRefused,
  POSTABLE_KINDS,
  createBoardPoster,
} from "../src/lib/board";
import { HubError } from "../src/lib/http";
import { TaskBoard, unansweredQuestions } from "../src/components/TaskBoard";
import type { BoardMessage, ConsoleClient } from "../src/lib/api";

function ok(body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("the board writer is the console's only write, and a narrow one", () => {
  it("posts one message to one task's board with the bearer token", async () => {
    const spy = vi.fn(async () => ok({ id: 1 }));
    const post = createBoardPoster(() => "tok", spy);
    await post("task_abc", { kind: "directive", body: "  only touch parser.c  " });
    const [path, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/tasks/task_abc/messages");
    expect(init.method).toBe(BOARD_POST_METHOD);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
    expect(JSON.parse(String(init.body))).toEqual({ kind: "directive", body: "only touch parser.c" });
  });

  it("can only say what a person may say", async () => {
    expect([...POSTABLE_KINDS]).toEqual(["message", "directive", "answer"]);
    const spy = vi.fn(async () => ok());
    const post = createBoardPoster(() => "", spy);
    for (const kind of ["verdict", "nudge", "status", "done"]) {
      await expect(post("task_1", { kind: kind as never, body: "x" })).rejects.toThrow(
        BoardWriteRefused,
      );
    }
    await expect(post("task_1", { kind: "message", body: "   " })).rejects.toThrow(
      BoardWriteRefused,
    );
    expect(spy).not.toHaveBeenCalled();
  });

  it("cannot be pointed at another route", async () => {
    const spy = vi.fn(async () => ok());
    const post = createBoardPoster(() => "", spy);
    await expect(
      post("../agents/agent_1/claim-next", { kind: "message", body: "x" }),
    ).rejects.toThrow(BoardWriteRefused);
    expect(spy).not.toHaveBeenCalled();
  });

  it("reports the hub's refusal", async () => {
    const post = createBoardPoster(
      () => "",
      async () => new Response(JSON.stringify({ detail: "needs write" }), { status: 403 }),
    );
    await expect(post("task_1", { kind: "message", body: "x" })).rejects.toBeInstanceOf(HubError);
  });

  it("is the only source file that names a mutating verb", () => {
    const text = readFileSync(resolve(__dirname, "..", "src", "lib", "board.ts"), "utf8");
    expect(text.match(/["'`](POST|PUT|PATCH|DELETE)["'`]/g)).toEqual(['"POST"']);
  });
});

const msg = (over: Partial<BoardMessage>): BoardMessage => ({
  id: 1,
  task_id: "task_1",
  author_kind: "agent",
  author: "agent_a",
  kind: "message",
  body: "",
  reply_to: null,
  metadata: {},
  created_at: "2026-10-07T12:00:00Z",
  ...over,
});

describe("the task conversation panel", () => {
  it("knows which questions are still waiting", () => {
    const messages = [
      msg({ id: 1, kind: "question", body: "region?" }),
      msg({ id: 2, kind: "question", body: "tabs?" }),
      msg({ id: 3, author_kind: "human", kind: "answer", reply_to: 1, body: "eu" }),
    ];
    expect(unansweredQuestions(messages).map((m) => m.id)).toEqual([2]);
  });

  it("shows the board live and answers a question from it", async () => {
    const page = {
      task_id: "task_1",
      cursor: 3,
      messages: [
        msg({ id: 1, kind: "activity", body: "Bash: make test" }),
        msg({ id: 2, kind: "status", body: "found the bug" }),
        msg({ id: 3, kind: "question", body: "keep the old flag?" }),
      ],
    };
    const client = {
      taskBoard: vi.fn(async (_id: string, after: number) =>
        after === 0 ? page : { task_id: "task_1", cursor: after, messages: [] },
      ),
    } as unknown as ConsoleClient;
    const post = vi.fn(async () => ({}));
    render(<TaskBoard client={client} taskId="task_1" post={post} />);
    await screen.findByText("found the bug");
    expect(screen.getByTestId("now").textContent).toContain("Bash: make test");
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));
    fireEvent.change(screen.getByLabelText("message to the agent"), {
      target: { value: "yes, keep it" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("task_1", {
        kind: "answer",
        body: "yes, keep it",
        reply_to: 3,
      }),
    );
  });
});
