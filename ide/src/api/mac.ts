// Typed client for the MAC hub. Development requests use Vite's /api proxy;
// packaged clients can point at a remote hub by setting mac.apiBaseUrl.

const DEFAULT_BASE = "/api";
const TOKEN_KEY = "mac.token";
const API_BASE_KEY = "mac.apiBaseUrl";
const REQUEST_TIMEOUT_MS = 30_000;

function runtimeEnv(): Record<string, string> {
  return (import.meta as ImportMeta & { env?: Record<string, string> }).env || {};
}

export interface ActivityEntry {
  phase: string;
  actor: string;
  summary: string;
  at: string;
}

export interface Task {
  id: string;
  title?: string;
  state?: string;
  project?: string;
  description?: string;
  priority?: number;
  owner_agent_id?: string | null;
  required_capabilities?: string[];
  dependencies?: string[];
  created_at?: string;
  updated_at?: string;
  metadata?: { activity?: ActivityEntry[]; [key: string]: unknown };
  [key: string]: unknown;
}

export interface TaskDetail {
  task: Task;
  detail_loaded?: boolean;
  evidence?: Array<Record<string, unknown>>;
  history?: Array<Record<string, unknown>>;
  reviews?: Array<Record<string, unknown>>;
  publications?: Array<Record<string, unknown>>;
}

export interface DispatchReason {
  code: string;
  message: string;
  detail?: Record<string, unknown>;
}

export interface TaskDispatchExplanation {
  task: Task;
  task_ready: boolean;
  dispatchable: boolean;
  eligible_agent_count: number;
  candidate_count: number;
  task_reasons: DispatchReason[];
  unclaimed_reasons: DispatchReason[];
  candidates: Array<{
    agent_id: string;
    agent_name: string;
    eligible: boolean;
    reasons: DispatchReason[];
  }>;
}

export interface Agent {
  id: string;
  name?: string;
  status?: string;
  health_status?: string;
  current_task_id?: string | null;
  capabilities?: string[];
  resources?: Record<string, unknown>;
  role_id?: string | null;
  last_seen_at?: string;
}

export interface DashboardAgent {
  agent: Agent;
  machine?: Record<string, unknown> | null;
  availability?: { eligible?: boolean; reasons?: string[] };
  active_tasks?: Task[];
  active_projects?: string[];
  active_lease_count?: number;
  capacity?: number;
}

export interface ProjectSummary {
  id?: string;
  name?: string;
  project?: string;
  status?: string;
  ready_count?: number;
  task_count?: number;
  active_task_count?: number;
  [key: string]: unknown;
}

