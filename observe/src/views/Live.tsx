import { useEffect, useRef } from "react";
import type { Snapshot } from "../lib/api";
import { FlowChart } from "../components/FlowChart";
import { Bars, Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { clockTime, count, duration, shortId } from "../lib/format";
import {
  TERMINAL_TASK_STATES,
  agentStatusColor,
  orderStates,
  taskStateColor,
} from "../lib/states";

function reason(snap: Snapshot, section: string): string | undefined {
  return snap.degraded.find((d) => d.section === section)?.reason;
}

/**
 * The default view: what is moving right now.
 *
 * Answers, top to bottom: is work landing or piling up (tiles), what shape is
 * the flow over the window (chart), and which individual tasks moved in the
 * last few minutes (ticker).
 */
export function LiveView({
  snap,
  onOpenTask,
}: {
  snap: Snapshot;
  onOpenTask: (id: string) => void;
}) {
  const tasks = snap.tasks;
  const flow = snap.flow;
  const agents = snap.agents;

  const live = tasks
    ? Object.entries(tasks.by_state).filter(
        ([state]) => !TERMINAL_TASK_STATES.includes(state),
      )
    : [];
  const inFlight = live
    .filter(([state]) => state === "running" || state === "claimed")
    .reduce((a, [, n]) => a + n, 0);
  const landed = flow?.series["completed"]?.reduce((a, b) => a + b, 0) ?? null;
  const failed =
    (flow?.series["failed"]?.reduce((a, b) => a + b, 0) ?? 0) +
    (flow?.series["cancelled"]?.reduce((a, b) => a + b, 0) ?? 0);
  const onlineAgents = agents
    ? agents.rows.filter((row) => row.status !== "offline").length
    : null;
  const doubted = agents ? agents.rows.filter((r) => r.belief_contradicted).length : 0;

  return (
    <>
      <div className="tiles">
        <Tile
          label="executing now"
          value={tasks ? inFlight : null}
          accent="var(--stage-3)"
          note={tasks ? `${count(tasks.live_total)} not finished` : undefined}
        />
        <Tile
          label={`landed · ${snap.window.hours}h`}
          value={landed}
          accent="var(--status-good)"
          note="transitions into completed"
        />
        <Tile
          label={`failed + cancelled · ${snap.window.hours}h`}
          value={flow ? failed : null}
          accent="var(--status-critical)"
          tone={failed > (landed ?? 0) ? "bad" : undefined}
          note={
            flow && landed !== null && landed + failed > 0
              ? `${Math.round((landed / (landed + failed)) * 100)}% landed`
              : undefined
          }
        />
        <Tile
          label="agents responding"
          value={onlineAgents}
          accent="var(--series-3)"
          tone={doubted > 0 ? "warn" : undefined}
          note={
            agents
              ? doubted > 0
                ? `${doubted} report a status we cannot believe`
                : `of ${count(agents.total)}`
              : undefined
          }
        />
        <Tile
          label={`transitions · ${snap.window.hours}h`}
          value={flow ? flow.total : null}
          accent="var(--series-1)"
          note={flow ? `${count(snap.transitions?.length ?? 0)} shown below` : undefined}
        />
      </div>

      <div className="grid">
        <Panel
          title="Task movement"
          wide
          accent="var(--series-1)"
          sub={
            flow
              ? `${Math.round(flow.bucket_seconds)}s buckets · into-state`
              : undefined
          }
        >
          {flow ? (
            <FlowChart flow={flow} />
          ) : (
            <Unavailable what="Flow" reason={reason(snap, "flow")} />
          )}
        </Panel>

        <Panel
          title="In flight, by state"
          accent="var(--stage-3)"
          sub={tasks ? `${count(tasks.live_total)} tasks` : undefined}
        >
          {tasks ? (
            <Bars
              data={orderStates(live.map(([s]) => s)).map((state) => ({
                key: state,
                value: tasks.by_state[state] ?? 0,
                color: taskStateColor(state),
                title: `${state}: p50 dwell ${duration(
                  tasks.dwell_seconds[state]?.p50,
                )}`,
              }))}
            />
          ) : (
            <Unavailable what="Task states" reason={reason(snap, "tasks")} />
          )}
        </Panel>

        <Panel
          title="Transition ticker"
          accent="var(--series-2)"
          sub={snap.transitions ? `newest first` : undefined}
        >
          {snap.transitions ? (
            <Ticker rows={snap.transitions} onOpenTask={onOpenTask} />
          ) : (
            <Unavailable what="Transitions" reason={reason(snap, "transitions")} />
          )}
        </Panel>

        <Panel
          title="Agents"
          accent="var(--series-3)"
          sub={agents ? `${count(agents.total)} live` : undefined}
        >
          {agents ? (
            <Bars
              data={Object.entries(agents.by_status)
                .sort()
                .map(([status, n]) => ({
                  key: status,
                  value: n,
                  color: agentStatusColor(status),
                }))}
            />
          ) : (
            <Unavailable what="Agents" reason={reason(snap, "agents")} />
          )}
        </Panel>
      </div>
    </>
  );
}

/**
 * The most recent state changes, newest first. Rows that are new since the
 * last render flash once — the only animation in the app that is not a
 * liveness indicator, and it encodes an actual event.
 */
function Ticker({
  rows,
  onOpenTask,
}: {
  rows: NonNullable<Snapshot["transitions"]>;
  onOpenTask: (id: string) => void;
}) {
  const seen = useRef<Set<string> | null>(null);
  const fresh = new Set<string>();
  const keyOf = (r: (typeof rows)[number]) =>
    `${r.task_id}@${r.created_at}->${r.to_state}`;
  if (seen.current) {
    for (const row of rows) {
      const key = keyOf(row);
      if (!seen.current.has(key)) fresh.add(key);
    }
  }
  useEffect(() => {
    seen.current = new Set(rows.map(keyOf));
  });

  if (!rows.length) return <Empty>No transitions in this window.</Empty>;

  return (
    <div style={{ maxHeight: 260, overflowY: "auto" }}>
      <table className="data">
        <thead>
          <tr>
            <th>at</th>
            <th>task</th>
            <th>from → to</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 40).map((row) => {
            const key = keyOf(row);
            return (
              <tr key={key} className={fresh.has(key) ? "just-moved" : undefined}>
                <td className="n" style={{ color: "var(--ink-muted)" }}>
                  {clockTime(row.created_at)}
                </td>
                <td className="truncate">
                  <button
                    type="button"
                    className="rowlink"
                    title={`${row.task_id} — open drill-down`}
                    onClick={() => onOpenTask(row.task_id)}
                  >
                    {row.title ?? shortId(row.task_id)}
                  </button>
                </td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <span className="id">{row.from_state ?? "?"}</span>
                  <span style={{ color: "var(--ink-muted)" }}> → </span>
                  <span className="chip">
                    <span
                      className="swatch"
                      style={{ background: taskStateColor(row.to_state ?? "") }}
                    />
                    {row.to_state ?? "?"}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
