import { useState } from "react";
import { type TaskDetail } from "../api/mac";

type Tab = "activity" | "evidence" | "history";

export function BottomPanel({ detail }: { detail: TaskDetail | null }) {
  const [tab, setTab] = useState<Tab>("activity");
  const t = detail?.task;
  const activity = (t?.metadata?.activity as any[]) || [];
  const evidence = detail?.evidence || [];
  const history = detail?.history || [];

  return (
    <div className="panel" style={{ display: "flex", flexDirection: "column" }}>
      <div className="bottom-tabs">
        {(["activity", "evidence", "history"] as Tab[]).map((x) => (
          <div
            key={x}
            className={"tab" + (tab === x ? " active" : "")}
            onClick={() => setTab(x)}
          >
            {x[0].toUpperCase() + x.slice(1)}
          </div>
        ))}
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {!detail && <div className="muted" style={{ padding: 12 }}>Select a task.</div>}

        {detail && tab === "activity" &&
          (activity.length ? (
            activity.map((e, i) => (
              <div className="activity-entry" key={i}>
                <div className="head">
                  {e.phase} / {e.actor} · {String(e.at || "").slice(0, 19)}
                </div>
                <pre>{e.summary}</pre>
              </div>
            ))
          ) : (
            <div className="muted" style={{ padding: 12 }}>No activity recorded yet.</div>
          ))}

        {detail && tab === "evidence" &&
          (evidence.length ? (
            evidence.map((ev: any, i: number) => (
              <div className="activity-entry" key={i}>
                <div className="head">{ev.kind} / {ev.created_by}</div>
                <pre>{ev.summary}</pre>
              </div>
            ))
          ) : (
            <div className="muted" style={{ padding: 12 }}>No evidence.</div>
          ))}

        {detail && tab === "history" &&
          history.map((h: any, i: number) => (
            <div className="activity-entry" key={i}>
              <div className="head">
                {h.from_state} → {h.to_state} ·{" "}
                {String(h.timestamp || h.created_at || "").slice(0, 19)}
                {h.reason ? ` · ${h.reason}` : ""}
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}
