import { useMemo, useState } from "react";
import type {
  ConsoleClient,
  GraphEdge,
  GraphNode,
  ProjectGraph,
  Snapshot,
} from "../lib/api";
import { EXPECTED_GRAPH_SCHEMA } from "../lib/api";
import { computeDagLayout } from "../lib/dag-layout";
import { useProjectGraph } from "../lib/useProjectGraph";
import { Bars, Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { UNKNOWN, count, duration, ranked, shortId } from "../lib/format";
import { TERMINAL_TASK_STATES, taskStateColor } from "../lib/states";

type Filter =
  | "all"
  | "live"
  | "held"
  | "running"
  | "waiting"
  | "blocked"
  | "open"
  | "failed";

const FILTERS: Array<[Filter, string]> = [
  ["all", "All"],
  ["live", "Live"],
  ["held", "Held"],
  ["running", "Running"],
  ["waiting", "Waiting"],
  ["blocked", "Blocked"],
  ["open", "Open"],
  ["failed", "Failed"],
];

function matchesFilter(node: GraphNode, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "live") return !TERMINAL_TASK_STATES.includes(node.state);
  if (filter === "held") return node.no_dispatch;
  if (filter === "failed") return node.state === "failed";
  return node.state === filter;
}

function edgeStroke(edge: GraphEdge, selected: string | null): string {
  if (edge.from === selected || edge.to === selected) return "var(--series-1)";
  if (edge.verdict === "dead") return "var(--status-critical)";
  if (edge.verdict === "pending") return "var(--status-warning)";
  if (edge.verdict === "satisfied" || edge.verdict === "settled") {
    return "var(--status-good)";
  }
  return "var(--axis)";
}

function labelOf(node: GraphNode): string {
  const title = node.title.trim();
  if (title && title.length <= 22) return title;
  if (title) return `${title.slice(0, 20)}…`;
  return shortId(node.id);
}

function graphReason(payload: { degraded: Array<{ section: string; reason: string }> }): string | undefined {
  return payload.degraded.find((entry) => entry.section === "graph")?.reason;
}

/**
 * Live project DAG: explorer, viewport, inspector.
 *
 * This is the additional view ADR 0018 reserved — not a replacement for the
 * table spine, and not the mutating Fleet IDE. The hub owns traversal and
 * join-policy verdicts; this file only lays out what it was given.
 */
export function MissionControlView({
  snap,
  client,
  project,
  selectedId,
  refreshKey,
  onSelectProject,
  onSelectNode,
  onOpenTask,
}: {
  snap: Snapshot;
  client: ConsoleClient;
  project: string;
  selectedId: string | null;
  refreshKey: number;
  onSelectProject: (project: string) => void;
  onSelectNode: (id: string) => void;
  onOpenTask: (id: string) => void;
}) {
  if (!project) {
    return <ProjectPicker snap={snap} onSelectProject={onSelectProject} />;
  }

  return (
    <LoadedGraph
      snap={snap}
      client={client}
      project={project}
      selectedId={selectedId}
      refreshKey={refreshKey}
      onSelectProject={onSelectProject}
      onSelectNode={onSelectNode}
      onOpenTask={onOpenTask}
    />
  );
}

