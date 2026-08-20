import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Allotment } from "allotment";
import {
  api,
  bootstrapTokenFromUrl,
  clearToken,
  getApiBaseUrl,
  hasManagedAuth,
  managedAuthLabel,
  setApiBaseUrl,
  setToken,
  streamDashboard,
  type AgentCard,
  type DashboardState,
  type TaskDetail,
} from "./api/mac";
import { ActivityRail, type WorkbenchView } from "./components/ActivityRail";
import { AgentMesh } from "./components/AgentMesh";
import { BottomPanel } from "./components/BottomPanel";
import { isPhysicalFleetAgent } from "./components/agentFacts";
import { projectFromUrl, pushProjectToUrl, replaceProjectInUrl } from "./components/projectScope";
import { WorkbenchExplorer } from "./components/WorkbenchExplorer";
import { selectedTask, WorkbenchViewContent } from "./components/WorkbenchViews";

const VIEWS = new Set<WorkbenchView>([
  "cockpit", "work", "task", "workflows", "agents", "runtime", "observability", "connections",
]);
const DASHBOARD_REFRESH_MIN_MS = 5_000;
const TASK_DETAIL_CACHE_LIMIT = 20;

const EMPTY_STATE: DashboardState = {
  overview: { counts: {}, task_states: {}, agent_statuses: {} },
  project_summaries: [], agents: [], tasks: [], fleets: [], workflows: [], workflow_drafts: [],
  workflow_runs: {}, events: [], messages: [], notifications: [], observability: {}, action_events: [],
  command_audit: [], runtimes: [], runtime_deltas: [], runtime_runs: [], rollouts: [], secrets: [],
  secret_audits: [], service_links: [], integration_findings: [], artifacts: [], agentbus_streams: [],
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
  const managedAuth = hasManagedAuth();
  const authLabel = managedAuthLabel();
  const [data, setData] = useState<DashboardState>(EMPTY_STATE);
  const [card, setCard] = useState<AgentCard | null>(null);
  const [view, setView] = useState<WorkbenchView>(initialView);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initialSelection);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(projectFromUrl);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [streamStatus, setStreamStatus] = useState<"connecting" | "connected" | "degraded">("connecting");
  const [needToken, setNeedToken] = useState(() => {
    const token = bootstrapTokenFromUrl();
    return !hasManagedAuth() && !token;
  });
  const [tokenInput, setTokenInput] = useState("");
  const [targetInput, setTargetInput] = useState(getApiBaseUrl);
  const [tokenError, setTokenError] = useState("");
  const [commandQuery, setCommandQuery] = useState("");
  const [selectedDetail, setSelectedDetail] = useState<TaskDetail | null>(null);
  const refreshInFlightRef = useRef<Promise<void> | null>(null);
  const refreshQueuedRef = useRef(false);
  const refreshTimerRef = useRef<number | null>(null);
  const refreshCompletedAtRef = useRef(0);
  const refreshRef = useRef<() => Promise<void>>(() => Promise.resolve());
  const cardRequestRef = useRef<Promise<AgentCard | null> | null>(null);
  const detailCacheRef = useRef(new Map<string, TaskDetail>());
  const detailRequestsRef = useRef(new Map<string, Promise<TaskDetail>>());
  const selectedTaskIdRef = useRef(selectedTaskId);
  selectedTaskIdRef.current = selectedTaskId;

  const physicalData = useMemo(
    () => ({ ...data, agents: data.agents.filter(isPhysicalFleetAgent) }),
    [data],
  );
  const viewData = useMemo(() => {
    if (!selectedDetail || selectedDetail.task.id !== selectedTaskId) return physicalData;
    return {
      ...physicalData,
      tasks: physicalData.tasks.map((summary) => summary.task.id === selectedDetail.task.id ? {
        ...selectedDetail,
        detail_loaded: true,
        task: { ...selectedDetail.task, ...summary.task },
      } : summary),
    };
  }, [physicalData, selectedDetail, selectedTaskId]);
  const detail = useMemo(
    () => selectedTask(viewData, selectedTaskId),
    [selectedTaskId, viewData],
  );
  const commandResults = useMemo(() => {
    const needle = commandQuery.trim().toLowerCase();
    if (!needle) return [];
    const taskResults = physicalData.tasks
      .filter(({ task }) => [task.title, task.project, task.id].some((item) => String(item || "").toLowerCase().includes(needle)))
      .slice(0, 6)
      .map(({ task }) => ({ id: task.id, label: task.title || task.id, kind: "task" as const }));
    const agentResults = physicalData.agents
      .map((item) => item.agent)
      .filter((agent) => [agent.name, agent.id, ...(agent.capabilities || [])].some((item) => String(item || "").toLowerCase().includes(needle)))
      .slice(0, 4)
      .map((agent) => ({ id: agent.id, label: agent.name || agent.id, kind: "agent" as const }));
    return [...taskResults, ...agentResults];
  }, [commandQuery, physicalData.agents, physicalData.tasks]);

  const fetchTaskDetail = useCallback((taskId: string, force = false): Promise<TaskDetail> => {
    if (!force) {
      const cached = detailCacheRef.current.get(taskId);
      if (cached) return Promise.resolve(cached);
      const active = detailRequestsRef.current.get(taskId);
      if (active) return active;
    }
    const request = api.getTask(taskId).then((next) => {
      const detail = { ...next, detail_loaded: true };
      const cache = detailCacheRef.current;
      cache.delete(taskId);
      cache.set(taskId, detail);
      while (cache.size > TASK_DETAIL_CACHE_LIMIT) {
        const oldest = cache.keys().next().value as string | undefined;
        if (!oldest) break;
        cache.delete(oldest);
      }
      return detail;
    });
    detailRequestsRef.current.set(taskId, request);
    const clearRequest = () => {
      if (detailRequestsRef.current.get(taskId) === request) {
        detailRequestsRef.current.delete(taskId);
      }
    };
    void request.then(clearRequest, clearRequest);
    return request;
  }, []);

  const performRefresh = useCallback(async () => {
    try {
      const next = await api.dashboardState();
      setData(next);
      setError("");
      setLoading(false);
    } catch (caught) {
      const message = String(caught instanceof Error ? caught.message : caught);
      setError(message);
      setLoading(false);
      if (/HTTP\s+(401|403)\b/.test(message)) {
        if (managedAuth) {
          setError("The active CLI login was rejected. Run `mac login renew`, then restart the Fleet IDE.");
        } else {
          clearToken();
          setTokenError("The hub rejected this token. Paste the current fleet-scoped bearer token.");
          setNeedToken(true);
        }
      }
    }
  }, [managedAuth]);

  const scheduleRefresh = useCallback(() => {
    refreshQueuedRef.current = true;
    if (refreshInFlightRef.current || refreshTimerRef.current !== null) return;
    const elapsed = Date.now() - refreshCompletedAtRef.current;
    const delay = Math.max(0, DASHBOARD_REFRESH_MIN_MS - elapsed);
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      refreshQueuedRef.current = false;
      void refreshRef.current();
    }, delay);
  }, []);

  const refresh = useCallback((): Promise<void> => {
    const active = refreshInFlightRef.current;
    if (active) return active;
    if (refreshTimerRef.current !== null) {
      window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
      refreshQueuedRef.current = false;
    }
    const request = performRefresh();
    refreshInFlightRef.current = request;
    void request.finally(() => {
      if (refreshInFlightRef.current !== request) return;
      refreshInFlightRef.current = null;
      refreshCompletedAtRef.current = Date.now();
      if (refreshQueuedRef.current) scheduleRefresh();
    });
    return request;
  }, [performRefresh, scheduleRefresh]);
  refreshRef.current = refresh;

  const refreshLatest = useCallback(async () => {
    if (refreshInFlightRef.current) scheduleRefresh();
    await refresh();
    const taskId = selectedTaskIdRef.current;
    if (!taskId) return;
    try {
      const next = await fetchTaskDetail(taskId, true);
      if (selectedTaskIdRef.current === taskId) setSelectedDetail(next);
    } catch {
      // The dashboard summary remains usable when optional task detail fails.
    }
  }, [fetchTaskDetail, refresh, scheduleRefresh]);

  useEffect(() => {
    if (needToken) return;
    void refresh();
    const interval = window.setInterval(scheduleRefresh, 30_000);
    return () => window.clearInterval(interval);
  }, [needToken, refresh, scheduleRefresh]);

  useEffect(() => {
    if (needToken) return;
    let active = true;
    if (!cardRequestRef.current) {
      cardRequestRef.current = api.agentCard().catch(() => null);
    }
    const request = cardRequestRef.current;
    void request.then((next) => {
      if (active) setCard(next);
    });
    return () => {
      active = false;
    };
  }, [needToken]);

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
            if (signal.event === "updated") scheduleRefresh();
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
  }, [needToken, scheduleRefresh]);

  useEffect(() => {
    if (!selectedTaskId || needToken) {
      setSelectedDetail(null);
      return;
    }
    let active = true;
    const cached = detailCacheRef.current.get(selectedTaskId) || null;
    setSelectedDetail(cached);
    void fetchTaskDetail(selectedTaskId).then((next) => {
      if (active && selectedTaskIdRef.current === selectedTaskId) {
        setSelectedDetail(next);
      }
    }).catch(() => {
      // A summary-only task remains selectable if detail hydration is unavailable.
    });
    return () => {
      active = false;
    };
  }, [fetchTaskDetail, needToken, selectedTaskId]);

  useEffect(() => () => {
    if (refreshTimerRef.current !== null) {
      window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!selectedTaskId && physicalData.tasks.length) {
      const active = physicalData.tasks.find(({ task }) => !["completed", "cancelled", "failed"].includes(String(task.state)));
      setSelectedTaskId((active || physicalData.tasks[0]).task.id);
    }
    const selectedAgentExists = physicalData.agents.some(
      (item) => item.agent.id === selectedAgentId,
    );
    if (!selectedAgentExists) {
      setSelectedAgentId(physicalData.agents[0]?.agent.id || null);
    }
  }, [physicalData.agents, physicalData.tasks, selectedAgentId, selectedTaskId]);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    if (selectedTaskId) url.searchParams.set("selected", selectedTaskId);
    else url.searchParams.delete("selected");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
  }, [selectedTaskId, view]);

  const handleSelectProject = useCallback((projectId: string | null) => {
    setSelectedProjectId(projectId);
    pushProjectToUrl(projectId);
  }, []);

  const handleSelectTask = useCallback((taskId: string) => {
    setSelectedTaskId(taskId);
    setView("task");
  }, []);

  // Restore project from URL on popstate (browser back/forward)
  useEffect(() => {
    function onPopState() {
      setSelectedProjectId(projectFromUrl());
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // Replace initial URL to include project param if restored from URL
  useEffect(() => {
    replaceProjectInUrl(selectedProjectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function submitToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = setToken(tokenInput);
    setApiBaseUrl(targetInput);
    if (!token) {
      setTokenError("Enter a bearer token to connect.");
      return;
    }
    cardRequestRef.current = null;
    detailCacheRef.current.clear();
    detailRequestsRef.current.clear();
    setCard(null);
    setSelectedDetail(null);
    setTokenError("");
    setNeedToken(false);
    setLoading(true);
  }

  function disconnect() {
    clearToken();
    cardRequestRef.current = null;
    detailCacheRef.current.clear();
    detailRequestsRef.current.clear();
    setCard(null);
    setSelectedDetail(null);
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
          <p>The normal connection is your active MAC CLI login. Run the command below, then restart the Fleet IDE.</p>
          <div className="login-recommended">
            <span>Recommended</span>
            <code>mac login</code>
            <small>The launcher reuses the scoped profile and SSH tunnel automatically.</small>
          </div>
          <div className="login-divider"><span>Manual fallback</span></div>
          <label>Hub URL<input aria-label="Hub URL" onChange={(event) => setTargetInput(event.target.value)} placeholder="/api or https://hub.example" value={targetInput} /></label>
          <label>Bearer token<input autoComplete="off" autoFocus aria-label="Hub bearer token" onChange={(event) => { setTokenInput(event.target.value); setTokenError(""); }} type="password" value={tokenInput} /></label>
          <button className="button primary" disabled={!tokenInput.trim()} type="submit">Connect to hub</button>
          {tokenError ? <p className="login-error" role="alert">{tokenError}</p> : null}
        </form>
      </div>
    );
  }

  const agents = viewData.agents.map((item) => item.agent);
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
                      handleSelectTask(result.id);
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
          <span>{error ? "Hub degraded" : loading ? "Hub loading" : "Hub online"}</span>
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
                  data={viewData}
                  onSelectAgent={setSelectedAgentId}
                  onSelectProject={handleSelectProject}
                  onSelectTask={handleSelectTask}
                  selectedAgentId={selectedAgentId}
                  selectedProjectId={selectedProjectId}
                  selectedTaskId={selectedTaskId}
                />
              </Allotment.Pane>
            ) : null}
            <Allotment.Pane minSize={480}>
              <Allotment vertical>
                <Allotment.Pane minSize={320} preferredSize="70%">
                  <div className="editor-stack">
                    <div className="editor-tabs">
                      {view !== "task" ? <div className="editor-tab active"><i className="codicon codicon-dashboard" /> {view === "cockpit" ? "Cockpit" : view[0].toUpperCase() + view.slice(1)} <i className="codicon codicon-close" /></div> : null}
                      {detail ? <button className={`editor-tab ${view === "task" ? "active" : ""}`} onClick={() => setView("task")} type="button"><i className="codicon codicon-issues" /> {detail.task.title || detail.task.id}</button> : null}
                    </div>
                    {loading ? <div className="loading-state"><i className="codicon codicon-loading codicon-modifier-spin" /> Loading fleet state…</div> : (
                      <WorkbenchViewContent
                        card={card}
                        data={viewData}
                        onRefresh={refreshLatest}
                        onSelectAgent={setSelectedAgentId}
                        onCloseTask={() => setView("work")}
                        onSelectTask={handleSelectTask}
                        selectedAgentId={selectedAgentId}
                        selectedProjectId={selectedProjectId}
                        selectedTaskId={selectedTaskId}
                        view={view}
                      />
                    )}
                  </div>
                </Allotment.Pane>
                <Allotment.Pane minSize={130} preferredSize="30%">
                  <BottomPanel data={viewData} detail={detail} />
                </Allotment.Pane>
              </Allotment>
            </Allotment.Pane>
            {!compactLayout ? (
              <Allotment.Pane minSize={300} preferredSize={365}>
                <AgentMesh
                  card={card}
                  data={viewData}
                  onRefresh={refreshLatest}
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
        {managedAuth ? (
          <span title="Authentication is supplied by the local MAC launcher"><i className="codicon codicon-lock" /> {authLabel}</span>
        ) : (
          <button onClick={disconnect} type="button">disconnect</button>
        )}
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
