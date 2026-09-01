import { useEffect, useRef, useState } from "react";
import { Panel } from "../components/primitives";
import { clockTime, shortId } from "../lib/format";
import type { ConsoleClient, NewsFeed, NewsItem } from "../lib/api";
import { agentStatusColor, taskStateColor } from "../lib/states";

export function NewsView({
  client,
  refreshKey,
  onOpenTask,
}: {
  client: ConsoleClient;
  refreshKey: number;
  onOpenTask: (id: string) => void;
}) {
  const [feed, setFeed] = useState<NewsFeed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);

  useEffect(() => {
    const mine = ++request.current;
    void client.news(150).then(
      (next) => {
        if (mine !== request.current) return;
        setFeed(next);
        setError(null);
      },
      (reason) => {
        if (mine === request.current) setError(reason instanceof Error ? reason.message : String(reason));
      },
    );
  }, [client, refreshKey]);

  return (
    <Panel
      title="Fleet news"
      accent="var(--series-4)"
      sub={feed ? `${feed.items.length} significant transitions · newest first` : undefined}
    >
      {error ? <div className="banner serious">News feed unavailable: {error}</div> : null}
      {!feed && !error ? <p className="empty">Reading fleet activity…</p> : null}
      {feed && !feed.items.length ? <p className="empty">No significant activity recorded.</p> : null}
      {feed?.items.length ? <NewsRows rows={feed.items} onOpenTask={onOpenTask} /> : null}
    </Panel>
  );
}

function NewsRows({ rows, onOpenTask }: { rows: NewsItem[]; onOpenTask: (id: string) => void }) {
  return (
    <ol className="news-board" aria-label="Fleet activity">
      {rows.map((item) => {
        const target = item.task_title ?? item.agent_name ?? item.task_id ?? item.agent_id;
        const color = item.kind === "task" ? taskStateColor(item.to_state ?? "") : agentStatusColor(item.status ?? "");
        return (
          <li key={item.sequence}>
            <time className="num" dateTime={item.created_at}>{clockTime(item.created_at)}</time>
            <span className="news-mark" style={{ background: color }} aria-hidden="true" />
            <span className="news-copy">
              <span className="micro">{item.kind} · {item.event_type.replace(/^(task|agent)\./, "")}</span>
              <span>
                {item.kind === "task" && item.task_id ? (
                  <button className="rowlink" type="button" onClick={() => onOpenTask(item.task_id!)}>
                    {target ?? shortId(item.task_id)}
                  </button>
                ) : <strong>{target ?? "unknown agent"}</strong>}
                {item.kind === "task" && item.event_type === "task.transitioned" ? (
                  <> moved <code>{item.from_state ?? "?"}</code> → <code>{item.to_state ?? "?"}</code></>
                ) : item.kind === "agent" && item.previous_status !== item.status ? (
                  <> moved <code>{item.previous_status ?? "?"}</code> → <code>{item.status ?? "?"}</code></>
                ) : <span className="news-detail"> · {item.actor}</span>}
              </span>
              <span className="news-meta num">{item.actor}{item.project ? ` · ${item.project}` : ""}</span>
              {item.failure_class ? (
                <span className="news-alert num">
                  {item.failure_class}{item.attempt_refunded ? " · attempt refunded" : ""}
                </span>
              ) : null}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