function ProjectPicker({
  snap,
  onSelectProject,
}: {
  snap: Snapshot;
  onSelectProject: (project: string) => void;
}) {
  const projects = snap.projects;
  if (!projects) {
    return (
      <Unavailable
        what="Projects"
        reason={snap.degraded.find((d) => d.section === "projects")?.reason}
      />
    );
  }
  return (
    <>
      <p className="empty" style={{ marginTop: 0 }}>
        Mission Control is one project at a time. Pick a project that carries
        tasks — the graph is live from the ledger, capped so a 6,000-row
        project does not become a hairball.
      </p>
      {projects.rows.length === 0 ? (
        <Empty>No project carries any task.</Empty>
      ) : (
        <Panel title="Projects with work" wide accent="var(--series-2)">
          <table className="data">
            <thead>
              <tr>
                <th>project</th>
                <th style={{ textAlign: "right" }}>live</th>
                <th style={{ textAlign: "right" }}>total</th>
              </tr>
            </thead>
            <tbody>
              {projects.rows.map((row) => (
                <tr key={row.project}>
                  <td>
                    <button
                      type="button"
                      className="rowlink"
                      onClick={() => onSelectProject(row.project)}
                    >
                      {row.project}
                    </button>
                  </td>
                  <td className="n">{count(row.live)}</td>
                  <td className="n">{count(row.total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </>
  );
}

function LoadedGraph({
  snap,
  client,
  project,
  selectedId,
  refreshKey,
  onSelectProject,
  onSelectNode,
  onOpenTask,
}: {
  snap: Snapshot;
  client: ConsoleClient;
  project: string;
  selectedId: string | null;
  refreshKey: number;
  onSelectProject: (project: string) => void;
  onSelectNode: (id: string) => void;
  onOpenTask: (id: string) => void;
}) {
  const { payload, error } = useProjectGraph(client, project, refreshKey);
  const [filter, setFilter] = useState<Filter>("all");

  if (error) {
    return (
      <div className="banner critical">
        <span className="icon" aria-hidden="true">
          ▲
        </span>
        <span>
          <strong>Could not read the graph for {project}.</strong> {error}
        </span>
      </div>
    );
  }

  if (!payload) {
    return <p className="empty">Reading the graph…</p>;
  }

  if (payload.schema !== EXPECTED_GRAPH_SCHEMA) {
    return (
      <div className="banner critical">
        <span className="icon" aria-hidden="true">
          ▲
        </span>
        <span>
          <strong>Schema mismatch.</strong> The hub returned{" "}
          <code>{payload.schema}</code>, which this console build does not
          understand. Fields may be missing or misread.
        </span>
      </div>
    );
  }

  if (!payload.graph) {
    return (
      <Unavailable what="Project graph" reason={graphReason(payload)} />
    );
  }

  return (
    <GraphBody
      snap={snap}
      project={project}
      graph={payload.graph}
      filter={filter}
      selectedId={selectedId}
      onFilter={setFilter}
      onSelectProject={onSelectProject}
      onSelectNode={onSelectNode}
      onOpenTask={onOpenTask}
    />
  );
}

function GraphBody({
  snap,
  project,
  graph,
  filter,
  selectedId,
  onFilter,
  onSelectProject,
  onSelectNode,
  onOpenTask,
}: {
  snap: Snapshot;
  project: string;
  graph: ProjectGraph;
  filter: Filter;
  selectedId: string | null;
  onFilter: (filter: Filter) => void;
  onSelectProject: (project: string) => void;
  onSelectNode: (id: string) => void;
  onOpenTask: (id: string) => void;
}) {
  const visible = useMemo(
    () => graph.nodes.filter((node) => matchesFilter(node, filter)),
    [graph.nodes, filter],
  );
  const selected =
    visible.find((node) => node.id === selectedId) ?? visible[0] ?? null;

  const layout = useMemo(() => {
    const ids = visible.map((node) => node.id);
    const idset = new Set(ids);
    const edges = graph.edges
      .filter((edge) => edge.from_in_view && idset.has(edge.from) && idset.has(edge.to))
      .map((edge) => ({ from: edge.from, to: edge.to }));
    return computeDagLayout(ids, edges);
  }, [visible, graph.edges]);

  const byId = useMemo(() => {
    const map: Record<string, GraphNode> = {};
    for (const node of graph.nodes) map[node.id] = node;
    return map;
  }, [graph.nodes]);

  const incoming = selected
    ? graph.edges.filter((edge) => edge.to === selected.id)
    : [];
  const outgoing = selected
    ? graph.edges.filter((edge) => edge.from === selected.id)
    : [];

  const heldNote =
    graph.truncated || graph.omitted > 0 ? "of the tasks in this view" : undefined;

  return (
    <>
      <div className="mc-toolbar">
        <span className="micro">project</span>
        <strong>{project}</strong>
        <button
          type="button"
          className="ghost"
          onClick={() => onSelectProject("")}
        >
          change
        </button>
        {snap.projects?.rows
          .filter((row) => row.project !== project)
          .slice(0, 6)
          .map((row) => (
            <button
              key={row.project}
              type="button"
              className="ghost"
              onClick={() => onSelectProject(row.project)}
            >
              {row.project}
            </button>
          ))}
      </div>

      <div className="tiles">
        <Tile
          label="in project"
          value={graph.total}
          accent="var(--series-1)"
          note={
            graph.truncated
              ? `${count(graph.shown)} shown, ${count(graph.omitted)} omitted`
              : undefined
          }
        />
        <Tile
          label="live"
          value={graph.live_total}
          accent="var(--status-warning)"
        />
        <Tile
          label="held"
          value={graph.held}
          accent="var(--series-4)"
          note={heldNote}
          tone={graph.held > 0 ? "warn" : undefined}
        />
        <Tile
          label="dead-blocked"
          value={graph.dead_blocked}
          accent="var(--status-critical)"
          tone={graph.dead_blocked > 0 ? "bad" : undefined}
          note="failed blocker, join all_success"
        />
        <Tile
          label="running"
          value={graph.by_state["running"] ?? 0}
          accent="var(--stage-3)"
        />
      </div>

      {graph.truncated ? (
        <div className="banner">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>
              Showing {count(graph.shown)} of {count(graph.total)} tasks.
            </strong>{" "}
            Live work filled the cap first
            {graph.omitted_live
              ? `; ${count(graph.omitted_live)} live tasks are also omitted`
              : ""}
            . {count(graph.omitted_edges)} edges point at a task that is not in
            this view.
          </span>
        </div>
      ) : graph.omitted_edges > 0 ? (
        <div className="banner">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>
              {count(graph.omitted_edges)} blocker
              {graph.omitted_edges === 1 ? "" : "s"} live in another project or
              were omitted.
            </strong>{" "}
            They still appear in the inspector so a root in this view is not
            mistaken for unblocked work.
          </span>
        </div>
      ) : null}

      <div className="mc-filters">
        {FILTERS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            className="ghost"
            aria-pressed={filter === id}
            onClick={() => onFilter(id)}
          >
            {label}
          </button>
        ))}
      </div>

      {graph.nodes.length === 0 ? (
        <Empty>This project has no tasks.</Empty>
      ) : visible.length === 0 ? (
        <Empty>No tasks match this filter.</Empty>
      ) : (
        <div className="mc-layout">
          <Panel
            title="Queue explorer"
            accent="var(--series-1)"
            sub={`${count(visible.length)} shown`}
          >
            <ul className="mc-explorer">
              {visible.map((node) => (
                <li key={node.id}>
                  <button
                    type="button"
                    className="mc-explorer-item"
                    aria-current={selected?.id === node.id}
                    onClick={() => onSelectNode(node.id)}
                  >
                    <span
                      className="swatch"
                      style={{ background: taskStateColor(node.state) }}
                    />
                    <span className="truncate" title={node.title}>
                      {node.title || shortId(node.id)}
                    </span>
                    <span className="micro">{node.state}</span>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>

          <Panel
            title="Dependency viewport"
            accent="var(--series-2)"
            sub="left is prerequisite"
          >
            <div className="mc-graph">
              <svg
                width="100%"
                height={Math.max(layout.height, 240)}
                viewBox={`0 0 ${layout.width} ${layout.height}`}
                role="img"
                aria-label={`${project} task dependency graph, prerequisite on the left`}
              >
                {layout.ranks.map((band) => (
                  <rect
                    key={band.rank}
                    x={band.x}
                    y={band.y}
                    width={band.width}
                    height={band.height}
                    fill="var(--surface-2)"
                  />
                ))}
                {layout.edges.map((laid, index) => {
                  const raw = graph.edges.find(
                    (edge) => edge.from === laid.from && edge.to === laid.to,
                  );
                  const selectedHit =
                    laid.from === selected?.id || laid.to === selected?.id;
                  return (
                    <line
                      key={`${laid.from}-${laid.to}-${index}`}
                      x1={laid.sourceX}
                      y1={laid.sourceY}
                      x2={laid.targetX}
                      y2={laid.targetY}
                      stroke={raw ? edgeStroke(raw, selected?.id ?? null) : "var(--axis)"}
                      strokeWidth={selectedHit ? 2 : 1}
                      strokeDasharray={
                        laid.isBackEdge || raw?.verdict === "dead" ? "4 3" : undefined
                      }
                    />
                  );
                })}
                {layout.nodes.map((laid) => {
                  const node = byId[laid.id];
                  if (!node) return null;
                  const active = laid.id === selected?.id;
                  return (
                    <g
                      key={laid.id}
                      onClick={() => onSelectNode(laid.id)}
                      style={{ cursor: "pointer" }}
                    >
                      <rect
                        x={laid.x}
                        y={laid.y}
                        width={laid.width}
                        height={laid.height}
                        fill={active ? "var(--series-1)" : "var(--surface-1)"}
                        stroke={
                          node.no_dispatch
                            ? "var(--series-4)"
                            : active
                              ? "var(--series-1)"
                              : taskStateColor(node.state)
                        }
                        strokeWidth={active || node.no_dispatch ? 2 : 1}
                      />
                      <text
                        x={laid.x + laid.width / 2}
                        y={laid.y + laid.height / 2 + 4}
                        textAnchor="middle"
                        fill={active ? "#fff" : "var(--ink-primary)"}
                        fontSize={10}
                        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                      >
                        {labelOf(node)}
                      </text>
                    </g>
                  );
                })}
              </svg>
            </div>
          </Panel>
        </div>
      )}

      {selected ? (
        <Inspector
          node={selected}
          incoming={incoming}
          outgoing={outgoing}
          byId={byId}
          onSelectNode={onSelectNode}
          onOpenTask={onOpenTask}
        />
      ) : null}

      <div className="grid">
        <Panel title="By state" accent="var(--series-2)">
          <Bars
            data={ranked(graph.by_state).map(([state, n]) => ({
              key: state,
              value: n,
              color: taskStateColor(state),
            }))}
          />
        </Panel>
      </div>
    </>
  );
}

function Inspector({
  node,
  incoming,
  outgoing,
  byId,
  onSelectNode,
  onOpenTask,
}: {
  node: GraphNode;
  incoming: GraphEdge[];
  outgoing: GraphEdge[];
  byId: Record<string, GraphNode>;
  onSelectNode: (id: string) => void;
  onOpenTask: (id: string) => void;
}) {
  return (
    <Panel
      title={`${node.title || shortId(node.id)}`}
      wide
      accent={taskStateColor(node.state)}
      sub={shortId(node.id)}
    >
      <div className="mc-inspector">
        <div className="chip">
          <span className="swatch" style={{ background: taskStateColor(node.state) }} />
          {node.state}
        </div>
        {node.no_dispatch ? <span className="chip">held · no_dispatch</span> : null}
        {node.cyclic ? <span className="chip">cycle</span> : null}
        <span className="micro">join {node.join_policy}</span>
        <span className="micro">priority {node.priority}</span>
        <span className="micro">
          dwell {node.dwell_seconds === null ? UNKNOWN : duration(node.dwell_seconds)}
        </span>
        <span className="micro">
          owner {node.owner_agent_id ? shortId(node.owner_agent_id) : UNKNOWN}
        </span>
        <button type="button" className="ghost" onClick={() => onOpenTask(node.id)}>
          open task
        </button>
      </div>
      <p className="mc-edge-list">
        <strong>Waits on</strong>
        {incoming.length === 0 ? (
          <span> nothing — this is a root in the current view.</span>
        ) : (
          incoming.map((edge) => (
            <EdgeRef
              key={`${edge.from}-${edge.to}`}
              edge={edge}
              end="from"
              byId={byId}
              onSelectNode={onSelectNode}
            />
          ))
        )}
      </p>
      <p className="mc-edge-list">
        <strong>Unblocks</strong>
        {outgoing.length === 0 ? (
          <span> no later waiter in this view.</span>
        ) : (
          outgoing.map((edge) => (
            <EdgeRef
              key={`${edge.from}-${edge.to}-out`}
              edge={edge}
              end="to"
              byId={byId}
              onSelectNode={onSelectNode}
            />
          ))
        )}
      </p>
    </Panel>
  );
}

function EdgeRef({
  edge,
  end,
  byId,
  onSelectNode,
}: {
  edge: GraphEdge;
  end: "from" | "to";
  byId: Record<string, GraphNode>;
  onSelectNode: (id: string) => void;
}) {
  const id = end === "from" ? edge.from : edge.to;
  const inView = end === "from" ? edge.from_in_view : Boolean(byId[id]);
  const title =
    end === "from"
      ? edge.from_title || byId[id]?.title || shortId(id)
      : byId[id]?.title || shortId(id);
  const state = end === "from" ? edge.from_state : byId[id]?.state;
  return (
    <span className="mc-edge-ref">
      {inView ? (
        <button type="button" className="rowlink" onClick={() => onSelectNode(id)}>
          {title}
        </button>
      ) : (
        <span title={id}>
          {title}
          {edge.from_project ? ` · ${edge.from_project}` : ""} (not in this view)
        </span>
      )}
      <span className="unknown-text">
        {" "}
        {state || UNKNOWN} · {edge.verdict}
      </span>
    </span>
  );
}
