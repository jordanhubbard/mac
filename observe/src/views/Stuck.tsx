import type { Snapshot } from "../lib/api";
import { Bars, Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { UNKNOWN, count, duration, shortId } from "../lib/format";
import { TERMINAL_TASK_STATES, orderStates, taskStateColor } from "../lib/states";

function reason(snap: Snapshot, section: string): string | undefined {
  return snap.degraded.find((d) => d.section === section)?.reason;
}

/** Dwell over an hour in a non-executing state is worth a second look. */
const DWELL_WARN_SECONDS = 3600;
const DWELL_BAD_SECONDS = 86400;

/**
 * Where work is stuck, and for how long.
 *
 * The count answers "how many"; the dwell percentiles answer "for how long",
 * which is the question a count cannot. A state whose p50 dwell is measured in
 * days is not a queue, it is a graveyard.
 */
export function StuckView({
  snap,
  onOpenTask,
}: {
  snap: Snapshot;
  onOpenTask?: (id: string) => void;
}) {
  const tasks = snap.tasks;
  const stuck = snap.stuck;

  const liveStates = tasks
    ? orderStates(
        Object.keys(tasks.by_state).filter((s) => !TERMINAL_TASK_STATES.includes(s)),
      )
    : [];
  const worst = tasks
    ? liveStates
        .map((state) => ({ state, dwell: tasks.dwell_seconds[state] }))
        .filter((row) => row.dwell?.p50 !== null && row.dwell?.p50 !== undefined)
        .sort((a, b) => (b.dwell!.p50 ?? 0) - (a.dwell!.p50 ?? 0))[0]
    : undefined;

  const terminal = tasks
    ? TERMINAL_TASK_STATES.map((s) => [s, tasks.by_state[s] ?? 0] as const)
    : [];
  const completed = tasks ? (tasks.by_state["completed"] ?? 0) : null;
  const notCompleted = terminal
    .filter(([s]) => s !== "completed")
    .reduce((a, [, n]) => a + n, 0);

  return (
    <>
      <div className="tiles">
        <Tile
          label="not finished"
          value={tasks ? tasks.live_total : null}
          accent="var(--status-serious)"
        />
        <Tile
          label="blocked"
          value={tasks ? (tasks.by_state["blocked"] ?? 0) : null}
          accent="var(--status-serious)"
          tone={(tasks?.by_state["blocked"] ?? 0) > 0 ? "warn" : undefined}
        />
        <Tile
          label="awaiting a human"
          value={
            tasks
              ? (tasks.by_state["needs_input"] ?? 0) + (tasks.by_state["waiting"] ?? 0)
              : null
          }
          accent="var(--status-warning)"
        />
        <Tile
          label="longest-dwelling state"
          value={worst ? `${worst.state} ${duration(worst.dwell!.p50)}` : null}
          accent="var(--status-critical)"
          note={worst ? `p50 of ${count(worst.dwell!.count)} tasks` : undefined}
        />
        <Tile
          label="lifetime completed"
          value={completed}
          accent="var(--status-good)"
          note={
            tasks && completed !== null
              ? `against ${count(notCompleted)} failed + cancelled`
              : undefined
          }
        />
      </div>

      <div className="grid">
        <Panel
          title="Dwell in current state"
          wide
          accent="var(--status-serious)"
          sub="p50 / p90 / max, non-terminal only"
        >
          {tasks ? (
            <table className="data">
              <thead>
                <tr>
                  <th>state</th>
                  <th style={{ textAlign: "right" }}>tasks</th>
                  <th style={{ textAlign: "right" }}>p50</th>
                  <th style={{ textAlign: "right" }}>p90</th>
                  <th style={{ textAlign: "right" }}>max</th>
                </tr>
              </thead>
              <tbody>
                {liveStates.map((state) => {
                  const dwell = tasks.dwell_seconds[state];
                  const p50 = dwell?.p50 ?? null;
                  const tone =
                    p50 === null
                      ? undefined
                      : p50 >= DWELL_BAD_SECONDS
                        ? "var(--status-critical)"
                        : p50 >= DWELL_WARN_SECONDS
                          ? "var(--status-warning)"
                          : undefined;
                  return (
                    <tr key={state}>
                      <td>
                        <span className="chip">
                          <span
                            className="swatch"
                            style={{ background: taskStateColor(state) }}
                          />
                          {state}
                        </span>
                      </td>
                      <td className="n">{count(tasks.by_state[state] ?? 0)}</td>
                      <td className="n" style={tone ? { color: tone } : undefined}>
                        {duration(p50)}
                      </td>
                      <td className="n">{duration(dwell?.p90)}</td>
                      <td className="n">{duration(dwell?.max)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <Unavailable what="Dwell" reason={reason(snap, "tasks")} />
          )}
          {tasks && tasks.undated_rows > 0 ? (
            <p className="unknown-text" style={{ fontSize: 11 }}>
              {count(tasks.undated_rows)} task
              {tasks.undated_rows === 1 ? " has" : "s have"} no readable
              updated_at and are excluded from dwell.
            </p>
          ) : null}
        </Panel>

        <Panel title="Lifetime outcomes" accent="var(--status-good)">
          {tasks ? (
            <Bars
              data={terminal.map(([state, n]) => ({
                key: state,
                value: n,
                color: taskStateColor(state),
              }))}
            />
          ) : (
            <Unavailable what="Outcomes" reason={reason(snap, "tasks")} />
          )}
        </Panel>

        <Panel
          title="Longest-stuck tasks"
          accent="var(--status-critical)"
          sub={stuck ? `oldest ${stuck.length}` : undefined}
        >
          {stuck ? (
            stuck.length === 0 ? (
              <Empty>Nothing is in flight.</Empty>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th style={{ textAlign: "right" }}>dwell</th>
                    <th>state</th>
                    <th>task</th>
                    <th>project</th>
                  </tr>
                </thead>
                <tbody>
                  {stuck.map((row) => (
                    <tr key={row.id}>
                      <td className="n">{duration(row.dwell_seconds)}</td>
                      <td>
                        <span className="chip">
                          <span
                            className="swatch"
                            style={{ background: taskStateColor(row.state) }}
                          />
                          {row.state}
                        </span>
                      </td>
                      <td className="truncate">
                        {onOpenTask ? (
                          <button
                            type="button"
                            className="rowlink"
                            title={`${row.id} — open drill-down`}
                            onClick={() => onOpenTask(row.id)}
                          >
                            {row.title || shortId(row.id)}
                          </button>
                        ) : (
                          <span title={row.id}>{row.title || shortId(row.id)}</span>
                        )}
                      </td>
                      <td className="truncate id">{row.project ?? UNKNOWN}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : (
            <Unavailable what="Stuck work" reason={reason(snap, "stuck")} />
          )}
        </Panel>
      </div>
    </>
  );
}