export interface DashboardState {
  schema?: string;
  overview: {
    counts: Record<string, number>;
    task_states: Record<string, number>;
    agent_statuses: Record<string, number>;
  };
  project_summaries: ProjectSummary[];
  agents: DashboardAgent[];
  tasks: TaskDetail[];
  fleets: Array<Record<string, unknown>>;
  workflows: Array<Record<string, unknown>>;
  workflow_drafts: Array<Record<string, unknown>>;
  workflow_runs: Record<string, unknown>;
  events: Array<Record<string, unknown>>;
  messages: Array<Record<string, unknown>>;
  notifications: Array<Record<string, unknown>>;
  observability: Record<string, unknown>;
  action_events: Array<Record<string, unknown>>;
  command_audit: Array<Record<string, unknown>>;
  runtimes: Array<Record<string, unknown>>;
  runtime_deltas: Array<Record<string, unknown>>;
  runtime_runs: Array<Record<string, unknown>>;
  rollouts: Array<Record<string, unknown>>;
  secrets: Array<Record<string, unknown>>;
  secret_audits: Array<Record<string, unknown>>;
  service_links: Array<Record<string, unknown>>;
  integration_findings: Array<Record<string, unknown>>;
  artifacts: Array<Record<string, unknown>>;
  /**
   * Conversations open on AgentBus. This replaced `terminal_sessions`, which
   * outlived the HTTP routes that could create one: the field kept arriving,
   * always empty, and the Terminal tab rendered a panel that could never be
   * non-empty — a screen that looks functional and is not.
   */
  agentbus_streams: Array<Record<string, unknown>>;
  server_time?: string;
  updated_at?: string;
  session?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AgentCard {
  name: string;
  description?: string;
  protocolVersion?: string;
  url?: string;
  version?: string;
  capabilities?: Record<string, unknown>;
  skills?: Array<{
    id?: string;
    name?: string;
    description?: string;
    tags?: string[];
    examples?: string[];
  }>;
  [key: string]: unknown;
}

export interface CommunicationIdentity {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  is_default: boolean;
  enabled: boolean;
  metadata?: Record<string, unknown>;
}

export interface CommunicationAccount {
  id: string;
  identity_id: string;
  channel: string;
  account_id: string;
  enabled: boolean;
  credential_refs?: Record<string, unknown>;
  config?: Record<string, unknown>;
}

export interface RepresentationBinding {
  id: string;
  subject_kind: string;
  subject_id: string;
  identity_id?: string | null;
  mode: "direct" | "delegated" | "internal_only";
  enabled: boolean;
}

export interface GatewayIdentityLease {
  id: string;
  account_id: string;
  agent_id: string;
  leased_until: string;
}

export interface TaskCreatePayload {
  title: string;
  description: string;
  project?: string;
  priority?: number;
  required_capabilities?: string[];
  dependencies?: string[];
  metadata?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface TaskUpdatePayload {
  actor?: string;
  description?: string;
  metadata?: Record<string, unknown>;
}

interface A2AResponse<T> {
  jsonrpc: "2.0";
  id: string;
  result?: T;
  error?: { code?: number; message?: string; data?: unknown };
}

export function normalizeTokenInput(raw: string): string {
  let token = raw.trim();
  token = token.replace(/^authorization:\s*/i, "").trim();
  token = token.replace(/^bearer\s+/i, "").trim();
  return token;
}

export function getToken(): string {
  return (
    sessionStorage.getItem(TOKEN_KEY) ||
    localStorage.getItem(TOKEN_KEY) ||
    runtimeEnv().VITE_MAC_TOKEN ||
    ""
  );
}

export function hasManagedAuth(): boolean {
  return runtimeEnv().VITE_MAC_AUTH_MODE === "managed";
}

export function managedAuthLabel(): string {
  return runtimeEnv().VITE_MAC_AUTH_LABEL || "CLI profile";
}

export function setToken(raw: string): string {
  const token = normalizeTokenInput(raw);
  localStorage.removeItem(TOKEN_KEY);
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
  return token;
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_KEY);
}

export function bootstrapTokenFromUrl(): string {
  const url = new URL(window.location.href);
  const raw = url.searchParams.get("t") || "";
  if (!raw) return getToken();
  const token = setToken(raw);
  url.searchParams.delete("t");
  window.history.replaceState({}, "", url.pathname + url.search + url.hash);
  return token;
}

export function getApiBaseUrl(): string {
  return (localStorage.getItem(API_BASE_KEY) || DEFAULT_BASE).replace(/\/$/, "");
}

export function setApiBaseUrl(raw: string): string {
  const base = raw.trim().replace(/\/$/, "") || DEFAULT_BASE;
  if (base === DEFAULT_BASE) localStorage.removeItem(API_BASE_KEY);
  else localStorage.setItem(API_BASE_KEY, base);
  return base;
}

function requestUrl(path: string): string {
  return getApiBaseUrl() + (path.startsWith("/") ? path : `/${path}`);
}

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(requestUrl(path), {
      method,
      headers: requestHeaders(),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`HTTP ${response.status} ${path}: ${text.slice(0, 300)}`);
    }
    return (await response.json()) as T;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error(`Request timed out after ${REQUEST_TIMEOUT_MS / 1000}s: ${method} ${path}`);
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

export async function streamDashboard(
  onSignal: (signal: Record<string, unknown>) => void,
  abortSignal: AbortSignal,
): Promise<void> {
  const response = await fetch(
    requestUrl("/dashboard/stream?timeout_seconds=25&poll_interval_seconds=1"),
    { headers: requestHeaders(), signal: abortSignal },
  );
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status} /dashboard/stream`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  while (!abortSignal.aborted) {
    const { done, value } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    const lines = buffered.split("\n");
    buffered = lines.pop() || "";
    for (const line of lines) {
      if (line.trim()) onSignal(JSON.parse(line) as Record<string, unknown>);
    }
  }
}

async function a2aCall<T>(method: string, params: Record<string, unknown>): Promise<T> {
  const id = `ide-${Date.now().toString(36)}`;
  const response = await req<A2AResponse<T>>("POST", "/a2a", {
    jsonrpc: "2.0",
    id,
    method,
    params,
  });
  if (response.error) {
    throw new Error(`A2A ${response.error.code ?? "error"}: ${response.error.message ?? "request failed"}`);
  }
  if (response.result === undefined) throw new Error("A2A response did not include a result");
  return response.result;
}

export interface TaskGroup {
  id: string;
  name: string;
  expression: string;
  description: string;
}

export interface TaskSelectionEntry {
  id: string;
  title: string;
  project: string | null;
  state: string;
  priority: number;
  questions: string[];
}

export interface TaskSelection {
  matched: number;
  /** Identifies WHICH tasks matched, not how many: a batch is refused if this
   *  changes between preview and apply, which a count cannot detect. */
  token: string;
  returned: number;
  truncated: boolean;
  tasks: TaskSelectionEntry[];
}

export interface TaskBatchOutcome {
  batch_id: string;
  selection_token: string;
  operation: string;
  selector: string;
  applied: boolean;
  matched: number;
  changed: string[];
  changed_count: number;
  failed: { id: string; error: string }[];
  failed_count: number;
  truncated: boolean;
}

export const api = {
  dashboardState: () => req<DashboardState>("GET", "/dashboard/state?view=ide"),
  listTasks: (state?: string) =>
    req<Task[]>("GET", `/tasks${state ? `?state=${encodeURIComponent(state)}` : ""}`),
  getTask: (id: string) => req<TaskDetail>("GET", `/tasks/${encodeURIComponent(id)}?view=compact`),
  explainTaskDispatch: (id: string) =>
    req<TaskDispatchExplanation>("GET", `/tasks/${encodeURIComponent(id)}/dispatch-explain`),
  listAgents: () => req<Agent[]>("GET", "/agents"),
  listCommunicationIdentities: () =>
    req<CommunicationIdentity[]>("GET", "/communication/identities"),
  listCommunicationAccounts: () =>
    req<CommunicationAccount[]>("GET", "/communication/accounts"),
  listRepresentationBindings: () =>
    req<RepresentationBinding[]>("GET", "/communication/representations"),
  listGatewayIdentityLeases: () =>
    req<GatewayIdentityLease[]>("GET", "/communication/gateway-leases?active_only=true"),
  createTask: (payload: TaskCreatePayload) => req<Task>("POST", "/tasks", payload),
  updateTask: (taskId: string, payload: TaskUpdatePayload) =>
    req<Task>("PUT", `/tasks/${encodeURIComponent(taskId)}`, payload),
  // --- task groups -------------------------------------------------------
  // A group is a selector expression, evaluated fresh on every call. The UI
  // never caches membership: a stale list is how a bulk action reaches tasks
  // the operator never saw.
  selectTasks: (selector: string, limit?: number, sample = 50) =>
    req<TaskSelection>("POST", "/tasks/select", { selector, limit, sample }),
  applyTaskBatch: (
    selector: string,
    operation: string,
    options: Record<string, unknown> = {},
    apply = false,
    expectToken?: string,
  ) =>
    req<TaskBatchOutcome>("POST", "/tasks/batch", {
      selector,
      operation,
      apply,
      actor: "human",
      expect_token: expectToken,
      options,
    }),
  listTaskGroups: () => req<TaskGroup[]>("GET", "/task-groups"),
  saveTaskGroup: (name: string, selector: string, description = "") =>
    req<TaskGroup>("POST", "/task-groups", {
      name,
      selector,
      description,
      actor: "human",
    }),
  deleteTaskGroup: (name: string) =>
    req<{ name: string; deleted: boolean }>(
      "DELETE",
      `/task-groups/${encodeURIComponent(name)}`,
    ),
  answerTaskInput: (taskId: string, answer: string) =>
    req<Task>("POST", `/tasks/${encodeURIComponent(taskId)}/answer`, {
      actor: "human",
      answer,
    }),
  askTaskInput: (taskId: string, questions: string[], why = "") =>
    req<Task>("POST", `/tasks/${encodeURIComponent(taskId)}/ask`, {
      actor: "human",
      questions: questions.map((question) => ({ question })),
      why,
    }),
  reopenTask: (taskId: string, reason: string) =>
    req<Task>("POST", `/tasks/${encodeURIComponent(taskId)}/reopen`, {
      actor: "human",
      reason,
    }),
  claimTask: (taskId: string, agentId: string) =>
    req<Record<string, unknown>>(
      "POST",
      `/tasks/${encodeURIComponent(taskId)}/claim?agent_id=${encodeURIComponent(agentId)}`,
    ),
  summary: (id: string) => req<TaskDetail>("GET", `/tasks/${encodeURIComponent(id)}`),
  requestReview: (taskId: string, reviewerAgentId: string) =>
    req<Record<string, unknown>>("POST", `/tasks/${encodeURIComponent(taskId)}/reviews`, {
      reviewer_agent_id: reviewerAgentId,
      actor: "human",
    }),
  workflowPlanPreview: (prompt: string, context: Record<string, unknown> = {}) =>
    req<Record<string, unknown>>("POST", "/dashboard/workflow-plan/preview", {
      goal: prompt,
      prompt,
      context,
    }),
  workflowPlanAccept: (draft: Record<string, unknown>) =>
    req<Record<string, unknown>>("POST", "/dashboard/workflow-plan/accept", draft),
  cancelWorkflowRun: (runId: string, reason = "Cancelled from Fleet Workbench") =>
    req<Record<string, unknown>>("POST", `/workflows/runs/${encodeURIComponent(runId)}/cancel`, {
      reason,
      actor: "human",
    }),
  agentCard: () => req<AgentCard>("GET", "/.well-known/agent-card.json"),
  sendA2AMessage: (text: string, contextId = "mac-fleet-workbench") =>
    a2aCall<Record<string, unknown>>("message/send", {
      message: {
        kind: "message",
        role: "user",
        messageId: `msg-${Date.now().toString(36)}`,
        contextId,
        parts: [{ kind: "text", text }],
      },
    }),
  getA2ATask: (taskId: string) => a2aCall<Record<string, unknown>>("tasks/get", { id: taskId }),
};
