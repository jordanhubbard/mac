import type { Snapshot } from "../lib/api";
import { Bars, Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { UNKNOWN, count, duration, ranked, sum } from "../lib/format";
import {
  agentStatusColor,
  healthColor,
  orderStates,
  taskStateColor,
} from "../lib/states";

function reason(snap: Snapshot, section: string): string | undefined {
  return snap.degraded.find((d) => d.section === section)?.reason;
}

/**
 * Agents, and whether the hub's belief about them is supported by evidence.
 *
 * The hub stores a *reported* status. This view puts that reported status next
 * to the last time the agent was actually heard from, because the two have
 * been observed to disagree — agents reporting `busy` while unreachable, and a
 * nap ticker flipping OFFLINE agents to IDLE. The console does not adjudicate;
 * it shows both and marks the contradiction.
 */
export function AgentsView({ snap }: { snap: Snapshot }) {
  const agents = snap.agents;
  if (!agents) {
    return <Unavailable what="Agents" reason={reason(snap, "agents")} />;
  }
  const doubted = agents.rows.filter((r) => r.belief_contradicted);
  const neverSeen = agents.rows.filter((r) => r.seconds_since_seen === null);
  // Refused is not offline. An offline agent stopped talking; a refused one is
  // talking constantly and being turned away, and the fix is on the hub side.
  const refused = agents.rows.filter((r) => r.registration_state === "refused");

  return (
    <>
      <div className="tiles">
        <Tile label="agents" value={agents.total} accent="var(--series-3)" />
        <Tile
          label="idle"
          value={agents.by_status["idle"] ?? 0}
          accent={agentStatusColor("idle")}
        />
        <Tile
          label="busy"
          value={agents.by_status["busy"] ?? 0}
          accent={agentStatusColor("busy")}
        />
        <Tile
          label="offline"
          value={agents.by_status["offline"] ?? 0}
          accent="var(--ink-muted)"
        />
        <Tile
          label="status not believable"
          value={doubted.length}
          accent="var(--status-critical)"
          tone={doubted.length ? "bad" : undefined}
          note="reports idle/busy, unheard >15m"
        />
        <Tile
          label="refused"
          value={refused.length}
          accent="var(--status-critical)"
          tone={refused.length ? "bad" : undefined}
          note="registration rejected by the hub"
        />
      </div>

      {refused.length > 0 ? (
        <div className="banner critical">
          <span className="icon" aria-hidden="true">
            ▲
          </span>
          <span>
            <strong>
              {count(refused.length)} host
              {refused.length === 1 ? " is" : "s are"} being refused at
              registration.
            </strong>{" "}
            {refused.map((row) => (
              <span key={row.id}>
                {row.name}:{" "}
                {row.registration_refusal?.message ??
                  "registration payload rejected"}{" "}
                (refused {count(row.registration_refusal?.refusal_count ?? 0)}×).{" "}
              </span>
            ))}
            This is not the same as offline — the host is running and asking to
            join.
          </span>
        </div>
      ) : null}

      {doubted.length > 0 ? (
        <div className="banner critical">
          <span className="icon" aria-hidden="true">
            ▲
          </span>
          <span>
            <strong>
              {count(doubted.length)} agent{doubted.length === 1 ? "" : "s"}{" "}
              {doubted.length === 1 ? "reports" : "report"} a status the evidence
              does not support.
            </strong>{" "}
            They claim idle or busy but the hub has not heard from them in over 15
            minutes. Treat any capacity number that counts them as optimistic.
          </span>
        </div>
      ) : null}

      <div className="grid">
        <Panel title="By reported status" accent="var(--series-3)">
          <Bars
            data={ranked(agents.by_status).map(([status, n]) => ({
              key: status,
              value: n,
              color: agentStatusColor(status),
            }))}
          />
        </Panel>
        <Panel title="By health" accent="var(--series-4)">
          <Bars
            data={ranked(agents.by_health).map(([health, n]) => ({
              key: health,
              value: n,
              color: healthColor(health),
            }))}
          />
        </Panel>

        <Panel
          title="Roster"
          wide
          accent="var(--series-1)"
          sub={
            agents.truncated
              ? `${count(agents.rows.length)} of ${count(agents.total)} shown`
              : `${count(agents.rows.length)} live`
          }
        >
          {agents.rows.length === 0 ? (
            <Empty>No agents are registered.</Empty>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>agent</th>
                  <th>status</th>
                  <th>health</th>
                  <th style={{ textAlign: "right" }}>last heard</th>
                  <th style={{ textAlign: "right" }}>tasks</th>
                  <th style={{ textAlign: "right" }}>leases</th>
                </tr>
              </thead>
              <tbody>
                {agents.rows.map((row) => (
                  <tr key={row.id} className={row.belief_contradicted ? "just-moved" : undefined}>
                    <td className="truncate" title={row.id}>
                      {row.name}
                      {row.dispatch_hold ? (
                        <span className="unknown-text"> · dispatch held</span>
                      ) : null}
                    </td>
                    <td>
                      <span className="chip">
                        <span
                          className="swatch"
                          style={{ background: agentStatusColor(row.status) }}
                        />
                        {row.status}
                        {row.belief_contradicted ? (
                          <span style={{ color: "var(--status-critical)" }}>
                            {" "}
                            ▲ unverified
                          </span>
                        ) : null}
                        {row.registration_state === "refused" ? (
                          <span
                            style={{ color: "var(--status-critical)" }}
                            title={
                              row.registration_refusal?.message ?? undefined
                            }
                          >
                            {" "}
                            ▲ {row.registered === false ? "never admitted" : "refused"}
                          </span>
                        ) : null}
                      </span>
                    </td>
                    <td>
                      <span className="chip">
                        <span
                          className="swatch"
                          style={{ background: healthColor(row.health_status) }}
                        />
                        {row.health_status}
                      </span>
                    </td>
                    <td
                      className="n"
                      style={
                        row.belief_contradicted
                          ? { color: "var(--status-critical)" }
                          : undefined
                      }
                    >
                      {row.seconds_since_seen === null
                        ? "never"
                        : duration(row.seconds_since_seen)}
                    </td>
                    <td className="n">{count(row.open_tasks)}</td>
                    <td className="n">{count(row.active_leases)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {neverSeen.length ? (
            <p className="unknown-text" style={{ fontSize: 11 }}>
              {count(neverSeen.length)} agent
              {neverSeen.length === 1 ? " has" : "s have"} no readable last_seen_at.
            </p>
          ) : null}
        </Panel>
      </div>
    </>
  );
}

/** Projects: which ones carry the live work, and how it is distributed. */
export function ProjectsView({ snap }: { snap: Snapshot }) {
  const projects = snap.projects;
  if (!projects) {
    return <Unavailable what="Projects" reason={reason(snap, "projects")} />;
  }
  const states = orderStates(
    projects.rows.flatMap((row) => Object.keys(row.by_state)),
  );

  return (
    <>
      <div className="tiles">
        <Tile
          label="registered projects"
          value={sum(projects.registered_by_status)}
          accent="var(--series-1)"
        />
        <Tile
          label="active"
          value={projects.registered_by_status["active"] ?? 0}
          accent="var(--status-good)"
        />
        <Tile
          label="projects carrying tasks"
          value={projects.with_tasks}
          accent="var(--series-2)"
          note={projects.truncated ? `top ${projects.rows.length} shown` : undefined}
        />
      </div>

      <div className="grid">
        <Panel title="Live work by project" wide accent="var(--series-2)">
          {projects.rows.length === 0 ? (
            <Empty>No project carries any task.</Empty>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>project</th>
                  <th style={{ textAlign: "right" }}>live</th>
                  <th style={{ textAlign: "right" }}>total</th>
                  <th>distribution</th>
                </tr>
              </thead>
              <tbody>
                {projects.rows.map((row) => (
                  <tr key={row.project}>
                    <td className="truncate">{row.project}</td>
                    <td className="n">{count(row.live)}</td>
                    <td className="n">{count(row.total)}</td>
                    <td>
                      <span
                        style={{ display: "flex", gap: 2, height: 12, minWidth: 120 }}
                      >
                        {states
                          .filter((state) => (row.by_state[state] ?? 0) > 0)
                          .map((state) => (
                            <span
                              key={state}
                              title={`${state}: ${row.by_state[state]}`}
                              style={{
                                flex: row.by_state[state],
                                background: taskStateColor(state),
                              }}
                            />
                          ))}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Registry status" accent="var(--series-1)">
          {sum(projects.registered_by_status) === 0 ? (
            <Empty>No projects are registered.</Empty>
          ) : (
            <Bars
              data={ranked(projects.registered_by_status).map(([status, n]) => ({
                key: status === "(none)" ? UNKNOWN : status,
                value: n,
                color: healthColor(status),
              }))}
            />
          )}
        </Panel>
      </div>
    </>
  );
}
