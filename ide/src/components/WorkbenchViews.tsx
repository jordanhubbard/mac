import { useEffect, useMemo, useState } from "react";
import { api, type Agent, type AgentCard, type CommunicationAccount, type CommunicationIdentity, type DashboardAgent, type DashboardState, type GatewayIdentityLease, type RepresentationBinding, type TaskDetail } from "../api/mac";
import type { WorkbenchView } from "./ActivityRail";
import { agentHardware, availabilityLabel, availableCodingClis, cpuLabel, gpuName, isAgentOnline, memoryLabel, platformLabel } from "./agentFacts";
import { TaskInspector } from "./TaskInspector";
import { TaskKanban } from "./TaskKanban";
import { WorkGraph } from "./WorkGraph";

const TERMINAL_STATES = new Set(["completed", "cancelled", "failed"]);

function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.join(", ") || fallback;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function age(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.valueOf())) return text(value);
  const seconds = Math.max(0, Math.round((Date.now() - date.valueOf()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function WorkbenchViewContent({
  view,
  data,
  card,
  selectedTaskId,
  selectedAgentId,
  selectedProjectId,
  onSelectTask,
  onSelectAgent,
  onRefresh,
}: {
  view: WorkbenchView;
  data: DashboardState;
  card: AgentCard | null;
  selectedTaskId: string | null;
  selectedAgentId: string | null;
  /** null = "All projects" — no project filter */
  selectedProjectId: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectAgent: (agentId: string) => void;
  onRefresh: () => void | Promise<void>;
}) {
  const agents = data.agents.map((item) => item.agent);
  switch (view) {
    case "work":
      return (
        <WorkView
          data={data}
          onRefresh={onRefresh}
          onSelectTask={onSelectTask}
          selectedProjectId={selectedProjectId}
          selectedTaskId={selectedTaskId}
        />
      );
    case "workflows":
      return <WorkflowView data={data} onRefresh={onRefresh} />;
    case "agents":
      return (
        <AgentsView
          agents={data.agents}
          onSelectAgent={onSelectAgent}
          selectedAgentId={selectedAgentId}
        />
      );
    case "runtime":
      return <RuntimeView data={data} />;
    case "observability":
      return <ObservabilityView data={data} />;
    case "connections":
      return <ConnectionsView card={card} data={data} />;
    default:
      return (
        <CockpitView
          agents={agents}
          data={data}
          onRefresh={onRefresh}
          onSelectTask={onSelectTask}
          selectedProjectId={selectedProjectId}
          selectedTaskId={selectedTaskId}
        />
      );
  }
}

function ViewHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="view-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="view-actions">{actions}</div> : null}
    </header>
  );
}

function CockpitView({
  data,
  agents,
  selectedTaskId,
  selectedProjectId,
  onSelectTask,
  onRefresh,
}: {
  data: DashboardState;
  agents: Agent[];
  selectedTaskId: string | null;
  selectedProjectId: string | null;
  onSelectTask: (taskId: string) => void;
  onRefresh: () => void | Promise<void>;
}) {
  const [inspectedTaskId, setInspectedTaskId] = useState<string | null>(null);
  const counts = data.overview.counts;
  const scopedTasks = useMemo(
    () =>
      selectedProjectId
        ? data.tasks.filter(
            ({ task }) => (task.project || "unassigned") === selectedProjectId,
          )
        : data.tasks,
    [data.tasks, selectedProjectId],
  );
  const states = useMemo(() => {
    if (!selectedProjectId) return data.overview.task_states;
    const totals: Record<string, number> = {};
    for (const { task } of scopedTasks) {
      const state = String(task.state || "open");
      totals[state] = (totals[state] ?? 0) + 1;
    }
    return totals;
  }, [data.overview.task_states, scopedTasks, selectedProjectId]);
  const health = agents.length
    ? Math.round((Number(counts.healthy_agents || 0) / agents.length) * 100)
    : 100;
  const active = scopedTasks.filter(
    (detail) => !TERMINAL_STATES.has(String(detail.task.state)),
  ).length;
  const inspectedTask = inspectedTaskId
    ? data.tasks.find(({ task }) => task.id === inspectedTaskId) || null
    : null;
  const canWrite = data.session?.can_write !== false;
  function inspectTask(taskId: string) {
    onSelectTask(taskId);
    setInspectedTaskId(taskId);
  }
  return (
    <main className="workbench-view cockpit-view">
      <ViewHeader
        description={`${selectedProjectId ? `Project ${selectedProjectId}` : "Live control plane"} · updated ${age(data.updated_at)}`}
        eyebrow="Operator workbench"
        title="Fleet cockpit"
      />
      <div className="telemetry-strip">
        <Metric icon="heart-pulse" label="Fleet health" tone="good" value={`${health}`} unit="/ 100" />
        <Metric icon="pulse" label="Active" value={active} />
        <Metric icon="person-add" label="Review" tone="warn" value={Number(states.needs_review || 0) + Number(states.reviewing || 0)} />
        <Metric icon="error" label="Blocked" tone="bad" value={states.blocked || 0} />
        <Metric icon="organization" label="A2A routable" value={agents.length} />
      </div>
      <section className="primary-surface graph-surface">
        <div className="surface-heading">
          <div>
            <span className="surface-kicker">Active work graph</span>
            <span className="surface-note">
              Up to 25 active tasks{selectedProjectId ? ` in ${selectedProjectId}` : ""} · arrows are
              unresolved dependencies
            </span>
          </div>
          <span className="live-indicator"><span /> stream connected</span>
        </div>
        <WorkGraph
          agents={agents}
          onInspectTask={inspectTask}
          onSelectTask={onSelectTask}
          selectedTaskId={selectedTaskId}
          tasks={scopedTasks}
        />
      </section>
      {inspectedTask ? (
        <TaskInspector
          canWrite={canWrite}
          detail={inspectedTask}
          key={inspectedTask.task.id}
          onClose={() => setInspectedTaskId(null)}
          onRefresh={onRefresh}
        />
      ) : null}
    </main>
  );
}

function Metric({
  icon,
  label,
  value,
  unit,
  tone = "info",
}: {
  icon: string;
  label: string;
  value: string | number;
  unit?: string;
  tone?: "info" | "good" | "warn" | "bad";
}) {
  return (
    <div className={`metric tone-${tone}`}>
      <i className={`codicon codicon-${icon}`} />
      <span className="metric-copy">
        <span>{label}</span>
        <strong>{value}<small>{unit}</small></strong>
      </span>
    </div>
  );
}

function WorkView({
  data,
  selectedTaskId,
  selectedProjectId,
  onSelectTask,
  onRefresh,
}: {
  data: DashboardState;
  selectedTaskId: string | null;
  /** null = all projects */
  selectedProjectId: string | null;
  onSelectTask: (taskId: string) => void;
  onRefresh: () => void | Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [inspectedTaskId, setInspectedTaskId] = useState<string | null>(null);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return data.tasks.filter(({ task }) => {
      if (selectedProjectId && (task.project || "unassigned") !== selectedProjectId) return false;
      return (
        !needle ||
        [task.title, task.project, task.state, task.id].some((value) =>
          String(value || "")
            .toLowerCase()
            .includes(needle),
        )
      );
    });
  }, [data.tasks, query, selectedProjectId]);
  const inspectedTask = inspectedTaskId
    ? data.tasks.find(({ task }) => task.id === inspectedTaskId) || null
    : null;
  const canWrite = data.session?.can_write !== false;
  function inspectTask(taskId: string) {
    onSelectTask(taskId);
    setInspectedTaskId(taskId);
  }
  return (
    <main className="workbench-view">
      <ViewHeader
        description={
          selectedProjectId
            ? `Showing tasks for project: ${selectedProjectId}`
            : "Search, inspect, and steer every ledger task."
        }
        eyebrow="Ledger"
        title="Work"
      />
      <div className="table-toolbar">
        <label className="command-input compact">
          <i className="codicon codicon-search" />
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search task, project, or state"
            value={query}
          />
        </label>
        <span>{visible.length} tasks{selectedProjectId ? ` in ${selectedProjectId}` : ""}</span>
      </div>
      <TaskKanban
        onInspectTask={inspectTask}
        onSelectTask={onSelectTask}
        selectedTaskId={selectedTaskId}
        tasks={visible}
      />
      {inspectedTask ? (
        <TaskInspector
          canWrite={canWrite}
          detail={inspectedTask}
          key={inspectedTask.task.id}
          onClose={() => setInspectedTaskId(null)}
          onRefresh={onRefresh}
        />
      ) : null}
    </main>
  );
}

