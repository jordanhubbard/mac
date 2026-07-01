import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Allotment } from "allotment";
import {
  api,
  bootstrapTokenFromUrl,
  clearToken,
  getApiBaseUrl,
  setApiBaseUrl,
  setToken,
  streamDashboard,
  type AgentCard,
  type DashboardState,
} from "./api/mac";
import { ActivityRail, type WorkbenchView } from "./components/ActivityRail";
import { AgentMesh } from "./components/AgentMesh";
import { BottomPanel } from "./components/BottomPanel";
import { WorkbenchExplorer } from "./components/WorkbenchExplorer";
import { selectedTask, WorkbenchViewContent } from "./components/WorkbenchViews";

const VIEWS = new Set<WorkbenchView>([
  "cockpit", "work", "workflows", "agents", "runtime", "observability", "connections",
]);

const EMPTY_STATE: DashboardState = {
  overview: { counts: {}, task_states: {}, agent_statuses: {} },
  project_summaries: [], agents: [], tasks: [], fleets: [], workflows: [], workflow_drafts: [],
  workflow_runs: {}, events: [], messages: [], notifications: [], observability: {}, action_events: [],
  command_audit: [], runtimes: [], runtime_deltas: [], runtime_runs: [], rollouts: [], secrets: [],
  secret_audits: [], service_links: [], integration_findings: [], artifacts: [], terminal_sessions: [],
};

function initialView(): WorkbenchView {
  const value = new URL(window.location.href).searchParams.get("view") as WorkbenchView | null;
  return value && VIEWS.has(value) ? value : "cockpit";
}

function initialSelection(): string | null {
  return new URL(window.location.href).searchParams.get("selected");
}

