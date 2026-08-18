import { useState } from "react";
import type {
  ConsoleClient,
  Snapshot,
  TaskDrilldown,
  TranscriptTurn,
} from "../lib/api";
import { useTask, useTranscript } from "../lib/useTask";
import { Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { UNKNOWN, bytes, clockTime, count, duration, shortId } from "../lib/format";
import { healthColor, taskStateColor } from "../lib/states";

function reason(detail: TaskDrilldown, section: string): string | undefined {
  return detail.degraded.find((d) => d.section === section)?.reason;
}

/**
 * One task, in full: how it moved, what the coding agent said while it moved,
 * and what the harness ran.
 *
 * The hard part of this screen is not showing the data — it is being honest
 * about how little of it there is. Roughly 2% of tasks on this fleet have any
 * transcript at all, so a blank transcript panel is overwhelmingly likely to
 * mean "we did not record it" rather than "nothing happened". Every empty state
 * below therefore says WHICH of those two it is.
 */
export function TaskView({
  client,
  taskId,
  snap,
  onBack,
}: {
  client: ConsoleClient;
  taskId: string | null;
  snap: Snapshot;
  onBack: () => void;
}) {
  const { detail, error, loading, reload } = useTask(client, taskId);
  const [openTurn, setOpenTurn] = useState<string | null>(null);

  if (!taskId) {
    return (
      <Empty>
        Pick a task from <strong>Live</strong> or <strong>Stuck work</strong> to see
        its history, transcript and commands.
      </Empty>
    );
  }

  if (error) {
    return (
      <div className="banner critical">
        <span className="icon" aria-hidden="true">
          ▲
        </span>
        <span>
          <strong>Could not read task {shortId(taskId)}.</strong> {error}
          <div>
            <button className="ghost" type="button" onClick={reload}>
              retry
            </button>
          </div>
        </span>
      </div>
    );
  }

  if (!detail) return <p className="empty">Reading task…</p>;

  if (!detail.found) {
    return (
      <div className="banner serious">
        <span className="icon" aria-hidden="true">
          !
        </span>
        <span>
          <strong>No task with id {taskId}.</strong> The hub answered — this is
          "there is no such task", not "the hub is down" and not "this task did
          nothing".
          <div>
            <button className="ghost" type="button" onClick={onBack}>
              back to live
            </button>
          </div>
        </span>
      </div>
    );
  }

  const task = detail.task!;
  const transcripts = detail.transcripts;
  const coverage = snap.transcripts;

  return (
    <>
      <div className="task-head">
        <button className="ghost" type="button" onClick={onBack}>
          ← back
        </button>
        <span className="chip">
          <span
            className="swatch"
            style={{ background: taskStateColor(task.state) }}
          />
          {task.state}
        </span>
        <h1>{task.title}</h1>
        <span className="id">{task.id}</span>
        {loading ? <span className="micro">reloading…</span> : null}
      </div>

      <div className="tiles">
        <Tile
          label="dwell in this state"
          value={duration(task.dwell_seconds)}
          accent={taskStateColor(task.state)}
        />
        <Tile label="age" value={duration(task.age_seconds)} accent="var(--series-1)" />
        <Tile
          label="attempts"
          value={`${count(task.attempt_count)}/${count(task.max_attempts)}`}
          accent="var(--series-4)"
          tone={task.attempt_count >= task.max_attempts ? "warn" : undefined}
        />
        <Tile
          label="project"
          value={task.project ?? null}
          accent="var(--series-2)"
          note={task.owner_agent_id ? `owner ${task.owner_agent_id}` : "unowned"}
        />
        <Tile
          label="transcript turns"
          value={transcripts ? transcripts.count : null}
          accent="var(--series-5)"
          note={
            transcripts && transcripts.count === 0 ? "nothing recorded" : undefined
          }
        />
      </div>

      <div className="grid">
        <Panel
          title="State history"
          accent="var(--series-1)"
          sub={
            detail.history
              ? `${count(detail.history.length)} event${
                  detail.history.length === 1 ? "" : "s"
                }`
              : undefined
          }
        >
          {detail.history ? (
            detail.history.length === 0 ? (
              <Empty>No history rows for this task.</Empty>
            ) : (
              <div style={{ maxHeight: 380, overflowY: "auto" }}>
                <table className="data">
                  <thead>
                    <tr>
                      <th>at</th>
                      <th>event</th>
                      <th>transition</th>
                      <th>actor</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.history.map((event) => (
                      <tr key={event.id}>
                        <td className="n" style={{ color: "var(--ink-muted)" }}>
                          {clockTime(event.created_at)}
                        </td>
                        <td className="id truncate" title={event.event_type}>
                          {event.event_type}
                        </td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          {event.to_state ? (
                            <>
                              <span className="id">{event.from_state ?? "?"}</span>
                              <span style={{ color: "var(--ink-muted)" }}> → </span>
                              <span className="chip">
                                <span
                                  className="swatch"
                                  style={{
                                    background: taskStateColor(event.to_state),
                                  }}
                                />
                                {event.to_state}
                              </span>
                            </>
                          ) : (
                            <span className="unknown-text">not a transition</span>
                          )}
                        </td>
                        <td className="id truncate">{event.actor ?? UNKNOWN}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : (
            <Unavailable what="History" reason={reason(detail, "history")} />
          )}
        </Panel>

        <Panel
          title="Agent transcript"
          wide
          accent="var(--series-5)"
          sub={
            transcripts
              ? `${count(transcripts.count)} turns · ${count(
                  transcripts.unattributed,
                )} unattributed`
              : undefined
          }
        >
          {transcripts ? (
            <>
              <CoverageNote coverage={coverage} count={transcripts.count} />
              {transcripts.count === 0 ? null : (
                <TurnList
                  client={client}
                  turns={transcripts.rows}
                  openTurn={openTurn}
                  onToggle={(id) => setOpenTurn((current) => (current === id ? null : id))}
                />
              )}
              {transcripts.truncated_list ? (
                <p className="unknown-text" style={{ fontSize: 11 }}>
                  Only the first {count(transcripts.rows.length)} turns are listed.
                </p>
              ) : null}
            </>
          ) : (
            <Unavailable what="Transcript" reason={reason(detail, "transcripts")} />
          )}
        </Panel>

        <Panel
          title="Harness commands"
          wide
          accent="var(--series-3)"
          sub={detail.commands ? `${count(detail.commands.length)} audited` : undefined}
        >
          {detail.commands ? (
            <>
              <div className="banner" style={{ marginBottom: 10 }}>
                <span className="icon" aria-hidden="true">
                  !
                </span>
                <span>
                  <strong>This is not everything that ran.</strong>{" "}
                  <code>command_audit</code> records the commands the MAC harness
                  itself spawned. Whatever the coding CLI executed inside its
                  sandbox is not captured here, so an empty or short list does not
                  mean the agent was idle.
                </span>
              </div>
              {detail.commands.length === 0 ? (
                <Empty>
                  The harness recorded no command spawns for this task. Given the
                  caveat above, read this as "nothing the harness itself ran",
                  not "nothing ran".
                </Empty>
              ) : (
                <table className="data">
                  <thead>
                    <tr>
                      <th>at</th>
                      <th>phase</th>
                      <th>argv</th>
                      <th style={{ textAlign: "right" }}>rc</th>
                      <th style={{ textAlign: "right" }}>took</th>
                      <th style={{ textAlign: "right" }}>out/err</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.commands.map((command) => (
                      <tr key={command.id}>
                        <td className="n" style={{ color: "var(--ink-muted)" }}>
                          {clockTime(command.created_at)}
                        </td>
                        <td className="id">{command.phase}</td>
                        <td className="id truncate" title={command.argv}>
                          {command.argv}
                        </td>
                        <td
                          className="n"
                          style={
                            command.returncode
                              ? { color: "var(--status-critical)" }
                              : undefined
                          }
                        >
                          {command.returncode === null ? UNKNOWN : command.returncode}
                        </td>
                        <td className="n">
                          {command.duration_ms === null
                            ? UNKNOWN
                            : `${Math.round(command.duration_ms)}ms`}
                        </td>
                        <td className="n" style={{ color: "var(--ink-muted)" }}>
                          {bytes(command.stdout_bytes)}/{bytes(command.stderr_bytes)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          ) : (
            <Unavailable what="Commands" reason={reason(detail, "commands")} />
          )}
        </Panel>

        <Panel title="Evidence" accent="var(--series-4)">
          {detail.evidence ? (
            detail.evidence.length === 0 ? (
              <Empty>No evidence was attached to this task.</Empty>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>kind</th>
                    <th>summary</th>
                    <th>by</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.evidence.map((row) => (
                    <tr key={row.id}>
                      <td className="id">{row.kind}</td>
                      <td className="truncate" title={`${row.summary} — ${row.uri}`}>
                        {row.summary || row.uri}
                      </td>
                      <td className="id truncate">{row.created_by}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : (
            <Unavailable what="Evidence" reason={reason(detail, "evidence")} />
          )}
        </Panel>

        <Panel title="Reviews & publications" accent="var(--series-2)">
          {detail.reviews && detail.publications ? (
            detail.reviews.length === 0 && detail.publications.length === 0 ? (
              <Empty>This task was never reviewed or published.</Empty>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>kind</th>
                    <th>status</th>
                    <th>at</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.reviews.map((row) => (
                    <tr key={row.id}>
                      <td className="id">review</td>
                      <td>
                        <span className="chip">
                          <span
                            className="swatch"
                            style={{ background: healthColor(row.status) }}
                          />
                          {row.status}
                        </span>
                      </td>
                      <td className="n">{clockTime(row.created_at)}</td>
                    </tr>
                  ))}
                  {detail.publications.map((row) => (
                    <tr key={row.id}>
                      <td className="id">publication</td>
                      <td>
                        <span className="chip">
                          <span
                            className="swatch"
                            style={{ background: healthColor(row.status) }}
                          />
                          {row.status}
                        </span>
                      </td>
                      <td className="n">{clockTime(row.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : (
            <Unavailable
              what="Reviews"
              reason={reason(detail, "reviews") ?? reason(detail, "publications")}
            />
          )}
        </Panel>
      </div>
    </>
  );
}

/**
 * The most important paragraph on the screen.
 *
 * "No transcript" and "an empty transcript" are different facts and this fleet
 * produces the first one 98% of the time. Rendering both as a blank panel would
 * let an operator conclude an agent did nothing when in truth nobody wrote it
 * down.
 */
function CoverageNote({
  coverage,
  count: turns,
}: {
  coverage: Snapshot["transcripts"];
  count: number;
}) {
  const pct =
    coverage && coverage.coverage_fraction !== null
      ? `${(coverage.coverage_fraction * 100).toFixed(1)}%`
      : null;

  if (turns === 0) {
    return (
      <div className="banner serious">
        <span className="icon" aria-hidden="true">
          !
        </span>
        <span>
          <strong>No transcript was recorded for this task.</strong> That is a gap
          in recording, not evidence that the agent did nothing — the two are
          indistinguishable from here.
          {pct ? (
            <div className="unknown-text">
              Fleet-wide, {pct} of tasks ({count(coverage!.tasks_with_transcript)} of{" "}
              {count(coverage!.tasks_total)}) have any transcript at all.
            </div>
          ) : (
            <div className="unknown-text">
              Fleet-wide transcript coverage is unknown — the console could not read
              it.
            </div>
          )}
        </span>
      </div>
    );
  }

  if (!pct) return null;
  return (
    <p className="unknown-text" style={{ fontSize: 11, marginTop: 0 }}>
      Fleet-wide, only {pct} of tasks have any transcript; this one is among them.
    </p>
  );
}

function TurnList({
  client,
  turns,
  openTurn,
  onToggle,
}: {
  client: ConsoleClient;
  turns: TranscriptTurn[];
  openTurn: string | null;
  onToggle: (id: string) => void;
}) {
  return (
    <div className="turns">
      {turns.map((turn) => (
        <Turn
          key={turn.id}
          client={client}
          turn={turn}
          open={openTurn === turn.id}
          onToggle={() => onToggle(turn.id)}
        />
      ))}
    </div>
  );
}

function Turn({
  client,
  turn,
  open,
  onToggle,
}: {
  client: ConsoleClient;
  turn: TranscriptTurn;
  open: boolean;
  onToggle: () => void;
}) {
  const { entry, error, loading } = useTranscript(client, open ? turn.id : null);
  return (
    <div className="turn">
      <button
        type="button"
        className="turn-head"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="num turn-seq">#{turn.sequence}</span>
        {/* An absent CLI name is stated, not left blank. These columns were
            empty on every historical row (an executor bug fixed later), so a
            blank cell here would read as "no CLI ran". */}
        {turn.coding_agent ? (
          <span className="chip">
            <span className="swatch" style={{ background: "var(--series-5)" }} />
            {turn.coding_agent}
            {turn.model ? <span className="unknown-text"> · {turn.model}</span> : null}
          </span>
        ) : (
          <span className="chip" title="coding_agent and model were not recorded">
            <span className="swatch" style={{ background: "var(--ink-muted)" }} />
            <span className="unknown-text">unattributed</span>
          </span>
        )}
        <span
          className="num"
          style={
            turn.returncode ? { color: "var(--status-critical)" } : undefined
          }
        >
          rc {turn.returncode === null ? UNKNOWN : turn.returncode}
        </span>
        <span className="num" style={{ color: "var(--ink-muted)" }}>
          {turn.duration_ms === null
            ? UNKNOWN
            : `${Math.round(turn.duration_ms)}ms`}
        </span>
        <span className="num" style={{ color: "var(--ink-muted)" }}>
          {turn.has_payload ? bytes(turn.payload_bytes) : "empty"}
        </span>
        {turn.truncated ? (
          <span className="unknown-text">truncated at capture</span>
        ) : null}
        <span className="turn-caret">{open ? "▾" : "▸"}</span>
      </button>

      {open ? (
        <div className="turn-body">
          {loading ? <p className="empty">Reading turn…</p> : null}
          {error ? (
            <div className="banner critical">
              <span className="icon" aria-hidden="true">
                ▲
              </span>
              <span>Could not read this turn: {error}</span>
            </div>
          ) : null}
          {entry && !entry.found ? (
            <Empty>The hub no longer has this turn.</Empty>
          ) : null}
          {entry && entry.found ? (
            <>
              {!turn.has_payload ? (
                <div className="banner">
                  <span className="icon" aria-hidden="true">
                    !
                  </span>
                  <span>
                    <strong>This turn was recorded with an empty payload.</strong>{" "}
                    The row exists, so something was written — but there is no
                    prompt or response text in it. That is different from the task
                    having no transcript at all.
                  </span>
                </div>
              ) : null}
              <TurnText label="prompt" value={entry.prompt} />
              <TurnText label="response" value={entry.response} />
              <TurnText label="stderr" value={entry.stderr} tone="critical" />
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function TurnText({
  label,
  value,
  tone,
}: {
  label: string;
  value: { text: string; clipped: boolean; full_length: number } | undefined;
  tone?: "critical";
}) {
  if (!value) return null;
  if (!value.text) {
    return (
      <div className="turn-text">
        <span className="micro">{label}</span>
        <p className="unknown-text" style={{ fontSize: 11, margin: "2px 0 0" }}>
          empty
        </p>
      </div>
    );
  }
  return (
    <div className="turn-text">
      <span className="micro">
        {label} · {count(value.full_length)} chars
        {value.clipped ? " · clipped for display" : ""}
      </span>
      <pre
        className="turn-pre"
        style={tone === "critical" ? { color: "var(--status-serious)" } : undefined}
      >
        {value.text}
      </pre>
      {value.clipped ? (
        <p className="unknown-text" style={{ fontSize: 11, margin: "3px 0 0" }}>
          Showing the first {count(value.text.length)} of{" "}
          {count(value.full_length)} characters. This is a prefix, not the whole
          turn.
        </p>
      ) : null}
    </div>
  );
}