function WorkflowView({ data, onRefresh }: { data: DashboardState; onRefresh: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const runs = Array.isArray(data.workflow_runs.latest) ? data.workflow_runs.latest as Array<Record<string, unknown>> : [];

  async function generate() {
    if (!prompt.trim()) return;
    setBusy(true);
    setMessage("");
    try {
      setPreview(await api.workflowPlanPreview(prompt.trim(), { source: "fleet-workbench" }));
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function accept() {
    if (!preview) return;
    setBusy(true);
    try {
      await api.workflowPlanAccept(preview);
      setMessage("Workflow accepted and released to the ledger.");
      setPreview(null);
      setPrompt("");
      onRefresh();
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }

  const nodes = preview && Array.isArray(preview.nodes) ? preview.nodes as Array<Record<string, unknown>> : [];
  return (
    <main className="workbench-view">
      <ViewHeader description="Turn an objective into an inspectable DAG, then approve it into the ledger." eyebrow="DAG automation" title="Workflow studio" />
      <div className="split-grid workflow-layout">
        <section className="primary-surface planner-surface">
          <div className="surface-heading"><span className="surface-kicker">Plan with the fleet</span></div>
          <textarea
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="Describe the desired outcome, constraints, verification, and rollout policy…"
            value={prompt}
          />
          <div className="form-actions">
            <button className="button primary" disabled={busy || !prompt.trim()} onClick={generate} type="button">
              <i className="codicon codicon-sparkle" /> {busy ? "Planning…" : "Generate graph"}
            </button>
            {preview ? <button className="button" disabled={busy} onClick={accept} type="button">Accept plan</button> : null}
            <span className="form-message">{message}</span>
          </div>
          <div className="workflow-preview">
            {preview ? (
              <>
                <div className="preview-summary">
                  <strong>{text(preview.title || preview.name, "Proposed workflow")}</strong>
                  <span>{nodes.length} nodes · inspect before accepting</span>
                </div>
                <div className="preview-nodes">
                  {nodes.map((node, index) => (
                    <div className="preview-node" key={text(node.id || node.key, String(index))}>
                      <span>{index + 1}</span>
                      <strong>{text(node.title || node.name || node.key, `Step ${index + 1}`)}</strong>
                      <small>{text(node.required_role || node.role || node.type)}</small>
                    </div>
                  ))}
                </div>
              </>
            ) : <div className="empty-state centered"><i className="codicon codicon-type-hierarchy" /><strong>No draft graph</strong><span>Your prompt will be previewed here before anything is created.</span></div>}
          </div>
        </section>
        <section className="primary-surface compact-surface">
          <div className="surface-heading"><span className="surface-kicker">Definitions</span><span>{data.workflows.length}</span></div>
          <RecordList records={data.workflows} primary="name" secondary="description" state="enabled" />
          <div className="surface-heading subheading"><span className="surface-kicker">Recent runs</span><span>{runs.length}</span></div>
          <RecordList records={runs} primary="workflow_id" secondary="current_node_key" state="state" />
        </section>
      </div>
    </main>
  );
}

function AgentsView({
  agents,
  selectedAgentId,
  onSelectAgent,
}: {
  agents: DashboardAgent[];
  selectedAgentId: string | null;
  onSelectAgent: (agentId: string) => void;
}) {
  return (
    <main className="workbench-view">
      <ViewHeader description="Capabilities, health, workload, and interoperability at a glance." eyebrow="A2A + ACP" title="Agent mesh" />
      <div className="agent-grid">
        {agents.map((item) => {
          const { agent } = item;
          const hardware = agentHardware(item);
          const gpu = gpuName(item);
          const codingClis = availableCodingClis(item);
          return (
          <button
            className={`agent-tile ${agent.id === selectedAgentId ? "selected" : ""}`}
            key={agent.id}
            onClick={() => onSelectAgent(agent.id)}
            type="button"
          >
            <div className="agent-tile-head">
              <span className="agent-avatar">{(agent.name || agent.id)[0]?.toUpperCase()}</span>
              <span><strong>{agent.name || agent.id.replace(/^agent_/, "")}</strong><small>{platformLabel(item)}</small></span>
              <span className={`presence ${isAgentOnline(item) ? "online" : "offline"}`} />
            </div>
            {Object.keys(hardware).length ? (
              <div className="agent-observed-facts">
                <span><small>CPU</small><strong>{cpuLabel(item)}</strong></span>
                <span><small>Memory</small><strong>{memoryLabel(item)}</strong></span>
                <span><small>GPU</small><strong>{gpu || "None reported"}</strong></span>
              </div>
            ) : <div className="agent-no-report">No observed hardware report</div>}
            <div className="agent-tooling">
              <small>Available coding CLIs</small>
              <span>{codingClis.length ? codingClis.join(" · ") : "none reported"}</span>
            </div>
            <div className="agent-tile-foot">
              <span>{agent.status || (agent.current_task_id ? "busy" : "idle")}</span>
              <span title={(item.availability?.reasons || []).join("; ")}>{availabilityLabel(item)}</span>
            </div>
          </button>
          );
        })}
      </div>
    </main>
  );
}

function RuntimeView({ data }: { data: DashboardState }) {
  return (
    <main className="workbench-view">
      <ViewHeader description="Promote validated runtime changes and watch canary health." eyebrow="Delivery" title="Runtime & rollouts" />
      <div className="three-column-grid">
        <RecordSection label="Runtime deltas" records={data.runtime_deltas} primary="summary" secondary="id" state="status" />
        <RecordSection label="Active runs" records={data.runtime_runs} primary="runtime_id" secondary="id" state="status" />
        <RecordSection label="Rollouts" records={data.rollouts} primary="version" secondary="strategy" state="status" />
      </div>
    </main>
  );
}

function ObservabilityView({ data }: { data: DashboardState }) {
  const records = [...data.events].reverse().slice(0, 100);
  return (
    <main className="workbench-view">
      <ViewHeader description="A unified stream of task, command, policy, notification, and integration events." eyebrow="Telemetry" title="Observability" />
      <div className="observability-layout">
        <section className="primary-surface event-surface">
          <div className="surface-heading"><span className="surface-kicker">Control-plane event stream</span><span>{records.length}</span></div>
          <EventLines records={records} />
        </section>
        <aside className="signal-sidebar">
          <RecordSection label="Notifications" records={data.notifications} primary="title" secondary="subject_id" state="status" />
          <RecordSection label="Integration findings" records={data.integration_findings} primary="title" secondary="source_id" state="status" />
        </aside>
      </div>
    </main>
  );
}

function ConnectionsView({ data, card }: { data: DashboardState; card: AgentCard | null }) {
  const [identities, setIdentities] = useState<CommunicationIdentity[]>([]);
  const [accounts, setAccounts] = useState<CommunicationAccount[]>([]);
  const [representations, setRepresentations] = useState<RepresentationBinding[]>([]);
  const [leases, setLeases] = useState<GatewayIdentityLease[]>([]);
  const [communicationError, setCommunicationError] = useState("");
  useEffect(() => {
    let active = true;
    void Promise.all([
      api.listCommunicationIdentities(),
      api.listCommunicationAccounts(),
      api.listRepresentationBindings(),
      api.listGatewayIdentityLeases(),
    ]).then(([nextIdentities, nextAccounts, nextRepresentations, nextLeases]) => {
      if (!active) return;
      setIdentities(nextIdentities);
      setAccounts(nextAccounts);
      setRepresentations(nextRepresentations);
      setLeases(nextLeases);
      setCommunicationError("");
    }).catch((error: unknown) => {
      if (active) setCommunicationError(String(error));
    });
    return () => { active = false; };
  }, []);
  const identityById = useMemo(
    () => new Map(identities.map((identity) => [identity.id, identity.display_name || identity.name])),
    [identities],
  );
  const accountById = useMemo(
    () => new Map(accounts.map((account) => [account.id, `${account.channel}/${account.account_id}`])),
    [accounts],
  );
  const identityRecords = identities.map((identity) => ({
    ...identity,
    role: identity.is_default ? "fleet default" : "optional direct identity",
    status: identity.enabled ? "enabled" : "disabled",
  }));
  const accountRecords = accounts.map((account) => ({
    ...account,
    name: `${account.channel}/${account.account_id}`,
    identity: identityById.get(account.identity_id) || account.identity_id,
    status: account.enabled ? "enabled" : "disabled",
  }));
  const representationRecords = representations.map((binding) => ({
    ...binding,
    name: `${binding.subject_kind}:${binding.subject_id}`,
    identity: binding.identity_id ? identityById.get(binding.identity_id) || binding.identity_id : "none",
    status: binding.enabled ? binding.mode : "disabled",
  }));
  const leaseRecords = leases.map((lease) => ({
    ...lease,
    name: accountById.get(lease.account_id) || lease.account_id,
    role: lease.agent_id,
    status: new Date(lease.leased_until) > new Date() ? "active" : "expired",
  }));
  return (
    <main className="workbench-view">
      <ViewHeader description="Stable public identities represent the fleet; internal workers do not need individual channel accounts." eyebrow="Interoperability" title="Connections" />
      {communicationError ? <div className="inline-error">Communication registry unavailable: {communicationError}</div> : null}
      <div className="three-column-grid">
        <RecordSection label="Public identities" records={identityRecords} primary="display_name" secondary="role" state="status" />
        <RecordSection label="Channel accounts" records={accountRecords} primary="name" secondary="identity" state="status" />
        <RecordSection label="Representation" records={representationRecords} primary="name" secondary="identity" state="status" />
        <RecordSection label="Active gateway leases" records={leaseRecords} primary="name" secondary="role" state="status" />
      </div>
      <div className="three-column-grid">
        <section className="primary-surface protocol-card">
          <div className="surface-heading"><span className="surface-kicker">A2A Agent Card</span><span>{card?.protocolVersion || "unavailable"}</span></div>
          <h2>{card?.name || "MAC control plane"}</h2>
          <p>{card?.description || "Agent discovery is not available from this target."}</p>
          <Definition label="Endpoint" value={card?.url} />
          <Definition label="Streaming" value={card?.capabilities?.streaming} />
          <Definition label="Skills" value={card?.skills?.map((skill) => skill.name || skill.id)} />
        </section>
        <RecordSection label="Service links" records={data.service_links} primary="name" secondary="role" state="status" />
        <section className="primary-surface compact-surface">
          <div className="surface-heading"><span className="surface-kicker">Secret inventory</span><span>{data.secrets.length}</span></div>
          <p className="security-note"><i className="codicon codicon-shield-lock" /> Values remain redacted. Only scope and operational status are shown.</p>
          <RecordList records={data.secrets} primary="name" secondary="scope" state="enabled" />
        </section>
      </div>
    </main>
  );
}

function RecordSection({
  label,
  records,
  primary,
  secondary,
  state,
}: {
  label: string;
  records: Array<Record<string, unknown>>;
  primary: string;
  secondary: string;
  state: string;
}) {
  return (
    <section className="primary-surface compact-surface">
      <div className="surface-heading"><span className="surface-kicker">{label}</span><span>{records.length}</span></div>
      <RecordList primary={primary} records={records} secondary={secondary} state={state} />
    </section>
  );
}

function RecordList({
  records,
  primary,
  secondary,
  state,
}: {
  records: Array<Record<string, unknown>>;
  primary: string;
  secondary: string;
  state: string;
}) {
  if (!records.length) return <div className="empty-state"><span>No records</span></div>;
  return (
    <div className="record-list">
      {records.slice(0, 30).map((record, index) => (
        <div className="record-item" key={text(record.id || record.name, String(index))}>
          <span className={`state-dot state-${text(record[state], "open")}`} />
          <span className="record-title"><strong>{text(record[primary], text(record.id))}</strong><small>{text(record[secondary])}</small></span>
          <span className="record-state">{text(record[state])}</span>
        </div>
      ))}
    </div>
  );
}

function EventLines({ records }: { records: Array<Record<string, unknown>> }) {
  if (!records.length) return <div className="empty-state centered"><span>No events yet.</span></div>;
  return (
    <div className="event-lines">
      {records.map((record, index) => {
        const detail = record.detail as Record<string, unknown> | undefined;
        return (
          <div className="event-line" key={text(record.id || record.sequence, String(index))}>
            <time>{text(record.created_at || record.timestamp).slice(11, 23)}</time>
            <span className={`event-level level-${text(record.level, "info").toLowerCase()}`}>{text(record.level, "INFO").toUpperCase()}</span>
            <span className="event-name">{text(record.event_type || record.name || record.kind, "event")}</span>
            <span className="event-detail">{text(detail?.summary || record.subject_id || detail)}</span>
          </div>
        );
      })}
    </div>
  );
}

function Definition({ label, value }: { label: string; value: unknown }) {
  return <div className="definition"><span>{label}</span><strong>{text(value)}</strong></div>;
}

export function selectedTask(data: DashboardState, taskId: string | null): TaskDetail | null {
  return data.tasks.find((detail) => detail.task.id === taskId) || null;
}