export function App() {
  const compactLayout = useCompactLayout();
  const [data, setData] = useState<DashboardState>(EMPTY_STATE);
  const [card, setCard] = useState<AgentCard | null>(null);
  const [view, setView] = useState<WorkbenchView>(initialView);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initialSelection);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [streamStatus, setStreamStatus] = useState<"connecting" | "connected" | "degraded">("connecting");
  const [needToken, setNeedToken] = useState(() => !bootstrapTokenFromUrl());
  const [tokenInput, setTokenInput] = useState("");
  const [targetInput, setTargetInput] = useState(getApiBaseUrl);
  const [tokenError, setTokenError] = useState("");
  const [commandQuery, setCommandQuery] = useState("");
  const detail = useMemo(() => selectedTask(data, selectedTaskId), [data, selectedTaskId]);
  const commandResults = useMemo(() => {
    const needle = commandQuery.trim().toLowerCase();
    if (!needle) return [];
    const taskResults = data.tasks
      .filter(({ task }) => [task.title, task.project, task.id].some((item) => String(item || "").toLowerCase().includes(needle)))
      .slice(0, 6)
      .map(({ task }) => ({ id: task.id, label: task.title || task.id, kind: "task" as const }));
    const agentResults = data.agents
      .map((item) => item.agent)
      .filter((agent) => [agent.name, agent.id, ...(agent.capabilities || [])].some((item) => String(item || "").toLowerCase().includes(needle)))
      .slice(0, 4)
      .map((agent) => ({ id: agent.id, label: agent.name || agent.id, kind: "agent" as const }));
    return [...taskResults, ...agentResults];
  }, [commandQuery, data.agents, data.tasks]);

  const refresh = useCallback(async () => {
    try {
      const [next, nextCard] = await Promise.all([
        api.dashboardState(),
        api.agentCard().catch(() => null),
      ]);
      setData(next);
      setCard(nextCard);
      setError("");
      setLoading(false);
    } catch (caught) {
      const message = String(caught instanceof Error ? caught.message : caught);
      setError(message);
      setLoading(false);
      if (/HTTP\s+(401|403)\b/.test(message)) {
        clearToken();
        setTokenError("The hub rejected this token. Paste the current fleet-scoped bearer token.");
        setNeedToken(true);
      }
    }
  }, []);

  useEffect(() => {
    if (needToken) return;
    void refresh();
    const interval = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(interval);
  }, [needToken, refresh]);

  useEffect(() => {
    if (needToken) return;
    let stopped = false;
    let controller: AbortController | null = null;
    let retryMs = 750;
    async function connect() {
      while (!stopped) {
        controller = new AbortController();
        setStreamStatus("connecting");
        try {
          await streamDashboard((signal) => {
            setStreamStatus("connected");
            retryMs = 750;
            if (signal.event === "updated") void refresh();
          }, controller.signal);
        } catch (caught) {
          if (stopped || controller.signal.aborted) break;
          setStreamStatus("degraded");
          setError(String(caught instanceof Error ? caught.message : caught));
        }
        if (stopped) break;
        await new Promise((resolve) => window.setTimeout(resolve, retryMs));
        retryMs = Math.min(retryMs * 2, 15_000);
      }
    }
    void connect();
    return () => {
      stopped = true;
      controller?.abort();
    };
  }, [needToken, refresh]);

  useEffect(() => {
    if (!selectedTaskId && data.tasks.length) {
      const active = data.tasks.find(({ task }) => !["completed", "cancelled", "failed"].includes(String(task.state)));
      setSelectedTaskId((active || data.tasks[0]).task.id);
    }
    if (!selectedAgentId && data.agents.length) setSelectedAgentId(data.agents[0].agent.id);
  }, [data.agents, data.tasks, selectedAgentId, selectedTaskId]);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    if (selectedTaskId) url.searchParams.set("selected", selectedTaskId);
    else url.searchParams.delete("selected");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
  }, [selectedTaskId, view]);

  function submitToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = setToken(tokenInput);
    setApiBaseUrl(targetInput);
    if (!token) {
      setTokenError("Enter a bearer token to connect.");
      return;
    }
    setTokenError("");
    setNeedToken(false);
    setLoading(true);
  }

  function disconnect() {
    clearToken();
    setTokenInput("");
    setTokenError("");
    setNeedToken(true);
  }

  if (needToken) {
    return (
      <div className="login-shell">
        <div className="login-brand"><BrandMark /><span>MAC Fleet Workbench</span></div>
        <form className="login-card" onSubmit={submitToken}>
          <span className="eyebrow">Control plane connection</span>
          <h1>Connect to a fleet hub</h1>
          <p>Use a read/write token for operator actions. Tokens live in this browser tab and URL bootstrap tokens are removed immediately.</p>
          <label>Hub URL<input aria-label="Hub URL" onChange={(event) => setTargetInput(event.target.value)} placeholder="/api or https://hub.example" value={targetInput} /></label>
          <label>Bearer token<input autoComplete="off" autoFocus aria-label="Hub bearer token" onChange={(event) => { setTokenInput(event.target.value); setTokenError(""); }} type="password" value={tokenInput} /></label>
          <button className="button primary" disabled={!tokenInput.trim()} type="submit">Connect to hub</button>
          {tokenError ? <p className="login-error" role="alert">{tokenError}</p> : null}
        </form>
      </div>
    );
  }

  const agents = data.agents.map((item) => item.agent);
  const busy = agents.filter((agent) => agent.current_task_id).length;
  return (
    <div className="workbench-shell">
      <header className="command-bar">
        <div className="brand"><BrandMark /><span>MAC Fleet Workbench</span></div>
        <div className="history-buttons"><button onClick={() => window.history.back()} type="button"><i className="codicon codicon-arrow-left" /></button><button onClick={() => window.history.forward()} type="button"><i className="codicon codicon-arrow-right" /></button></div>
        <div className="command-center">
          <label className="command-input">
            <i className="codicon codicon-search" />
            <input
              aria-label="Search tasks, agents, and commands"
              onChange={(event) => setCommandQuery(event.target.value)}
              placeholder="Search tasks, agents, commands…"
              value={commandQuery}
            />
            <kbd>⌘K</kbd>
          </label>
          {commandQuery ? (
            <div className="command-results">
              {commandResults.length ? commandResults.map((result) => (
                <button
                  key={`${result.kind}-${result.id}`}
                  onClick={() => {
                    if (result.kind === "task") {
                      setSelectedTaskId(result.id);
                      setView("work");
                    } else {
                      setSelectedAgentId(result.id);
                      setView("agents");
                    }
                    setCommandQuery("");
                  }}
                  type="button"
                >
                  <i className={`codicon codicon-${result.kind === "task" ? "issues" : "hubot"}`} />
                  <span>{result.label}</span>
                  <small>{result.kind}</small>
                </button>
              )) : <span className="command-empty">No matching tasks or agents</span>}
            </div>
          ) : null}
        </div>
        <div className="connection-summary">
          <span className={`presence ${error ? "offline" : "online"}`} />
          <span>{error ? "Hub degraded" : "Hub online"}</span>
          <span className="separator">·</span>
          <span>{agents.length} agents</span>
        </div>
      </header>

      <div className="workbench-body">
        <ActivityRail active={view} onChange={setView} />
        <div className="workbench-panes">
          <Allotment>
            {!compactLayout ? (
              <Allotment.Pane minSize={210} preferredSize={270}>
                <WorkbenchExplorer
                  activeView={view}
                  data={data}
                  onSelectAgent={setSelectedAgentId}
                  onSelectTask={setSelectedTaskId}
                  selectedAgentId={selectedAgentId}
                  selectedTaskId={selectedTaskId}
                />
              </Allotment.Pane>
            ) : null}
            <Allotment.Pane minSize={480}>
              <Allotment vertical>
                <Allotment.Pane minSize={320} preferredSize="70%">
                  <div className="editor-stack">
                    <div className="editor-tabs">
                      <div className="editor-tab active"><i className="codicon codicon-dashboard" /> {view === "cockpit" ? "Cockpit" : view[0].toUpperCase() + view.slice(1)} <i className="codicon codicon-close" /></div>
                      {detail ? <div className="editor-tab"><i className="codicon codicon-issues" /> {detail.task.title || detail.task.id}</div> : null}
                    </div>
                    {loading ? <div className="loading-state"><i className="codicon codicon-loading codicon-modifier-spin" /> Loading fleet state…</div> : (
                      <WorkbenchViewContent
                        card={card}
                        data={data}
                        onRefresh={refresh}
                        onSelectAgent={setSelectedAgentId}
                        onSelectTask={setSelectedTaskId}
                        selectedAgentId={selectedAgentId}
                        selectedTaskId={selectedTaskId}
                        view={view}
                      />
                    )}
                  </div>
                </Allotment.Pane>
                <Allotment.Pane minSize={130} preferredSize="30%">
                  <BottomPanel data={data} detail={detail} />
                </Allotment.Pane>
              </Allotment>
            </Allotment.Pane>
            {!compactLayout ? (
              <Allotment.Pane minSize={300} preferredSize={365}>
                <AgentMesh
                  card={card}
                  data={data}
                  onRefresh={refresh}
                  onSelectAgent={setSelectedAgentId}
                  selectedAgentId={selectedAgentId}
                  selectedTask={detail}
                />
              </Allotment.Pane>
            ) : null}
          </Allotment>
        </div>
      </div>

      <footer className="status-bar">
        <span><i className="codicon codicon-git-branch" /> main</span>
        <span><i className="codicon codicon-globe" /> primary hub</span>
        <span><i className={`codicon codicon-${streamStatus === "connected" ? "radio-tower" : "sync"}`} /> stream {streamStatus}</span>
        <span><i className="codicon codicon-shield" /> policy enforced</span>
        <span><i className="codicon codicon-symbol-structure" /> CodeGraph ready</span>
        <span className="status-spacer" />
        <span>{busy}/{agents.length} busy</span>
        <button onClick={disconnect} type="button">disconnect</button>
      </footer>
    </div>
  );
}

function BrandMark() {
  return <span className="brand-mark" aria-hidden="true"><span /><span /></span>;
}

function useCompactLayout(): boolean {
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 820px)").matches);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 820px)");
    const update = () => setCompact(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return compact;
}
