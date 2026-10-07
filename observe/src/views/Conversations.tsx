import { useCallback, useEffect, useRef, useState } from "react";
import type { BoardMessage, ConsoleClient } from "../lib/api";
import { clockTime } from "../lib/format";
import { Panel } from "../components/primitives";
import { authorLabel } from "../components/TaskBoard";

const POLL_MS = 4_000;
const KEEP = 400;

/**
 * Every agent in one place: what each is doing, saying and asking, newest at
 * the bottom, with the questions still waiting for a person pinned on top.
 * Open a task to talk to its agent.
 */
export function ConversationsView({
  client,
  onOpenTask,
}: {
  client: ConsoleClient;
  onOpenTask: (id: string) => void;
}) {
  const [messages, setMessages] = useState<BoardMessage[]>([]);
  const [questions, setQuestions] = useState<BoardMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [read, setRead] = useState(false);
  const cursor = useRef(0);

  const poll = useCallback(async () => {
    try {
      const feed = await client.board(cursor.current);
      cursor.current = feed.cursor;
      if (feed.messages.length)
        setMessages((prev) => [...prev, ...feed.messages].slice(-KEEP));
      setQuestions(feed.open_questions);
      setError(null);
      setRead(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [client]);

  useEffect(() => {
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  const line = (m: BoardMessage) => (
    <li
      key={m.id}
      data-kind={m.kind}
      style={{ padding: "4px 0", borderTop: "1px solid var(--line, #8883)" }}
    >
      <span className="micro num">{clockTime(m.created_at)}</span>{" "}
      <a
        className="rowlink"
        href={`?view=task&task=${encodeURIComponent(m.task_id)}`}
        onClick={(event) => {
          event.preventDefault();
          onOpenTask(m.task_id);
        }}
      >
        {m.task_title || m.task_id}
      </a>{" "}
      <strong>{authorLabel(m)}</strong>{" "}
      {m.kind !== "message" ? <span className="chip">{m.kind}</span> : null}{" "}
      <span style={{ whiteSpace: "pre-wrap" }}>{m.body}</span>
    </li>
  );

  return (
    <div className="grid">
      {error ? (
        <div className="banner serious">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>The board could not be read.</strong> {error}
          </span>
        </div>
      ) : null}
      <Panel
        title="Waiting for you"
        sub="questions agents asked that nobody has answered; open the task to answer"
        wide
      >
        {!read ? (
          <p className="empty">Reading…</p>
        ) : questions.length === 0 ? (
          <p className="empty">No agent is waiting on a person.</p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>{questions.map(line)}</ul>
        )}
      </Panel>
      <Panel title="All agents" sub="every task's board, live" wide>
        {!read ? (
          <p className="empty">Reading…</p>
        ) : messages.length === 0 ? (
          <p className="empty">
            No agent has posted yet. Boards fill as Claude Code agents work
            (MAC_CODING_AGENT=claude).
          </p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>{messages.map(line)}</ul>
        )}
      </Panel>
    </div>
  );
}
