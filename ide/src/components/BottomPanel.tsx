import { useState } from "react";
import type { DashboardState, TaskDetail } from "../api/mac";

type BottomTab = "events" | "bus" | "evidence" | "problems";

function value(record: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const candidate = record[key];
    if (candidate !== null && candidate !== undefined && candidate !== "") {
      return typeof candidate === "object" ? JSON.stringify(candidate) : String(candidate);
    }
  }
  return "—";
}

export function BottomPanel({ data, detail }: { data: DashboardState; detail: TaskDetail | null }) {
  const [tab, setTab] = useState<BottomTab>("events");
  const evidence = detail?.evidence || [];
  const history = detail?.history || [];
  const findings = data.integration_findings.filter((record) => record.status === "open");
  const tabs: Array<{ id: BottomTab; label: string; count?: number }> = [
    { id: "events", label: "Event stream", count: data.events.length },
    { id: "bus", label: "Bus", count: data.bus_streams.length },
    { id: "evidence", label: "Evidence", count: evidence.length },
    { id: "problems", label: "Problems", count: findings.length },
  ];

  return (
    <section className="bottom-panel">
      <div className="bottom-panel-tabs" role="tablist">
        {tabs.map((item) => (
          <button
            aria-selected={tab === item.id}
            className={tab === item.id ? "active" : ""}
            key={item.id}
            onClick={() => setTab(item.id)}
            role="tab"
            type="button"
          >
            {item.label}{item.count ? <span>{item.count}</span> : null}
          </button>
        ))}
        <span className="panel-spacer" />
        <button title="Pause stream" type="button"><i className="codicon codicon-debug-pause" /></button>
        <button title="Filter" type="button"><i className="codicon codicon-filter" /></button>
      </div>
      <div className="bottom-panel-body">
        {tab === "events" ? <Events data={data} history={history} /> : null}
        {tab === "bus" ? <BusStreams records={data.bus_streams} /> : null}
        {tab === "evidence" ? <Evidence records={evidence} /> : null}
        {tab === "problems" ? <Problems records={findings} /> : null}
      </div>
    </section>
  );
}

function Events({ data, history }: { data: DashboardState; history: Array<Record<string, unknown>> }) {
  const records = [...data.events, ...history].slice(-80).reverse();
  if (!records.length) return <Empty label="No control-plane events have been recorded." />;
  return (
    <div className="console-lines">
      {records.map((record, index) => {
        const timestamp = value(record, "created_at", "timestamp");
        const name = value(record, "event_type", "name", "kind", "to_state");
        const actor = value(record, "actor", "subject_id", "from_state");
        const detail = value(record, "reason", "detail", "summary");
        return (
          <div className="console-line" key={value(record, "id", "sequence") + index}>
            <time>{timestamp === "—" ? "--:--:--" : timestamp.slice(11, 23)}</time>
            <span className="console-level">INFO</span>
            <span className="console-name">[{actor}]</span>
            <span>{name}</span>
            <span className="console-detail">{detail}</span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * The conversations the fleet is having, on AgentBus.
 *
 * This tab was Terminal: PTY sessions over an HTTP facade that reached past
 * the bus into a machine. That facade is gone (#417) because it was the last
 * place this UI could COMMAND rather than observe — the PTY itself survives as
 * a capability an operator asks a NAMED agent for over the bus. Rebuilding the
 * route here would undo that while looking like progress, so the tab shows the
 * bus instead: who is talking to whom, on what topic.
 *
 * Sender and recipient are addressing, not access. A stream with no recipient
 * was said to the fleet.
 */
function BusStreams({ records }: { records: Array<Record<string, unknown>> }) {
  if (!records.length) return <Empty label="The bus is quiet — no AgentBus streams are open." />;
  return (
    <div className="record-list bottom-records">
      {records.map((record, index) => {
        const recipient = value(record, "recipient_agent_id");
        return (
          <div className="record-item" key={value(record, "id", "stream_id") + index}>
            <i className="codicon codicon-comment-discussion" />
            <span className="record-title">
              <strong>
                {value(record, "sender_agent_id", "agent_id")}
                {recipient === "—" ? " → the fleet" : ` → ${recipient}`}
              </strong>
              <small>{value(record, "topic")}</small>
            </span>
            <span className="record-state">{value(record, "status", "state")}</span>
          </div>
        );
      })}
    </div>
  );
}

function Evidence({ records }: { records: Array<Record<string, unknown>> }) {
  if (!records.length) return <Empty label="Select a task with evidence to inspect its artifacts and verification." />;
  return (
    <div className="record-list bottom-records">
      {records.map((record, index) => (
        <div className="record-item evidence-item" key={value(record, "id") + index}>
          <i className="codicon codicon-verified-filled" />
          <span className="record-title"><strong>{value(record, "kind", "evidence_type")}</strong><small>{value(record, "summary")}</small></span>
          {value(record, "uri") !== "—" ? <a href={value(record, "uri")} rel="noreferrer" target="_blank">open artifact</a> : null}
          <span className="record-state">{value(record, "created_by")}</span>
        </div>
      ))}
    </div>
  );
}

function Problems({ records }: { records: Array<Record<string, unknown>> }) {
  if (!records.length) return <Empty label="No open integration findings." />;
  return (
    <div className="problem-list">
      {records.map((record, index) => (
        <div className="problem-item" key={value(record, "id", "fingerprint") + index}>
          <i className={`codicon codicon-${record.severity === "critical" ? "error" : "warning"}`} />
          <span className="record-title"><strong>{value(record, "title")}</strong><small>{value(record, "source_kind", "source_id")}</small></span>
          <span>{value(record, "severity")}</span>
        </div>
      ))}
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return <div className="empty-state centered"><span>{label}</span></div>;
}
