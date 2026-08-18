import type { Snapshot } from "../lib/api";
import { Bars, Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { UNKNOWN, bytes, count, duration, ranked, sum } from "../lib/format";
import { categoricalColor, healthColor } from "../lib/states";

function reason(snap: Snapshot, section: string): string | undefined {
  return snap.degraded.find((d) => d.section === section)?.reason;
}

function StatusPanel({
  title,
  accent,
  data,
  emptyNote,
}: {
  title: string;
  accent: string;
  data: Record<string, number>;
  emptyNote: string;
}) {
  const rows = ranked(data);
  const universe = rows.map(([key]) => key);
  return (
    <Panel title={title} accent={accent} sub={`${count(sum(data))} total`}>
      {rows.length === 0 ? (
        <Empty>{emptyNote}</Empty>
      ) : (
        <Bars
          data={rows.map(([status, n]) => ({
            key: status === "(none)" ? UNKNOWN : status,
            value: n,
            color:
              healthColor(status) === "#898781"
                ? categoricalColor(status, universe)
                : healthColor(status),
          }))}
        />
      )}
    </Panel>
  );
}

/** Reviews, publications, work packages and leases — the delivery pipelines. */
export function PipelinesView({ snap }: { snap: Snapshot }) {
  const p = snap.pipelines;
  if (!p) return <Unavailable what="Pipelines" reason={reason(snap, "pipelines")} />;
  return (
    <>
      <div className="tiles">
        <Tile
          label="reviews pending"
          value={p.reviews["pending"] ?? 0}
          accent="var(--status-warning)"
        />
        <Tile
          label="publications pending"
          value={p.publications["pending"] ?? 0}
          accent="var(--status-warning)"
        />
        <Tile
          label="leases active"
          value={p.leases["active"] ?? 0}
          accent="var(--series-3)"
          note={`${count(p.leases["expired"] ?? 0)} expired on record`}
        />
      </div>
      <div className="grid">
        <StatusPanel
          title="Reviews"
          accent="var(--series-1)"
          data={p.reviews}
          emptyNote="No review has ever been opened."
        />
        <StatusPanel
          title="Publications"
          accent="var(--series-2)"
          data={p.publications}
          emptyNote="Nothing has ever been published."
        />
        <StatusPanel
          title="Leases"
          accent="var(--series-5)"
          data={p.leases}
          emptyNote="No lease has ever been taken."
        />
      </div>
    </>
  );
}

/** Dreaming and nap cycles — the fleet's reflection loop. */
export function CyclesView({ snap }: { snap: Snapshot }) {
  const cycles = snap.cycles;
  const dreams = snap.dreams;
  return (
    <>
      <div className="tiles">
        <Tile
          label="nap schedules enabled"
          value={cycles ? cycles.schedules_enabled : null}
          accent="var(--series-6)"
          note={cycles ? `of ${count(cycles.schedules_total)}` : undefined}
        />
        <Tile
          label="naps running"
          value={cycles ? (cycles.naps_by_status["running"] ?? 0) : null}
          accent="var(--series-1)"
        />
        <Tile
          label="naps failed"
          value={cycles ? (cycles.naps_by_status["failed"] ?? 0) : null}
          accent="var(--status-critical)"
        />
        <Tile
          label="dream runs"
          value={dreams ? sum(dreams.by_status) : null}
          accent="var(--series-5)"
          note={
            dreams
              ? `${count(dreams.by_state["promoted"] ?? 0)} promoted to memory`
              : "table absent"
          }
        />
      </div>
      <div className="grid">
        <Panel title="Nap runs" accent="var(--series-6)">
          {cycles ? (
            sum(cycles.naps_by_status) === 0 ? (
              <Empty>
                No nap has ever run on this hub. If naps are scheduled, that gap is
                itself the finding.
              </Empty>
            ) : (
              <Bars
                data={ranked(cycles.naps_by_status).map(([status, n]) => ({
                  key: status,
                  value: n,
                  color: healthColor(status),
                }))}
              />
            )
          ) : (
            <Unavailable what="Nap cycles" reason={reason(snap, "cycles")} />
          )}
        </Panel>

        <Panel
          title="Recent naps"
          accent="var(--series-6)"
          sub={cycles ? "newest first" : undefined}
        >
          {cycles ? (
            cycles.recent_naps.length === 0 ? (
              <Empty>No nap runs recorded.</Empty>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>agent</th>
                    <th>status</th>
                    <th style={{ textAlign: "right" }}>started</th>
                  </tr>
                </thead>
                <tbody>
                  {cycles.recent_naps.map((row) => (
                    <tr key={row.id}>
                      <td className="truncate id">{row.agent_id ?? UNKNOWN}</td>
                      <td>
                        <span className="chip">
                          <span
                            className="swatch"
                            style={{ background: healthColor(row.status) }}
                          />
                          {row.status}
                        </span>
                      </td>
                      <td className="n">{duration(row.age_seconds)} ago</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : (
            <Unavailable what="Nap history" reason={reason(snap, "cycles")} />
          )}
        </Panel>

        <Panel title="Dream runs" wide accent="var(--series-5)">
          {dreams ? (
            dreams.recent.length === 0 ? (
              <Empty>No dream run recorded.</Empty>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>agent</th>
                    <th>project</th>
                    <th>status</th>
                    <th>disposition</th>
                    <th style={{ textAlign: "right" }}>age</th>
                  </tr>
                </thead>
                <tbody>
                  {dreams.recent.map((row) => (
                    <tr key={row.id}>
                      <td className="truncate id">{row.agent_id ?? UNKNOWN}</td>
                      <td className="truncate id">{row.project ?? UNKNOWN}</td>
                      <td>
                        <span className="chip">
                          <span
                            className="swatch"
                            style={{ background: healthColor(row.status) }}
                          />
                          {row.status}
                        </span>
                      </td>
                      <td className="id">{row.state}</td>
                      <td className="n">{duration(row.age_seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : (
            <Unavailable
              what="Dreaming"
              reason={
                reason(snap, "dreams") ??
                "dream_runs is created lazily by the dreaming service, so its absence usually means dreaming has never run on this hub."
              }
            />
          )}
        </Panel>
      </div>
    </>
  );
}

/** Telemetry health and AgentBus traffic — is the observability itself alive? */
export function TelemetryView({ snap }: { snap: Snapshot }) {
  const t = snap.telemetry;
  const bus = snap.agentbus;
  const t2 = snap.transcripts;
  return (
    <>
      <TranscriptCoverageBanner snap={snap} />
      <div className="tiles">
        <Tile
          label="observability cursor"
          value={t ? t.cursor : null}
          accent="var(--series-1)"
        />
        <Tile
          label={`events · ${snap.window.hours}h`}
          value={t ? t.events_in_window : null}
          accent="var(--series-2)"
          note={t ? `${count(t.events_total)} retained` : undefined}
        />
        <Tile
          label="errors in window"
          value={
            t ? (t.by_level_in_window["error"] ?? 0) + (t.by_level_in_window["critical"] ?? 0) : null
          }
          accent="var(--status-critical)"
          tone={
            t && (t.by_level_in_window["error"] ?? 0) + (t.by_level_in_window["critical"] ?? 0) > 0
              ? "bad"
              : undefined
          }
        />
        <Tile
          label="retention span"
          value={t ? duration(t.retention_span_seconds) : null}
          accent="var(--series-4)"
          note="oldest event still on disk"
        />
        <Tile
          label="tasks with a transcript"
          value={
            t2 && t2.coverage_fraction !== null
              ? `${(t2.coverage_fraction * 100).toFixed(1)}%`
              : null
          }
          accent="var(--series-5)"
          tone={
            t2 && t2.coverage_fraction !== null && t2.coverage_fraction < 0.5
              ? "warn"
              : undefined
          }
          note={
            t2
              ? `${count(t2.tasks_with_transcript)} of ${count(t2.tasks_total)}`
              : undefined
          }
        />
        <Tile
          label={`bus chunks · ${snap.window.hours}h`}
          value={bus ? bus.chunks_in_window : null}
          accent="var(--series-3)"
          note={bus ? bytes(bus.chunk_bytes_in_window) : undefined}
        />
      </div>
      <div className="grid">
        <Panel title="Events by level" accent="var(--series-2)">
          {t ? (
            sum(t.by_level_in_window) === 0 ? (
              <Empty>
                No observability event in this window. On a busy fleet that is a
                telemetry outage, not quiet.
              </Empty>
            ) : (
              <Bars
                data={ranked(t.by_level_in_window).map(([level, n]) => ({
                  key: level,
                  value: n,
                  color:
                    level === "error" || level === "critical"
                      ? "var(--status-critical)"
                      : level === "warning"
                        ? "var(--status-warning)"
                        : "var(--series-1)",
                }))}
              />
            )
          ) : (
            <Unavailable what="Telemetry" reason={reason(snap, "telemetry")} />
          )}
        </Panel>

        <Panel title="Loudest event names" accent="var(--series-4)">
          {t ? (
            t.top_names_in_window.length === 0 ? (
              <Empty>Nothing emitted in this window.</Empty>
            ) : (
              <Bars
                data={t.top_names_in_window.map((row, index) => ({
                  key: row.name,
                  value: row.count,
                  color: index === 0 ? "var(--series-2)" : "var(--series-1)",
                }))}
              />
            )
          ) : (
            <Unavailable what="Event names" reason={reason(snap, "telemetry")} />
          )}
        </Panel>

        <Panel title="Recording coverage" accent="var(--series-5)">
          {t2 ? (
            <>
              <Bars
                data={[
                  {
                    key: "tasks with a transcript",
                    value: t2.tasks_with_transcript,
                    color: "var(--series-5)",
                  },
                  {
                    key: "tasks with none",
                    value: Math.max(0, t2.tasks_total - t2.tasks_with_transcript),
                    color: "var(--ink-muted)",
                  },
                ]}
              />
              <table className="data" style={{ marginTop: 10 }}>
                <tbody>
                  <tr>
                    <td>transcript turns stored</td>
                    <td className="n">{count(t2.rows_total)}</td>
                  </tr>
                  <tr>
                    <td>…naming the CLI that ran</td>
                    <td className="n">{count(t2.attributed_rows)}</td>
                  </tr>
                  <tr>
                    <td>…unattributed</td>
                    <td
                      className="n"
                      style={
                        t2.unattributed_rows > 0
                          ? { color: "var(--status-warning)" }
                          : undefined
                      }
                    >
                      {count(t2.unattributed_rows)}
                    </td>
                  </tr>
                  <tr>
                    <td>harness commands audited</td>
                    <td className="n">{count(t2.commands_audited)}</td>
                  </tr>
                </tbody>
              </table>
            </>
          ) : (
            <Unavailable what="Recording coverage" reason={reason(snap, "transcripts")} />
          )}
        </Panel>

        <StatusPanelOrMissing
          snap={snap}
          title="AgentBus streams"
          accent="var(--series-3)"
          data={bus?.streams_by_status}
          section="agentbus"
          emptyNote="No stream has ever been opened."
        />
        <StatusPanelOrMissing
          snap={snap}
          title="AgentBus messages"
          accent="var(--series-5)"
          data={bus?.messages_by_status}
          section="agentbus"
          emptyNote="No message has ever been queued."
        />
      </div>
    </>
  );
}

function StatusPanelOrMissing({
  snap,
  title,
  accent,
  data,
  section,
  emptyNote,
}: {
  snap: Snapshot;
  title: string;
  accent: string;
  data: Record<string, number> | undefined;
  section: string;
  emptyNote: string;
}) {
  if (!data) {
    return (
      <Panel title={title} accent={accent}>
        <Unavailable what={title} reason={reason(snap, section)} />
      </Panel>
    );
  }
  return (
    <StatusPanel title={title} accent={accent} data={data} emptyNote={emptyNote} />
  );
}

/**
 * How much of what the fleet did was written down at all.
 *
 * Stated at fleet level, above the numbers, because it changes how every
 * per-task panel should be read: with coverage this low, an empty transcript
 * is far more likely to mean "not recorded" than "nothing happened".
 */
function TranscriptCoverageBanner({ snap }: { snap: Snapshot }) {
  const t = snap.transcripts;
  if (!t) {
    return (
      <Unavailable what="Recording coverage" reason={reason(snap, "transcripts")} />
    );
  }
  if (t.coverage_fraction === null) {
    return (
      <div className="banner">
        <span className="icon" aria-hidden="true">
          !
        </span>
        <span>
          There are no tasks, so transcript coverage is undefined — not 0%.
        </span>
      </div>
    );
  }
  const pct = t.coverage_fraction * 100;
  if (pct >= 50 && t.unattributed_rows === 0) return null;
  return (
    <div className="banner serious">
      <span className="icon" aria-hidden="true">
        !
      </span>
      <span>
        <strong>
          {pct.toFixed(1)}% of tasks have a recorded transcript
          ({count(t.tasks_with_transcript)} of {count(t.tasks_total)}).
        </strong>{" "}
        An empty transcript panel on a task is therefore far more likely to mean
        "nobody recorded it" than "the agent did nothing".
        {t.unattributed_rows > 0 ? (
          <div className="unknown-text">
            {count(t.unattributed_rows)} of {count(t.rows_total)} stored turns do
            not name the CLI or model that produced them. Those rows stay
            unattributed permanently; only turns written after the executor fix
            carry attribution.
          </div>
        ) : null}
      </span>
    </div>
  );
}
