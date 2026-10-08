import { useCallback, useEffect, useRef, useState } from "react";
import type { BoardMessage, ConsoleClient } from "../lib/api";
import { HubError } from "../lib/http";
import type { BoardPost } from "../lib/board";
import { clockTime } from "../lib/format";
import { Panel } from "./primitives";

export type PostToBoard = (taskId: string, post: BoardPost) => Promise<unknown>;

const POLL_MS = 3_000;

export function authorLabel(message: BoardMessage): string {
  if (message.author_kind === "agent") return "agent";
  if (message.author_kind === "hub") return message.kind === "verdict" ? "judge" : "hub";
  return message.author || "person";
}

export function unansweredQuestions(messages: BoardMessage[]): BoardMessage[] {
  const answered = new Set(
    messages.filter((m) => m.kind === "answer" && m.reply_to !== null).map((m) => m.reply_to),
  );
  return messages.filter(
    (m) => m.kind === "question" && m.author_kind === "agent" && !answered.has(m.id),
  );
}

/**
 * One task's conversation, live: what its agent is doing and saying, what
 * people told it, what the judge found, and a box to say something to it. A
 * message sent here reaches the running agent after its next tool call.
 */
export function TaskBoard({
  client,
  taskId,
  post,
}: {
  client: ConsoleClient;
  taskId: string;
  post: PostToBoard;
}) {
  const [messages, setMessages] = useState<BoardMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [directive, setDirective] = useState(false);
  const [replyTo, setReplyTo] = useState<number | null>(null);
  const [sending, setSending] = useState(false);
  const cursor = useRef(0);

  const poll = useCallback(async () => {
    try {
      const page = await client.taskBoard(taskId, cursor.current);
      if (page.messages.length) {
        cursor.current = page.cursor;
        setMessages((prev) => [...prev, ...page.messages]);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [client, taskId]);

  useEffect(() => {
    cursor.current = 0;
    setMessages([]);
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  const send = async () => {
    setSending(true);
    try {
      await post(taskId, {
        kind: replyTo !== null ? "answer" : directive ? "directive" : "message",
        body: draft,
        ...(replyTo !== null ? { reply_to: replyTo } : {}),
      });
      setDraft("");
      setReplyTo(null);
      setError(null);
      await poll();
    } catch (err) {
      setError(
        err instanceof HubError && err.status === 403
          ? "This token can read the board but not write to it; use a token with write access."
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setSending(false);
    }
  };

  const activity = [...messages].reverse().find((m) => m.kind === "activity");
  const shown = messages.filter((m) => m.kind !== "activity");
  const open = unansweredQuestions(messages);

  return (
    <Panel
      title="Conversation"
      sub="the agent's board: live, and it hears what you send here"
      wide
    >
      {error ? <p className="unknown-text">{error}</p> : null}
      {activity ? (
        <p className="micro" data-testid="now">
          now ({clockTime(activity.created_at)}): {activity.body}
        </p>
      ) : null}
      {shown.length === 0 ? (
        <p className="empty">Nothing on this task's board yet.</p>
      ) : (
        <ul className="board" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {shown.map((m) => (
            <li
              key={m.id}
              data-kind={m.kind}
              style={{
                padding: "4px 0",
                borderTop: "1px solid var(--line, #8883)",
                fontWeight: open.some((q) => q.id === m.id) ? 600 : undefined,
              }}
            >
              <span className="micro num">{clockTime(m.created_at)}</span>{" "}
              <strong>{authorLabel(m)}</strong>{" "}
              {m.kind !== "message" ? <span className="chip">{m.kind}</span> : null}{" "}
              <span style={{ whiteSpace: "pre-wrap" }}>{m.body}</span>
              {open.some((q) => q.id === m.id) ? (
                <>
                  {" "}
                  <button className="ghost" type="button" onClick={() => setReplyTo(m.id)}>
                    Answer
                  </button>
                </>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (draft.trim()) void send();
        }}
        style={{ display: "grid", gap: 6, marginTop: 10 }}
      >
        {replyTo !== null ? (
          <span className="micro">
            answering question #{replyTo}{" "}
            <button className="ghost" type="button" onClick={() => setReplyTo(null)}>
              cancel
            </button>
          </span>
        ) : null}
        <textarea
          aria-label="message to the agent"
          rows={3}
          value={draft}
          placeholder={replyTo !== null ? "Your answer" : "Tell the agent something"}
          onChange={(event) => setDraft(event.target.value)}
          style={{ width: "100%", boxSizing: "border-box" }}
        />
        <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {replyTo === null ? (
            <label className="micro">
              <input
                type="checkbox"
                checked={directive}
                onChange={(event) => setDirective(event.target.checked)}
              />{" "}
              directive (an instruction it must follow)
            </label>
          ) : null}
          <button className="primary" type="submit" disabled={sending || !draft.trim()}>
            {sending ? "Sending…" : "Send"}
          </button>
        </span>
      </form>
    </Panel>
  );
}
