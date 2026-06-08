// Maintained dashboard source. The browser module is checked in as app.js so
// mac does not require Node.js/npm to serve or install the UI.
// Regenerate with:
//   cd src/mac/ui && npx -y -p typescript@5 tsc --target es2021 --module es2020 \
//     --lib dom,es2021 --strict false --skipLibCheck --outDir /tmp/uib app.ts
//   cp /tmp/uib/app.js app.js
import { createDashboardApi, normalizeApiBaseUrl, type DashboardConnection, type DashboardTarget } from "./dashboard_api.js";

type ViewKey =
  | "overview"
  | "work"
  | "projects"
  | "map"
  | "fleets"
  | "agents"
  | "tasks"
  | "workflows"
  | "hermes"
  | "ops"
  | "integrations"
  | "runtime"
  | "observability"
  | "secrets";
type Tone = "good" | "warn" | "bad" | "info";
type JsonObject = Record<string, unknown>;

interface ApiRecord {
  id: string;
  [key: string]: unknown;
}

interface AgentRecord extends ApiRecord {
  name: string;
  machine_id: string;
  role_id?: string | null;
  capabilities?: string[];
  resources?: JsonObject;
  status: string;
  health_status: string;
  current_task_id?: string | null;
  last_seen_at?: string;
  hermes_instance_id?: string | null;
}

interface MachineRecord extends ApiRecord {
  hostname: string;
  trusted: boolean;
  labels?: JsonObject;
  resources?: JsonObject;
}

interface FleetRecord extends ApiRecord {
  name: string;
  description?: string;
  status: string;
  metadata?: JsonObject;
  tenant_id?: string | null;
  agent_ids?: string[];
  observed_agent_ids?: string[];
  unmanaged_agent_ids?: string[];
}

interface TaskRecord extends ApiRecord {
  title: string;
  description?: string;
  state: string;
  project?: string | null;
  priority?: number;
  required_capabilities?: string[];
  dependencies?: string[];
  metadata?: JsonObject;
  owner_agent_id?: string | null;
  leased_until?: string | null;
  attempt_count?: number;
  max_attempts?: number;
  started_at?: string | null;
  completed_at?: string | null;
  last_updated_at?: string | null;
  updated_at?: string | null;
}

interface TaskDetail {
  task: TaskRecord;
  history: ApiRecord[];
  evidence: ApiRecord[];
  reviews: ApiRecord[];
  publications: ApiRecord[];
  summary?: JsonObject;
}

interface AgentItem {
  agent: AgentRecord;
  machine: MachineRecord | null;
  active_tasks: TaskRecord[];
  active_projects?: string[];
  capacity: number;
  active_lease_count: number;
  availability: { eligible: boolean; reasons: string[] };
}

interface DispatchCandidate {
  agent_id: string;
  agent_name: string;
  eligible: boolean;
  reasons: string[];
}

interface DispatchTask {
  task: TaskRecord;
  tenant_id?: string | null;
  eligible_agent_count: number;
  candidate_count?: number;
  candidate_limit?: number;
  candidate_truncated?: boolean;
  candidates: DispatchCandidate[];
}

interface ProjectSummary {
  project: string;
  task_count: number;
  active_count: number;
  ready_count: number;
  blocked_count: number;
  review_count: number;
  completed_count: number;
  state_counts: Record<string, number>;
  dependency_edge_count: number;
  cross_project_dependency_count: number;
  active_agent_ids: string[];
  active_agent_names: string[];
  required_capabilities: string[];
  frontier_tasks: TaskRecord[];
  waiting_tasks: Array<TaskRecord & { waiting_on?: string[] }>;
  active_tasks: TaskRecord[];
  cross_project_edges: JsonObject[];
  bridge_item_count: number;
  repository_count: number;
  description?: string;
  status?: string;
  metadata?: JsonObject;
  project_id?: string | null;
  record?: JsonObject | null;
}

interface HermesWorkContext {
  schema: string;
  authority: Record<string, string>;
  hermes_instance?: JsonObject;
  fleets?: FleetRecord[];
  projects: ProjectSummary[];
  tasks: Array<TaskRecord & { project?: string; origin?: JsonObject }>;
  task_count: number;
  task_limit: number;
  task_truncated: boolean;
  agents: Array<{
    id: string;
    name: string;
    status: string;
    health_status: string;
    active_task_ids: string[];
    active_projects: string[];
    hermes_instance_id?: string | null;
  }>;
  relationships: {
    task_dependencies?: JsonObject[];
    agent_assignments?: JsonObject[];
    hermes_task_origins?: JsonObject[];
  };
  operations: {
    api?: JsonObject[];
    mac_cli?: string[];
    mac_hermes_cli?: string[];
    hgmac_cli?: string[];
    task_state_transitions?: Record<string, string[]>;
  };
}

interface HermesRuntimeProof {
  schema: string;
  ready: boolean;
  checks?: Record<string, boolean>;
  missing?: string[];
  evidence?: JsonObject;
}

interface SwarmSummary {
  agent_total: number;
  status: Array<{ key: string; count: number }>;
  health: Array<{ key: string; count: number }>;
  role: Array<{ key: string; count: number }>;
  project: Array<{ key: string; count: number }>;
  capability: Array<{ key: string; count: number }>;
  machine: Array<{ key: string; count: number }>;
}

interface RolloutStatus {
  rollout: ApiRecord;
  runtime: ApiRecord | null;
  events: ApiRecord[];
  latest_eval_run: ApiRecord | null;
}

interface HermesStartup {
  ready?: boolean;
  warnings?: string[];
  operator_health?: {
    status?: string;
    state_refs_existing?: number;
    slack_activation_source?: string;
    secret_redaction_effective?: boolean;
    log_actionable_count?: number;
    task_project_runtime_status?: string;
    task_project_runtime_ready?: boolean;
    task_project_runtime_present?: boolean;
    task_project_runtime_hermes_instance_id?: string;
  };
  security?: JsonObject;
  slack?: JsonObject;
  logs?: JsonObject;
  task_project_runtime?: JsonObject;
}

interface HermesConfigField {
  key: string;
  value?: unknown;
  default?: unknown;
  type?: string;
  source?: string;
  desired?: boolean;
}

interface HermesEnvVar {
  name: string;
  description?: string;
  prompt?: string;
  category?: string;
  required?: boolean;
  password?: boolean;
  present?: boolean;
  desired?: boolean;
  source?: string;
  redacted_value?: string;
  url?: string | null;
}

interface HermesPluginRecord {
  key: string;
  name: string;
  label?: string;
  kind?: string;
  source?: string;
  state?: string;
  state_source?: string;
  description?: string;
  requires_env?: unknown[];
  optional_env?: unknown[];
  provides_tools?: string[];
  provides_hooks?: string[];
}

interface HermesSkillRecord {
  name: string;
  key: string;
  category?: string;
  source?: string;
  state?: string;
  state_source?: string;
  enabled?: boolean;
  description?: string;
  tags?: unknown[];
  triggers?: unknown[];
  platforms?: unknown[];
  required_environment_variables?: unknown[];
}

interface HermesConfigSurface {
  schema: string;
  fleet_id: string;
  fleet_name: string;
  registry_key?: string;
  registry_path?: string;
  hermes_home?: string;
  config_path?: string;
  env_path?: string;
  runtime?: Record<string, unknown>;
  agent_count: number;
  agents: Array<{ id?: string; name?: string; hermes_instance_id?: string | null }>;
  agent_overrides?: Array<{ name: string; keys: string[] }>;
  config_fields: HermesConfigField[];
  env_vars: HermesEnvVar[];
  plugins: HermesPluginRecord[];
  skills: HermesSkillRecord[];
  desired?: JsonObject;
  updated_at?: string;
}

interface ObservabilityEvent extends ApiRecord {
  sequence: number;
  kind: string;
  layer: string;
  source: string;
  level: string;
  name: string;
  subject_type?: string | null;
  subject_id?: string | null;
  value?: number | null;
  unit?: string;
  detail?: JsonObject;
  created_at: string;
}

interface AuditEvent extends ApiRecord {
  subject_type: string;
  subject_id: string;
  event_type: string;
  actor: string;
  detail?: JsonObject;
  created_at: string;
}

interface ObservabilitySummary {
  counts: Record<string, number>;
  levels: Record<string, number>;
  layers: Record<string, number>;
  latest: ObservabilityEvent[];
  latest_metrics: ObservabilityEvent[];
}

interface CommandAuditRecord extends ApiRecord {
  command_id: string;
  agent_id: string;
  phase: string;
  argv: string[];
  cwd: string;
  task_id?: string | null;
  lease_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  returncode?: number | null;
  stdout_sha256?: string | null;
  stderr_sha256?: string | null;
  stdout_bytes?: number | null;
  stderr_bytes?: number | null;
  metadata?: JsonObject;
  created_at: string;
}

interface OperatorNotification extends ApiRecord {
  event_type: string;
  subject_type?: string | null;
  subject_id?: string | null;
  title: string;
  body: string;
  channels?: string[];
  metadata?: JsonObject;
  status: string;
  created_at: string;
  delivered_at?: string | null;
}

interface WorkflowDraftRecord extends ApiRecord {
  tenant_id?: string | null;
  goal: string;
  status: string;
  proposed_steps?: JsonObject[];
  questions?: JsonObject[];
  answers?: JsonObject;
  compiled_workflow_id?: string | null;
  updated_at?: string;
}

interface NotifierChannelRecord extends ApiRecord {
  name: string;
  channel_type: string;
  enabled: boolean;
  event_types?: string[];
  target?: JsonObject;
  updated_at?: string;
}

interface IntegrationFinding extends ApiRecord {
  source_id: string;
  source_kind: string;
  finding_type: string;
  severity: string;
  status: string;
  title: string;
  detail?: JsonObject;
  fingerprint: string;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at?: string | null;
  resolution?: string | null;
}

interface IntegrationObservation extends ApiRecord {
  source_id: string;
  source_kind: string;
  authority: string;
  status: string;
  fingerprint?: string | null;
  cursor?: string | null;
  detail?: JsonObject;
  observed_at: string;
}

interface ServiceCredentialRef {
  name: string;
  source: string;
  present: boolean;
  redacted_value?: string;
}

interface ServiceLinkRecord extends ApiRecord {
  name: string;
  kind: string;
  role: string;
  status: string;
  url?: string;
  ui_url?: string;
  health_url?: string;
  auth?: {
    type?: string;
    credential_pass_through?: boolean;
    pass_through_url?: string;
    notes?: string;
  };
  credentials?: ServiceCredentialRef[];
}

interface DashboardSession {
  scopes: string[];
  tenant_id?: string | null;
  agent_id?: string | null;
  is_admin: boolean;
  can_read: boolean;
  can_write: boolean;
  mode: string;
}

interface DashboardData {
  server_time?: string;
  updated_at?: string;
  overview: {
    counts: Record<string, number>;
    task_states: Record<string, number>;
    agent_statuses: Record<string, number>;
  };
  project_summaries: ProjectSummary[];
  swarm_summary: SwarmSummary;
  tenants: ApiRecord[];
  users: ApiRecord[];
  personas: ApiRecord[];
  hermes_instances: ApiRecord[];
  hermes_work_contexts: Record<string, HermesWorkContext>;
  hermes_runtime_proofs: Record<string, HermesRuntimeProof>;
  hermes_config_surfaces: HermesConfigSurface[];
  platform_bindings: ApiRecord[];
  roles: ApiRecord[];
  provisioning_requests: ApiRecord[];
  machines: MachineRecord[];
  fleets: FleetRecord[];
  agents: AgentItem[];
  tasks: TaskDetail[];
  dead_letters: TaskRecord[];
  dispatch: { open_task_count: number; tasks: DispatchTask[] };
  messages: ApiRecord[];
  notifications: OperatorNotification[];
  notifier_channels: NotifierChannelRecord[];
  workflows: ApiRecord[];
  workflow_drafts: WorkflowDraftRecord[];
  workflow_runs: { counts?: Record<string, number>; total?: number; latest?: ApiRecord[] };
  agentbus_streams: ApiRecord[];
  artifacts: ApiRecord[];
  bridge_items: ApiRecord[];
  beads_repositories: ApiRecord[];
  memory_records: ApiRecord[];
  nap_schedules: ApiRecord[];
  nap_runs: ApiRecord[];
  integration_findings: IntegrationFinding[];
  integration_observations: IntegrationObservation[];
  service_links: ServiceLinkRecord[];
  events: AuditEvent[];
  command_audit: CommandAuditRecord[];
  secrets: ApiRecord[];
  secret_audits: ApiRecord[];
  runtimes: ApiRecord[];
  runtime_deltas: ApiRecord[];
  runtime_runs: ApiRecord[];
  rollouts: RolloutStatus[];
  eval_sets: ApiRecord[];
  eval_runs: ApiRecord[];
  observability: ObservabilitySummary;
  hermes_startup?: HermesStartup | null;
  session?: DashboardSession;
}

interface DashboardStreamEvent {
  event?: string;
  server_time?: string;
  updated_at?: string;
  observability_sequence?: number;
}

interface WorkflowPlanNode {
  node_id: string;
  title: string;
  description: string;
  required_capabilities: string[];
  depends_on: string[];
  priority: number;
  metadata?: JsonObject;
}

interface WorkflowPlanDraft {
  schema?: string;
  plan_id: string;
  goal: string;
  project?: string | null;
  source?: string;
  created_at?: string;
  nodes: WorkflowPlanNode[];
}

interface DashboardState {
  activeView: ViewKey;
  token: string;
  apiBaseUrl: string;
  connection: DashboardConnection;
  loading: boolean;
  loadedAt: Date | null;
  data: DashboardData | null;
  error: string | null;
  actionMessage: string | null;
  agentQuery: string;
  agentFilter: string;
  agentSort: string;
  agentPage: number;
  projectFilter: string;
  showDerivedProjects: boolean;
  taskFilter: string;
  selectedId: string;
  targets: DashboardTarget[];
  selectedTargetId: string;
  selectedTokenSourceId: string;
  auditSubjectType: string;
  auditSubjectId: string;
  auditEventPrefix: string;
  auditActor: string;
  auditLayer: string;
  auditLevel: string;
  auditAgentId: string;
  auditTaskId: string;
  auditProject: string;
  auditFleet: string;
  auditSince: string;
  auditUntil: string;
  observabilityLive: ObservabilityEvent[];
  dashboardStream: AbortController | null;
  dashboardStreamStatus: string;
  observabilityStream: AbortController | null;
  observabilityStreamStatus: string;
  // wf-05: which workflow's graph + drafts are shown on the Workflows tab,
  // and which node (if any) is open in the inspector panel.
  selectedWorkflowId: string;
  selectedNodeKey: string;
  workflowPlanDraft: WorkflowPlanDraft | null;
}

interface DashboardNodes {
  nav: HTMLElement;
  title: HTMLElement;
  viewSelect: HTMLSelectElement;
  banner: HTMLElement;
  content: HTMLElement;
  refresh: HTMLButtonElement;
  syncState: HTMLElement;
  connectionForm: HTMLFormElement;
  topbarTargetSelect: HTMLSelectElement;
  tokenSourceSelect: HTMLSelectElement;
  topbarTokenInput: HTMLInputElement;
  topbarTokenField: HTMLElement;
  topbarTestingUrlInput: HTMLInputElement;
  topbarTestingUrlField: HTMLElement;
  connectionButton: HTMLButtonElement;
  tokenForm: HTMLFormElement;
  targetSelect: HTMLSelectElement;
  tokenInput: HTMLInputElement;
  apiUrlInput: HTMLInputElement;
  clearToken: HTMLButtonElement;
  loginScreen: HTMLElement;
  loginForm: HTMLFormElement;
  loginTargetSelect: HTMLSelectElement;
  loginTokenInput: HTMLInputElement;
  loginApiUrlInput: HTMLInputElement;
  serviceLinks: HTMLElement;
  connectionBadge: HTMLElement;
  themeToggle: HTMLButtonElement;
}

const TOKEN_KEY = "mac.dashboard.token";
const API_BASE_URL_KEY = "mac.dashboard.apiBaseUrl";
const TARGET_KEY = "mac.dashboard.targetId";
const TOKEN_SOURCE_KEY = "mac.dashboard.tokenSourceId";
const THEME_KEY = "mac.dashboard.theme";
type ThemeName = "light" | "dark";

function readStoredTheme(): ThemeName {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(THEME_KEY);
  } catch {
    stored = null;
  }
  if (stored === "light" || stored === "dark") return stored;
  if (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  ) {
    return "dark";
  }
  return "light";
}

function applyTheme(theme: ThemeName): void {
  document.documentElement.setAttribute("data-theme", theme);
  const toggle = document.querySelector<HTMLButtonElement>("#themeToggle");
  if (toggle) {
    const isDark = theme === "dark";
    toggle.setAttribute("aria-pressed", String(isDark));
    const label = toggle.querySelector<HTMLElement>(".theme-toggle-label");
    if (label) label.textContent = isDark ? "Light" : "Dark";
  }
}

function setTheme(theme: ThemeName): void {
  applyTheme(theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* storage unavailable — theme still applies for this session */
  }
}

function toggleTheme(): void {
  const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  setTheme(current === "dark" ? "light" : "dark");
}
const TASK_STATES = [
  "open",
  "blocked",
  "claimed",
  "running",
  "needs_review",
  "reviewing",
  "completed",
  "failed",
  "cancelled",
];
const TERMINAL_TASK_STATES = new Set(["completed", "failed", "cancelled"]);
const AUDIT_SUBJECT_TYPES = [
  "",
  "task",
  "agent",
  "project",
  "fleet",
  "rollout",
  "eval_set",
  "secret",
  "environment",
  "conversation_thread",
  "vector_ref",
];
const OBSERVABILITY_LEVELS = ["", "debug", "info", "warning", "error", "critical"];
const AGENT_PAGE_SIZE = 50;
const VIEW_TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  work: "Work",
  projects: "Projects",
  map: "Map",
  fleets: "Fleets",
  agents: "Agents",
  tasks: "Tasks",
  workflows: "Workflows",
  hermes: "Hermes",
  ops: "Operations",
  integrations: "Integrations",
  runtime: "Runtime",
  observability: "Observability",
  secrets: "Secrets",
};
const VIEW_KEYS = new Set(Object.keys(VIEW_TITLES));
const DESTRUCTIVE_ACTION_LABELS: Record<string, string> = {
  Project: "Delete this project?",
  Agent: "Delete this agent?",
  Task: "Delete this task?",
  Secret: "Delete this secret?",
};
const DEFAULT_URL_STATE = readUrlState();

function readStoredApiBaseUrl(): string {
  const configUrl = normalizeApiBaseUrl(window.MAC_DASHBOARD_CONFIG?.apiBaseUrl || "");
  if (configUrl) return configUrl;
  try {
    return normalizeApiBaseUrl(sessionStorage.getItem(API_BASE_URL_KEY) || "");
  } catch {
    return "";
  }
}

function readStoredTargetId(): string {
  try {
    return sessionStorage.getItem(TARGET_KEY) || "";
  } catch {
    return "";
  }
}

function readStoredTokenSourceId(): string {
  try {
    return sessionStorage.getItem(TOKEN_SOURCE_KEY) || "";
  } catch {
    return "";
  }
}

// Bootstrap token from ?t=<token> URL param (e.g. from a fresh deploy link).
// Stored into sessionStorage and then stripped from the URL so it doesn't
// linger in browser history.
(function bootstrapConnectionFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const t = params.get("t");
  if (t) {
    sessionStorage.setItem(TOKEN_KEY, t);
    params.delete("t");
  }
  const apiBaseUrl = normalizeApiBaseUrl(params.get("api") || params.get("u") || "");
  if (apiBaseUrl) {
    sessionStorage.setItem(API_BASE_URL_KEY, apiBaseUrl);
    params.delete("api");
    params.delete("u");
  }
  if (t || apiBaseUrl) {
    const newSearch = params.toString();
    const newUrl = window.location.pathname + (newSearch ? "?" + newSearch : "") + window.location.hash;
    history.replaceState(null, "", newUrl);
  }
})();

function connectionSnapshot(apiBaseUrl: string): DashboardConnection {
  const normalized = normalizeApiBaseUrl(apiBaseUrl);
  const config = window.MAC_DASHBOARD_CONFIG || {};
  return {
    mode: window.macDashboard ? "electron-managed" : normalized ? "remote-api" : "browser-same-origin",
    apiBaseUrl: normalized,
    displayName: config.displayName || (normalized || "This MAC server"),
  };
}

const INITIAL_API_BASE_URL = readStoredApiBaseUrl();

const state: DashboardState = {
  activeView: DEFAULT_URL_STATE.activeView,
  token: sessionStorage.getItem(TOKEN_KEY) || "",
  apiBaseUrl: INITIAL_API_BASE_URL,
  connection: connectionSnapshot(INITIAL_API_BASE_URL),
  loading: false,
  loadedAt: null,
  data: null,
  error: null,
  actionMessage: null,
  agentQuery: DEFAULT_URL_STATE.agentQuery,
  agentFilter: DEFAULT_URL_STATE.agentFilter,
  agentSort: DEFAULT_URL_STATE.agentSort,
  agentPage: DEFAULT_URL_STATE.agentPage,
  projectFilter: DEFAULT_URL_STATE.projectFilter,
  showDerivedProjects: DEFAULT_URL_STATE.showDerivedProjects,
  taskFilter: DEFAULT_URL_STATE.taskFilter,
  selectedId: DEFAULT_URL_STATE.selectedId,
  targets: [],
  selectedTargetId: readStoredTargetId(),
  selectedTokenSourceId: readStoredTokenSourceId(),
  auditSubjectType: DEFAULT_URL_STATE.auditSubjectType,
  auditSubjectId: DEFAULT_URL_STATE.auditSubjectId,
  auditEventPrefix: DEFAULT_URL_STATE.auditEventPrefix,
  auditActor: DEFAULT_URL_STATE.auditActor,
  auditLayer: DEFAULT_URL_STATE.auditLayer,
  auditLevel: DEFAULT_URL_STATE.auditLevel,
  auditAgentId: DEFAULT_URL_STATE.auditAgentId,
  auditTaskId: DEFAULT_URL_STATE.auditTaskId,
  auditProject: DEFAULT_URL_STATE.auditProject,
  auditFleet: DEFAULT_URL_STATE.auditFleet,
  auditSince: DEFAULT_URL_STATE.auditSince,
  auditUntil: DEFAULT_URL_STATE.auditUntil,
  observabilityLive: [],
  dashboardStream: null,
  dashboardStreamStatus: "idle",
  observabilityStream: null,
  observabilityStreamStatus: "idle",
  selectedWorkflowId: "",
  selectedNodeKey: "",
  workflowPlanDraft: null,
};

const nodes: DashboardNodes = {
  nav: requiredElement("#viewNav"),
  title: requiredElement("#viewTitle"),
  viewSelect: requiredElement<HTMLSelectElement>("#viewSelect"),
  banner: requiredElement("#banner"),
  content: requiredElement("#content"),
  refresh: requiredElement("#refreshButton"),
  syncState: requiredElement("#syncState"),
  connectionForm: requiredElement<HTMLFormElement>("#connectionForm"),
  topbarTargetSelect: requiredElement<HTMLSelectElement>("#topbarTargetSelect"),
  tokenSourceSelect: requiredElement<HTMLSelectElement>("#tokenSourceSelect"),
  topbarTokenInput: requiredElement<HTMLInputElement>("#topbarTokenInput"),
  topbarTokenField: requiredElement("#topbarTokenField"),
  topbarTestingUrlInput: requiredElement<HTMLInputElement>("#topbarTestingUrlInput"),
  topbarTestingUrlField: requiredElement("#topbarTestingUrlField"),
  connectionButton: requiredElement<HTMLButtonElement>("#connectionButton"),
  tokenForm: requiredElement("#tokenForm"),
  targetSelect: requiredElement<HTMLSelectElement>("#targetSelect"),
  tokenInput: requiredElement("#tokenInput"),
  apiUrlInput: requiredElement<HTMLInputElement>("#apiUrlInput"),
  clearToken: requiredElement("#clearTokenButton"),
  loginScreen: requiredElement("#loginScreen"),
  loginForm: requiredElement<HTMLFormElement>("#loginForm"),
  loginTargetSelect: requiredElement<HTMLSelectElement>("#loginTargetSelect"),
  loginTokenInput: requiredElement<HTMLInputElement>("#loginTokenInput"),
  loginApiUrlInput: requiredElement<HTMLInputElement>("#loginApiUrlInput"),
  serviceLinks: requiredElement("#serviceLinks"),
  connectionBadge: requiredElement("#connectionBadge"),
  themeToggle: requiredElement<HTMLButtonElement>("#themeToggle"),
};
const api = createDashboardApi(() => state.token, () => state.apiBaseUrl);

applyTheme(readStoredTheme());
nodes.tokenInput.value = state.token;
nodes.loginTokenInput.value = state.token;
nodes.topbarTokenInput.value = state.token;
nodes.apiUrlInput.value = state.apiBaseUrl;
nodes.loginApiUrlInput.value = state.apiBaseUrl;
nodes.topbarTestingUrlInput.value = state.apiBaseUrl;
renderTargetSelects();
renderTokenSourceSelect();
bindEvents();
const connectionReady = syncElectronConnection();
if (state.token || window.macDashboard) {
  hideLoginScreen();
  connectionReady.finally(() => loadDashboard());
} else {
  showLoginScreen(false);
}

function bindEvents(): void {
  nodes.nav.addEventListener("click", (event) => {
    const button = (event.target as Element | null)?.closest<HTMLElement>("[data-view]");
    if (!button) return;
    state.activeView = (button.dataset.view || "overview") as ViewKey;
    state.actionMessage = null;
    updateUrlState();
    render();
  });
  nodes.refresh.addEventListener("click", () => loadDashboard());
  nodes.themeToggle.addEventListener("click", () => toggleTheme());
  nodes.viewSelect.addEventListener("change", () => {
    navigateDashboardView(nodes.viewSelect.value as ViewKey);
  });
  nodes.content.addEventListener("click", handleContentClick);
  nodes.content.addEventListener("keydown", handleContentKeydown);
  nodes.content.addEventListener("submit", handleActionSubmit);
  nodes.content.addEventListener("change", handleContentChange);
  nodes.loginTargetSelect.addEventListener("change", () => mirrorTargetSelect(nodes.loginTargetSelect.value));
  nodes.targetSelect.addEventListener("change", () => mirrorTargetSelect(nodes.targetSelect.value));
  nodes.topbarTargetSelect.addEventListener("change", () => mirrorTargetSelect(nodes.topbarTargetSelect.value));
  nodes.tokenSourceSelect.addEventListener("change", () => mirrorTokenSourceSelect(nodes.tokenSourceSelect.value));
  nodes.connectionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (isConnectionLive()) await disconnectFromControls();
    else await connectFromControls(nodes.topbarTargetSelect.value, nodes.topbarTestingUrlInput.value, nodes.topbarTokenInput.value);
  });
  nodes.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = nodes.loginTokenInput.value.trim();
    state.token = token;
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
    nodes.tokenInput.value = token;
    nodes.topbarTokenInput.value = token;
    try {
      await connectFromControls(nodes.loginTargetSelect.value, nodes.loginApiUrlInput.value, token);
      hideLoginScreen();
    } catch (error) {
      state.error = error instanceof Error ? error.message : String(error);
      render();
      showLoginScreen(false);
    }
  });
  nodes.tokenForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    state.token = nodes.tokenInput.value.trim();
    if (state.token) sessionStorage.setItem(TOKEN_KEY, state.token);
    else sessionStorage.removeItem(TOKEN_KEY);
    nodes.topbarTokenInput.value = state.token;
    try {
      await connectFromControls(nodes.targetSelect.value, nodes.apiUrlInput.value, state.token);
      hideLoginScreen();
    } catch (error) {
      state.error = error instanceof Error ? error.message : String(error);
      render();
    }
  });
  nodes.clearToken.addEventListener("click", () => {
    state.token = "";
    nodes.tokenInput.value = "";
    nodes.loginTokenInput.value = "";
    nodes.topbarTokenInput.value = "";
    sessionStorage.removeItem(TOKEN_KEY);
    showLoginScreen();
  });
  nodes.serviceLinks.addEventListener("click", async (event) => {
    const btn = (event.target as Element | null)?.closest<HTMLElement>("[data-service-id]");
    if (!btn || btn.hasAttribute("disabled")) return;
    const serviceId = btn.dataset.serviceId || "";
    const directUrl = btn.dataset.url || "";
    if (btn.dataset.passThrough === "1" && serviceId) {
      btn.setAttribute("disabled", "");
      try {
        const result = (await requestJSON(`/dashboard/service-links/${serviceId}/navigate`)) as { url: string };
        await openService(serviceId, result.url);
      } catch {
        if (directUrl) await openService(serviceId, directUrl);
      } finally {
        btn.removeAttribute("disabled");
      }
    } else if (directUrl) {
      await openService(serviceId, directUrl);
    }
  });
}

async function loadDashboard(): Promise<void> {
  state.loading = true;
  state.error = null;
  renderSyncState();
  try {
    applyDashboardData((await requestJSON("/dashboard/state")) as DashboardData);
    state.connection = { ...state.connection, connected: true };
    syncDashboardSubscription();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
    state.connection = { ...state.connection, connected: false };
    stopDashboardStream();
    if (isAuthError(state.error) && !window.macDashboard) {
      sessionStorage.removeItem(TOKEN_KEY);
      showLoginScreen();
    }
  } finally {
    state.loading = false;
    render();
  }
}

function applyDashboardData(data: DashboardData): void {
  state.data = data;
  applyServerTime(data.server_time || data.updated_at || "");
}

function applyServerTime(serverTime: string): void {
  const parsed = serverTime ? new Date(serverTime) : new Date();
  state.loadedAt = Number.isNaN(parsed.getTime()) ? new Date() : parsed;
}

async function requestJSON(path: string, init: RequestInit = {}): Promise<unknown> {
  return api.request(path, init);
}

async function openService(serviceId: string, fallbackUrl: string): Promise<void> {
  return api.openService(serviceId, fallbackUrl);
}

function targetLabel(target: DashboardTarget): string {
  if (target.mode === "fleet-ssh") return `${target.label} (SSH)`;
  if (target.mode === "fleet-direct") return target.label;
  return target.label || "Testing URL";
}

function renderTargetSelects(): void {
  const targets = state.targets.length
    ? state.targets
    : [{ id: "", label: "Testing URL", mode: "testing-url" as const, apiUrl: state.apiBaseUrl }];
  const selected = state.selectedTargetId && targets.some((target) => target.id === state.selectedTargetId)
    ? state.selectedTargetId
    : targets[0]?.id || "";
  const options = targets.map((target) => `<option value="${escapeHtml(target.id)}">${escapeHtml(targetLabel(target))}</option>`).join("");
  for (const select of [nodes.loginTargetSelect, nodes.targetSelect, nodes.topbarTargetSelect]) {
    select.innerHTML = options;
    select.value = selected;
    select.disabled = targets.length <= 1 && !window.macDashboard;
  }
  state.selectedTargetId = selected;
  renderTokenSourceSelect();
  syncTestingUrlControls();
}

function mirrorTargetSelect(targetId: string): void {
  state.selectedTargetId = targetId;
  nodes.loginTargetSelect.value = targetId;
  nodes.targetSelect.value = targetId;
  nodes.topbarTargetSelect.value = targetId;
  renderTokenSourceSelect();
  syncTestingUrlControls();
  try {
    if (targetId) sessionStorage.setItem(TARGET_KEY, targetId);
    else sessionStorage.removeItem(TARGET_KEY);
  } catch {
    /* storage unavailable — target still applies for this session */
  }
}

function selectedTarget(): DashboardTarget | undefined {
  return state.targets.find((item) => item.id === state.selectedTargetId);
}

function tokenSourcesForSelectedTarget() {
  const target = selectedTarget();
  if (window.macDashboard && target?.tokenSources?.length) return target.tokenSources;
  return [{ id: "manual", label: "Manual bearer token" }];
}

function renderTokenSourceSelect(): void {
  const sources = tokenSourcesForSelectedTarget();
  const target = selectedTarget();
  const defaultSourceId = target?.selectedTokenSourceId || sources[0]?.id || "manual";
  const selected = state.selectedTokenSourceId && sources.some((source) => source.id === state.selectedTokenSourceId)
    ? state.selectedTokenSourceId
    : defaultSourceId;
  nodes.tokenSourceSelect.innerHTML = sources
    .map((source) => `<option value="${escapeHtml(source.id)}">${escapeHtml(source.label)}</option>`)
    .join("");
  nodes.tokenSourceSelect.value = selected;
  state.selectedTokenSourceId = selected;
  syncTokenSourceControls();
}

function mirrorTokenSourceSelect(sourceId: string): void {
  state.selectedTokenSourceId = sourceId;
  nodes.tokenSourceSelect.value = sourceId;
  syncTokenSourceControls();
  try {
    if (sourceId) sessionStorage.setItem(TOKEN_SOURCE_KEY, sourceId);
    else sessionStorage.removeItem(TOKEN_SOURCE_KEY);
  } catch {
    /* storage unavailable — token source still applies for this session */
  }
}

function syncTokenSourceControls(): void {
  const isManual = !window.macDashboard || state.selectedTokenSourceId === "manual";
  nodes.topbarTokenField.hidden = !isManual;
  nodes.topbarTokenInput.disabled = !isManual;
  if (!isManual) nodes.topbarTokenInput.value = "";
}

function syncTestingUrlControls(): void {
  const target = selectedTarget();
  const isTesting = !window.macDashboard || !target || target.mode.startsWith("testing");
  const value = isTesting ? (target?.apiUrl || state.apiBaseUrl) : "";
  nodes.apiUrlInput.disabled = !isTesting;
  nodes.loginApiUrlInput.disabled = !isTesting;
  nodes.topbarTestingUrlInput.disabled = !isTesting;
  nodes.topbarTestingUrlField.hidden = !isTesting;
  nodes.apiUrlInput.value = value;
  nodes.loginApiUrlInput.value = value;
  nodes.topbarTestingUrlInput.value = value;
}

async function connectFromControls(targetId: string, testingUrl: string, manualToken: string): Promise<void> {
  state.error = null;
  await applySelectedTarget(targetId, testingUrl, state.selectedTokenSourceId, manualToken);
  hideLoginScreen();
  await loadDashboard();
}

async function disconnectFromControls(): Promise<void> {
  stopDashboardStream();
  stopObservabilityStream();
  state.loading = false;
  state.error = null;
  state.actionMessage = null;
  state.data = null;
  state.loadedAt = null;
  if (window.macDashboard) {
    const connection = await api.disconnect();
    state.connection = {
      ...state.connection,
      ...connection,
      mode: "electron-managed",
      connected: false,
    };
  } else {
    state.connection = { ...api.connection(), connected: false };
  }
  render();
}

async function applySelectedTarget(
  targetId: string,
  testingUrl: string,
  tokenSourceId = "",
  manualToken = "",
): Promise<void> {
  mirrorTargetSelect(targetId);
  if (tokenSourceId) mirrorTokenSourceSelect(tokenSourceId);
  const target = state.targets.find((item) => item.id === targetId);
  if (window.macDashboard && target?.id) {
    const options = {
      ...(target.mode.startsWith("testing") ? { apiUrl: normalizeApiBaseUrl(testingUrl) } : {}),
      ...(state.selectedTokenSourceId ? { tokenSourceId: state.selectedTokenSourceId } : {}),
      ...(state.selectedTokenSourceId === "manual" ? { token: manualToken.trim() } : {}),
    };
    const connection = await api.selectTarget(target.id, options);
    state.connection = {
      mode: "electron-managed",
      apiBaseUrl: normalizeApiBaseUrl(connection.apiBaseUrl || state.apiBaseUrl),
      displayName: connection.displayName || target.label || "Electron managed",
      targetId: connection.targetId || target.id,
      tokenSourceId: connection.tokenSourceId || state.selectedTokenSourceId,
      connected: connection.connected !== false,
    };
    state.apiBaseUrl = state.connection.apiBaseUrl;
    if (connection.tokenSourceId) mirrorTokenSourceSelect(connection.tokenSourceId);
    syncTestingUrlControls();
    renderSyncState();
    return;
  }
  state.token = manualToken.trim();
  if (state.token) sessionStorage.setItem(TOKEN_KEY, state.token);
  else sessionStorage.removeItem(TOKEN_KEY);
  setApiBaseUrl(testingUrl);
}

function setApiBaseUrl(raw: string): void {
  state.apiBaseUrl = normalizeApiBaseUrl(raw);
  state.connection = api.connection();
  state.connection.connected = false;
  nodes.apiUrlInput.value = state.apiBaseUrl;
  nodes.loginApiUrlInput.value = state.apiBaseUrl;
  syncTestingUrlControls();
  try {
    if (state.apiBaseUrl) sessionStorage.setItem(API_BASE_URL_KEY, state.apiBaseUrl);
    else sessionStorage.removeItem(API_BASE_URL_KEY);
  } catch {
    /* storage unavailable — connection still applies for this session */
  }
  renderSyncState();
}

async function syncElectronConnection(): Promise<void> {
  const bridge = window.macDashboard;
  if (!bridge?.connection) {
    state.connection = api.connection();
    renderSyncState();
    return;
  }
  try {
    state.targets = await api.targets();
    renderTargetSelects();
    const connection = await bridge.connection();
    if (!connection) return;
    if (connection.apiBaseUrl !== undefined) {
      state.apiBaseUrl = normalizeApiBaseUrl(connection.apiBaseUrl);
    }
    if (connection.targetId) mirrorTargetSelect(connection.targetId);
    if (connection.tokenSourceId) mirrorTokenSourceSelect(connection.tokenSourceId);
    state.connection = {
      mode: "electron-managed",
      apiBaseUrl: state.apiBaseUrl,
      displayName: connection.displayName || state.connection.displayName || "Electron managed",
      targetId: connection.targetId || state.selectedTargetId,
      tokenSourceId: connection.tokenSourceId || state.selectedTokenSourceId,
      connected: !!connection.connected,
    };
    syncTestingUrlControls();
    renderSyncState();
  } catch {
    state.connection = { ...api.connection(), mode: "electron-managed" };
    renderSyncState();
  }
}

function renderServiceLinksSidebar(services: ServiceLinkRecord[]): string {
  const visible = services.filter((s) => s.url || s.ui_url);
  if (!visible.length) return "";
  return `
    <span class="service-link-label">Services</span>
    ${visible.map((service) => {
      const hasPassThrough = !!(service.auth?.credential_pass_through);
      const directUrl = service.ui_url || service.url || "";
      const tone = healthTone(service.status);
      return `<button class="service-link-btn" type="button"
        data-service-id="${escapeHtml(service.id)}"
        data-url="${escapeHtml(directUrl)}"
        data-pass-through="${hasPassThrough ? "1" : "0"}"
        title="${escapeHtml(service.role)}"
      ><span>${escapeHtml(service.name)}</span>${chip(service.status || "unknown", tone)}</button>`;
    }).join("")}
  `;
}

function render(): void {
  document.querySelectorAll<HTMLElement>("[data-view]").forEach((button) => {
    const active = button.dataset.view === state.activeView;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  nodes.title.textContent = VIEW_TITLES[state.activeView];
  nodes.viewSelect.value = state.activeView;
  renderSyncState();
  renderBanner();
  if (state.data) {
    nodes.serviceLinks.hidden = false;
    nodes.serviceLinks.innerHTML = renderServiceLinksSidebar(state.data.service_links || []);
  }
  if (state.loading && !state.data) {
    nodes.content.innerHTML = `<div class="empty-state">Loading</div>`;
    return;
  }
  if (!state.data) {
    nodes.content.innerHTML = `<div class="empty-state">No dashboard data</div>`;
    return;
  }
  const action = state.actionMessage ? `<div class="action-status">${escapeHtml(state.actionMessage)}</div>` : "";
  const body =
    state.activeView === "work"
      ? renderWork()
      : state.activeView === "projects"
      ? renderProjects()
      : state.activeView === "map"
      ? renderMap()
      : state.activeView === "fleets"
      ? renderFleets()
      : state.activeView === "agents"
      ? renderAgents()
      : state.activeView === "tasks"
        ? renderTasks()
        : state.activeView === "workflows"
          ? renderWorkflows()
          : state.activeView === "hermes"
            ? renderHermes()
            : state.activeView === "ops"
              ? renderOperations()
              : state.activeView === "integrations"
                ? renderIntegrations()
                : state.activeView === "runtime"
                  ? renderRuntime()
                  : state.activeView === "observability"
                    ? renderObservability()
                    : state.activeView === "secrets"
                      ? renderSecrets()
                      : renderOverview();
  nodes.content.innerHTML = `${action}${body}`;
  bindViewControls();
  syncDashboardSubscription();
  syncObservabilitySubscription();
}

function renderPreservingFocusedControl(): void {
  const active = document.activeElement as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null;
  const id = active?.id || "";
  const selectionStart = active && "selectionStart" in active ? active.selectionStart : null;
  const selectionEnd = active && "selectionEnd" in active ? active.selectionEnd : null;
  render();
  if (!id) return;
  const next = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null;
  if (!next) return;
  next.focus();
  if (
    selectionStart !== null
    && selectionEnd !== null
    && "setSelectionRange" in next
    && next instanceof HTMLInputElement
  ) {
    next.setSelectionRange(selectionStart, selectionEnd);
  }
}

function isConnectionLive(): boolean {
  return !!state.connection?.connected;
}

function renderSyncState(): void {
  const connection = state.connection || api.connection();
  const modeLabel =
    connection.mode === "electron-managed"
      ? "Electron"
      : connection.mode === "remote-api"
        ? "Remote"
        : "Same origin";
  const displayName = connection.displayName || connection.apiBaseUrl || "This MAC server";
  nodes.connectionBadge.textContent = `${modeLabel}: ${displayName}`;
  nodes.connectionBadge.title = connection.apiBaseUrl || displayName;
  const connected = isConnectionLive();
  nodes.connectionButton.textContent = connected ? "Disconnect" : "Connect";
  nodes.connectionButton.setAttribute("aria-pressed", String(connected));
  nodes.connectionButton.classList.toggle("is-connected", connected);
  nodes.syncState.textContent = state.loading
    ? "Loading"
    : state.loadedAt
      ? `Updated ${formatTime(state.loadedAt)}`
      : connected
        ? "Not updated"
        : "Not connected";
}

function renderBanner(): void {
  if (!state.error) {
    nodes.banner.hidden = true;
    nodes.banner.textContent = "";
    return;
  }
  nodes.banner.hidden = false;
  nodes.banner.textContent = isAuthError(state.error)
    ? "Dashboard data needs a signed-in session or a token with read scope."
    : state.error;
}

function isAuthError(message: string): boolean {
  return /^403\b/.test(message);
}

function showLoginScreen(focus = true): void {
  nodes.loginTokenInput.value = state.token || nodes.tokenInput.value.trim();
  nodes.loginApiUrlInput.value = state.apiBaseUrl || nodes.apiUrlInput.value.trim();
  renderTargetSelects();
  nodes.loginScreen.hidden = false;
  if (focus) window.setTimeout(() => nodes.loginTokenInput.focus(), 0);
}

function hideLoginScreen(): void {
  nodes.loginScreen.hidden = true;
}

function readUrlState(): Pick<DashboardState, "activeView" | "agentQuery" | "agentFilter" | "agentSort" | "agentPage" | "projectFilter" | "showDerivedProjects" | "taskFilter" | "selectedId" | "auditSubjectType" | "auditSubjectId" | "auditEventPrefix" | "auditActor" | "auditLayer" | "auditLevel" | "auditAgentId" | "auditTaskId" | "auditProject" | "auditFleet" | "auditSince" | "auditUntil"> {
  const params = new URLSearchParams(window.location.search);
  const rawView = params.get("view") || "overview";
  const page = Number(params.get("agent_page") || "1");
  const subjectType = params.get("obs_subject_type") || "";
  return {
    activeView: VIEW_KEYS.has(rawView) ? rawView as ViewKey : "overview",
    agentQuery: params.get("agent_q") || "",
    agentFilter: params.get("agent_filter") || "all",
    agentSort: params.get("agent_sort") || "name",
    agentPage: Number.isFinite(page) && page > 0 ? Math.floor(page) : 1,
    projectFilter: params.get("project") || "all",
    showDerivedProjects: params.get("show_derived") === "1",
    taskFilter: params.get("task_state") || "all",
    selectedId: params.get("selected") || "",
    auditSubjectType: AUDIT_SUBJECT_TYPES.includes(subjectType) ? subjectType : "",
    auditSubjectId: params.get("obs_subject_id") || "",
    auditEventPrefix: params.get("obs_event_prefix") || "",
    auditActor: params.get("obs_actor") || "",
    auditLayer: params.get("obs_layer") || "",
    auditLevel: params.get("obs_level") || "",
    auditAgentId: params.get("obs_agent") || "",
    auditTaskId: params.get("obs_task") || "",
    auditProject: params.get("obs_project") || "",
    auditFleet: params.get("obs_fleet") || "",
    auditSince: params.get("obs_since") || "",
    auditUntil: params.get("obs_until") || "",
  };
}

function applyUrlState(): void {
  const next = readUrlState();
  state.activeView = next.activeView;
  state.agentQuery = next.agentQuery;
  state.agentFilter = next.agentFilter;
  state.agentSort = next.agentSort;
  state.agentPage = next.agentPage;
  state.projectFilter = next.projectFilter;
  state.showDerivedProjects = next.showDerivedProjects;
  state.taskFilter = next.taskFilter;
  state.selectedId = next.selectedId;
  state.auditSubjectType = next.auditSubjectType;
  state.auditSubjectId = next.auditSubjectId;
  state.auditEventPrefix = next.auditEventPrefix;
  state.auditActor = next.auditActor;
  state.auditLayer = next.auditLayer;
  state.auditLevel = next.auditLevel;
  state.auditAgentId = next.auditAgentId;
  state.auditTaskId = next.auditTaskId;
  state.auditProject = next.auditProject;
  state.auditFleet = next.auditFleet;
  state.auditSince = next.auditSince;
  state.auditUntil = next.auditUntil;
}

function updateUrlState(replace = false): void {
  const params = new URLSearchParams();
  if (state.activeView !== "overview") params.set("view", state.activeView);
  if (state.agentQuery.trim()) params.set("agent_q", state.agentQuery.trim());
  if (state.agentFilter !== "all") params.set("agent_filter", state.agentFilter);
  if (state.agentSort !== "name") params.set("agent_sort", state.agentSort);
  if (state.agentPage > 1) params.set("agent_page", String(state.agentPage));
  if (state.projectFilter !== "all") params.set("project", state.projectFilter);
  if (state.showDerivedProjects) params.set("show_derived", "1");
  if (state.taskFilter !== "all") params.set("task_state", state.taskFilter);
  if (state.selectedId) params.set("selected", state.selectedId);
  if (state.auditSubjectType) params.set("obs_subject_type", state.auditSubjectType);
  if (state.auditSubjectId.trim()) params.set("obs_subject_id", state.auditSubjectId.trim());
  if (state.auditEventPrefix.trim()) params.set("obs_event_prefix", state.auditEventPrefix.trim());
  if (state.auditActor.trim()) params.set("obs_actor", state.auditActor.trim());
  if (state.auditLayer.trim()) params.set("obs_layer", state.auditLayer.trim());
  if (state.auditLevel.trim()) params.set("obs_level", state.auditLevel.trim());
  if (state.auditAgentId.trim()) params.set("obs_agent", state.auditAgentId.trim());
  if (state.auditTaskId.trim()) params.set("obs_task", state.auditTaskId.trim());
  if (state.auditProject.trim()) params.set("obs_project", state.auditProject.trim());
  if (state.auditFleet.trim()) params.set("obs_fleet", state.auditFleet.trim());
  if (state.auditSince.trim()) params.set("obs_since", state.auditSince.trim());
  if (state.auditUntil.trim()) params.set("obs_until", state.auditUntil.trim());
  const query = params.toString();
  const nextUrl = `${window.location.pathname}${query ? `?${query}` : ""}`;
  const method = replace ? "replaceState" : "pushState";
  window.history[method]({}, "", nextUrl);
}

window.addEventListener("popstate", () => {
  applyUrlState();
  state.actionMessage = null;
  render();
});

function isDerivedProject(project: ProjectSummary): boolean {
  return !project.project_id;
}

function visibleProjectSummaries(data: DashboardData, includeSelected = true): ProjectSummary[] {
  if (state.showDerivedProjects) return data.project_summaries;
  const visible = data.project_summaries.filter((project) => !isDerivedProject(project));
  if (!includeSelected) return visible;
  const selectedNames = new Set<string>();
  if (state.projectFilter !== "all") selectedNames.add(state.projectFilter);
  if (state.selectedId) {
    const selectedTask = taskDetailById(data, state.selectedId);
    if (selectedTask) selectedNames.add(taskProject(selectedTask.task));
    selectedNames.add(state.selectedId);
  }
  for (const name of selectedNames) {
    if (!name || visible.some((project) => project.project === name)) continue;
    const selected = data.project_summaries.find((project) => project.project === name);
    if (selected) visible.push(selected);
  }
  return visible;
}

function projectFilterOptions(data: DashboardData): ProjectSummary[] {
  return visibleProjectSummaries(data, true);
}

function projectScopeSummaries(data: DashboardData): ProjectSummary[] {
  const projects = visibleProjectSummaries(data, true);
  return state.projectFilter === "all"
    ? projects
    : projects.filter((project) => project.project === state.projectFilter);
}

function projectVisibilityToggle(data: DashboardData): string {
  const derivedCount = data.project_summaries.filter(isDerivedProject).length;
  if (!derivedCount) return "";
  return `
    <label class="inline-checkbox toolbar-checkbox">
      <input type="checkbox" id="showDerivedProjects" ${state.showDerivedProjects ? "checked" : ""}>
      Show derived
    </label>
  `;
}

function renderOverview(): string {
  const data = mustData();
  const counts = data.overview.counts;
  const startup = data.hermes_startup;
  const startupStatus = startup?.operator_health?.status || (startup?.ready ? "healthy" : "degraded");
  const readyStories = visibleProjectSummaries(data, false).reduce((sum, project) => sum + project.ready_count, 0);
  const attentionCount =
    data.agents.filter((item) => !item.availability.eligible).length +
    data.dead_letters.length +
    data.rollouts.filter((item) => ["rescuing", "failed"].includes(String(item.rollout.status))).length +
    data.dispatch.tasks.filter((item) => item.eligible_agent_count === 0).length;
  const pendingNotifications = data.notifications.filter((item) => item.status === "pending").length;
  const openFindings = data.integration_findings.filter((item) => item.status === "open").length;
  return `
    ${overviewLaunchpad(data, attentionCount, pendingNotifications, openFindings)}
    <section class="metric-grid">
      ${metric("Fleets", counts.fleets || 0, `${data.fleets.reduce((sum, fleet) => sum + (fleet.agent_ids || []).length, 0)} fleet memberships`)}
      ${metric("Agents", counts.agents || 0, `${counts.healthy_agents || 0} healthy, ${counts.busy_agents || 0} busy`)}
      ${metric("Projects", counts.projects || 0, `${readyStories} ready stories`)}
      ${metric("Active Work", counts.active_tasks || 0, `${counts.dead_letters || 0} dead letters`)}
      ${metric("Hermes", counts.hermes_instances || 0, `${startupStatus}, ${counts.platform_bindings || 0} bindings`)}
    </section>
    <details class="surface action-drawer">
      <summary>
        <span>Dispatch Controls</span>
        <span class="muted small">Manual lease tick for operators</span>
      </summary>
      <form class="action-form compact" data-action="dispatchTick">
        <label>Lease seconds <input name="lease_seconds" type="number" value="900" min="1"></label>
        <label>Limit <input name="limit" type="number" value="100" min="1"></label>
        <label>Stale after <input name="stale_after_seconds" type="number" placeholder="optional"></label>
        <button type="submit">Run Tick</button>
      </form>
    </details>
    <section class="split">
      <div class="surface">
        <h2>Task States</h2>
        ${stateBars(TASK_STATES, data.overview.task_states, data.tasks.length)}
      </div>
      <div class="surface">
        <h2>Attention</h2>
        ${attentionList(data)}
      </div>
    </section>
  `;
}

function overviewLaunchpad(
  data: DashboardData,
  attentionCount: number,
  pendingNotifications: number,
  openFindings: number,
): string {
  const readyStories = visibleProjectSummaries(data, false).reduce((sum, project) => sum + project.ready_count, 0);
  const blockedAgents = data.agents.filter((item) => !item.availability.eligible).length;
  return `
    <section class="launchpad" aria-label="Top-line dashboard actions">
      ${launchpadAction("Review Work", "Project frontier, active stories, and dependencies", "work", `${readyStories} ready`)}
      ${launchpadAction("Create Task", "Open the task board and add new work", "tasks", `${data.tasks.length} tasks`)}
      ${launchpadAction("Add Agent", "Register capacity and inspect fleet health", "agents", `${blockedAgents} blocked`)}
      ${launchpadAction("Watch Incidents", "Notifications, findings, audits, and live stream", "observability", `${attentionCount + pendingNotifications + openFindings} signals`)}
      ${launchpadAction("Runtime Rollouts", "Runtimes, canaries, health, and rescue controls", "runtime", `${data.rollouts.length} rollouts`)}
      ${launchpadAction("Secret Access", "Request handles and audit redacted access", "secrets", `${data.secrets.length} secrets`)}
    </section>
  `;
}

function launchpadAction(title: string, detail: string, view: ViewKey, badge: string): string {
  return `
    <button class="launchpad-action" type="button" data-dashboard-go="${escapeHtml(view)}">
      <span>
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(detail)}</span>
      </span>
      ${chip(badge, "info")}
    </button>
  `;
}

function renderWork(): string {
  const data = mustData();
  const projects = visibleProjectSummaries(data);
  const projectOptions = projectFilterOptions(data);
  const selectedProject = selectedProjectSummary(data);
  const scopedProjects = projectScopeSummaries(data);
  const selectedTask = selectedTaskDetail(data) || selectedProject?.frontier_tasks
    .map((task) => taskDetailById(data, task.id))
    .find(Boolean) || null;
  const readyStories = scopedProjects.reduce((sum, project) => sum + project.ready_count, 0);
  const blockedStories = scopedProjects.reduce((sum, project) => sum + project.blocked_count, 0);
  const activeAgents = new Set(scopedProjects.flatMap((project) => project.active_agent_ids)).size;
  return `
    <section class="toolbar">
      <select id="projectFilter">
        ${option("all", "All projects", state.projectFilter)}
        ${projectOptions.map((project) => option(project.project, project.project, state.projectFilter)).join("")}
      </select>
      ${projectVisibilityToggle(data)}
      <button type="button" id="clearWorkScope">Clear Scope</button>
    </section>
    <section class="metric-grid">
      ${metric("Projects", projects.length, `${readyStories} ready stories`)}
      ${metric("Active Agents", activeAgents, "working in selected scope")}
      ${metric("Blocked Stories", blockedStories, "waiting on dependencies")}
      ${metric("Cross-Project Edges", projects.reduce((sum, project) => sum + project.cross_project_dependency_count, 0), "dependency order links")}
    </section>
    <section class="work-layout">
      <div class="surface">
        <div class="surface-heading">
          <h2>Epic / Project Frontier</h2>
          ${chip(selectedProject?.project || "all projects", "info")}
        </div>
        <div class="project-frontier-list">
          ${scopedProjects
            .map(projectFrontierRecord)
            .join("") || `<div class="empty-state">No projects</div>`}
        </div>
      </div>
      <div class="surface">
        <div class="surface-heading">
          <h2>Story Scope</h2>
          ${selectedTask ? chip(selectedTask.task.state, statusTone(selectedTask.task.state)) : chip("none selected", "warn")}
        </div>
        ${selectedTask ? storyScopePanel(data, selectedTask) : `<div class="empty-state">Select a story to inspect related agents</div>`}
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Project Agents</h2>
        ${projectAgentsPanel(data, selectedProject)}
      </div>
      <div class="surface">
        <h2>Dependency Order</h2>
        ${dependencyOrderPanel(data, selectedProject)}
      </div>
    </section>
  `;
}

function renderProjects(): string {
  const data = mustData();
  const writable = canWrite(data);
  const projectOptions = projectFilterOptions(data);
  const totalVisibleProjects = visibleProjectSummaries(data, false);
  const projects = projectScopeSummaries(data);
  const derivedCount = data.project_summaries.filter(isDerivedProject).length;
  const activeAgents = new Set(projects.flatMap((project) => project.active_agent_ids)).size;
  return `
    <section class="toolbar">
      <select id="projectFilter">
        ${option("all", "All projects", state.projectFilter)}
        ${projectOptions.map((project) => option(project.project, project.project, state.projectFilter)).join("")}
      </select>
      ${projectVisibilityToggle(data)}
      <button type="button" id="clearWorkScope">Clear Scope</button>
      ${sessionAccessBadge(data)}
    </section>
    ${writable ? "" : `<section class="action-status">Read-only token: project fields can be inspected but not edited.</section>`}
    <section class="project-focus-layout">
      ${projectInspector(projects, data)}
      <div class="surface">
        <h2>Project Metrics</h2>
        <section class="metric-grid compact-metrics">
          ${metric("Total Projects", totalVisibleProjects.length, state.showDerivedProjects ? "including derived buckets" : "record-backed projects")}
          ${metric("Hidden Derived", state.showDerivedProjects ? 0 : derivedCount, state.showDerivedProjects ? "derived buckets visible" : "derived buckets hidden")}
          ${metric("Visible Projects", projects.length, state.projectFilter === "all" ? "unfiltered" : `scope: ${state.projectFilter}`)}
          ${metric("Ready Stories", projects.reduce((sum, project) => sum + project.ready_count, 0), "available for dispatch")}
          ${metric("Active Agents", activeAgents, "working in scope")}
        </section>
        <details class="action-drawer inline-drawer">
          <summary>
            <span>Create Project</span>
            <span class="muted small">Add a durable project record</span>
          </summary>
          <form class="action-form aligned-form project-create-form" data-action="projectCreate">
            <label>Name <input name="name" required ${disabledAttr(!writable)}></label>
            <label>Status ${select("status", ["active", "inactive", "archived"], "active", !writable)}</label>
            <label class="field-full">Description <textarea name="description" ${disabledAttr(!writable)}></textarea></label>
            <label class="field-full">Metadata JSON <textarea class="json-editor" name="metadata" placeholder="{}" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabledAttr(!writable)}></textarea></label>
            <div class="field-full form-actions"><button type="submit" ${disabledAttr(!writable)}>Create</button></div>
          </form>
        </details>
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Projects</h2>
        <div class="chip-row">
          ${chip(`${projects.length} visible`, "info")}
          ${state.projectFilter === "all" ? "" : chip(`scope ${state.projectFilter}`, "warn")}
        </div>
      </div>
      ${projectTable(projects, data)}
    </section>
  `;
}

function renderFleets(): string {
  const data = mustData();
  const activeFleets = data.fleets.filter((fleet) => fleet.status === "active").length;
  const selectedFleet = selectedFleetRecord(data);
  return `
    <section class="metric-grid">
      ${metric("Fleets", data.fleets.length, `${activeFleets} active`)}
      ${metric("Members", data.fleets.reduce((sum, fleet) => sum + (fleet.agent_ids || []).length, 0), "agent memberships")}
      ${metric("Agents", data.agents.length, "available to assign")}
      ${metric("Machines", data.machines.length, "registered hosts")}
    </section>
    <section class="action-status">Fleet membership is derived from agent registration. Use the Agents view to inspect each agent's fleet.</section>
    <section class="split">
      <div class="surface">
        <h2>Fleet Topology</h2>
        ${fleetMembershipSummary(data)}
      </div>
      <div class="surface">
        <h2>Selected Fleet</h2>
        ${selectedFleet ? fleetDetail(selectedFleet, data) : `<div class="empty-state">Select a fleet to inspect members</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Fleets</h2>
        ${chip(`${data.fleets.length} configured`, "info")}
      </div>
      <div class="record-list">
        ${data.fleets.length ? data.fleets.map((fleet) => fleetRecord(fleet, data)).join("") : `<div class="empty-state">No fleets</div>`}
      </div>
    </section>
  `;
}

function renderMap(): string {
  const data = mustData();
  const activeTasks = data.tasks.filter((detail) => !TERMINAL_TASK_STATES.has(detail.task.state));
  const dependencyCount = data.tasks.reduce((sum, detail) => sum + (detail.task.dependencies || []).length, 0);
  return `
    <section class="metric-grid">
      ${metric("Topology Nodes", data.fleets.length + data.machines.length + data.agents.length + activeTasks.length, "fleets, machines, agents, active tasks")}
      ${metric("Dispatch Queue", data.dispatch.open_task_count || 0, "open tasks awaiting agents")}
      ${metric("Dependencies", dependencyCount, "task dependency edges")}
      ${metric("AgentBus", data.agentbus_streams.length, "recent streams")}
    </section>
    <section class="split map-split">
      <div class="surface">
        <div class="surface-heading">
          <h2>Fleet Relationship Map</h2>
          ${chip(state.selectedId || "nothing selected", state.selectedId ? "info" : "warn")}
        </div>
        ${relationshipGraph(data)}
      </div>
      <div class="surface">
        <h2>Selection</h2>
        ${topologySelectionDetail(data)}
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Dispatch Eligibility</h2>
        <div class="record-list">
          ${data.dispatch.tasks.length ? data.dispatch.tasks.slice(0, 20).map(dispatchRecord).join("") : `<div class="empty-state">No dispatch candidates</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Dependency Edges</h2>
        <div class="record-list">
          ${taskDependencyRecords(data)}
        </div>
      </div>
    </section>
  `;
}

function renderAgents(): string {
  const data = mustData();
  const agents = filteredAgents(data);
  const pageCount = Math.max(1, Math.ceil(agents.length / AGENT_PAGE_SIZE));
  if (state.agentPage > pageCount) state.agentPage = pageCount;
  const start = (state.agentPage - 1) * AGENT_PAGE_SIZE;
  const visible = agents.slice(start, start + AGENT_PAGE_SIZE);
  const visibleIds = visible.map((item) => item.agent.id);
  const writable = canWrite(data);
  return `
    <section class="metric-grid">
      ${metric("Agents", agents.length, `${data.agents.length} inventory rows`)}
      ${metric("Healthy", agents.filter((item) => item.agent.health_status === "healthy").length, "matching agents")}
      ${metric("Blocked", agents.filter((item) => !item.availability.eligible).length, "not dispatch eligible")}
      ${metric("Page", `${state.agentPage}/${pageCount}`, `${visible.length} rows shown`)}
    </section>
    <section class="toolbar">
      <input id="agentSearch" type="search" placeholder="Search agents, fleets, hosts, health, capabilities" value="${escapeHtml(state.agentQuery)}">
      <select id="agentFilter">
        ${option("all", "All agents", state.agentFilter)}
        ${option("eligible", "Eligible", state.agentFilter)}
        ${option("blocked", "Blocked", state.agentFilter)}
        ${option("idle", "Idle", state.agentFilter)}
        ${option("busy", "Busy", state.agentFilter)}
        ${option("draining", "Draining", state.agentFilter)}
        ${option("offline", "Offline", state.agentFilter)}
        ${option("degraded", "Degraded", state.agentFilter)}
        ${option("unhealthy", "Unhealthy", state.agentFilter)}
      </select>
      <select id="agentSort">
        ${option("name", "Sort by name", state.agentSort)}
        ${option("fleet", "Sort by fleet", state.agentSort)}
        ${option("status", "Sort by status", state.agentSort)}
        ${option("project", "Sort by project", state.agentSort)}
        ${option("capacity", "Sort by capacity", state.agentSort)}
        ${option("last_seen", "Sort by last seen", state.agentSort)}
      </select>
      <button type="button" id="clearAgentFilters">Clear</button>
      ${sessionAccessBadge(data)}
    </section>
    ${writable ? "" : `<section class="action-status">Read-only token: agent records can be inspected but not changed.</section>`}
    <details class="surface action-drawer">
      <summary>
        <span>Create Agent</span>
        <span class="muted small">Register capacity after the machine is known</span>
      </summary>
      <form class="action-form aligned-form" data-action="agentCreate">
        <label>Machine ${machineSelect("machine_id", data.machines, "", !writable)}</label>
        <label>Fleet ${fleetSelect("fleet_id", data.fleets, defaultFleetId(data), !writable)}</label>
        <label>Name <input name="name" required ${disabledAttr(!writable)}></label>
        <label>Agent ID <input name="agent_id" placeholder="agent_rocky" ${disabledAttr(!writable)}></label>
        <label>Hermes Instance ID <input name="hermes_instance_id" placeholder="hermes_rocky" ${disabledAttr(!writable)}></label>
        <label>Capabilities <input name="capabilities" placeholder="ops,python,hermes,review" ${disabledAttr(!writable)}></label>
        <label>Resources JSON <textarea name="resources" placeholder="{}" ${disabledAttr(!writable)}></textarea></label>
        <label>Actor <input name="actor" value="human" ${disabledAttr(!writable)}></label>
        <button type="submit" ${disabledAttr(!writable)}>Create</button>
      </form>
    </details>
    <section class="surface">
      <div class="surface-heading">
        <h2>Agent Resource Table</h2>
        ${chip(`${agents.length} matching`, "info")}
      </div>
      <form class="action-form compact" data-action="agentBulkUpdate">
        <input type="hidden" name="agent_ids" value="${escapeHtml(visibleIds.join(","))}">
        <label>Status <select name="status">${option("", "No status change", "")}${["idle", "draining", "offline"].map((value) => option(value, labelize(value), "")).join("")}</select></label>
        <label>Health <select name="health_status">${option("", "No health change", "")}${["healthy", "degraded", "unhealthy"].map((value) => option(value, labelize(value), "")).join("")}</select></label>
        <button type="submit">Apply To Visible</button>
      </form>
      ${agentTable(visible, data)}
      <div class="pager">
        <button type="button" id="agentPrevPage" ${state.agentPage <= 1 ? "disabled" : ""}>Previous</button>
        <span class="muted small">Rows ${agents.length ? start + 1 : 0}-${start + visible.length} of ${agents.length}</span>
        <button type="button" id="agentNextPage" ${state.agentPage >= pageCount ? "disabled" : ""}>Next</button>
      </div>
      ${agentInspector(data)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Project Cohorts</h2>
        ${swarmBuckets(data.swarm_summary.project)}
      </div>
      <div class="surface">
        <h2>Capability Footprint</h2>
        ${swarmBuckets(data.swarm_summary.capability)}
      </div>
    </section>
  `;
}

function renderTasks(): string {
  const data = mustData();
  const tasks = state.taskFilter === "all"
    ? data.tasks
    : data.tasks.filter((detail) => detail.task.state === state.taskFilter);
  return `
    <section class="toolbar">
      <select id="taskFilter">
        ${option("all", "All states", state.taskFilter)}
        ${TASK_STATES.map((taskState) => option(taskState, labelize(taskState), state.taskFilter)).join("")}
      </select>
      <button type="button" id="clearTaskFilter">Clear</button>
    </section>
    <details class="surface action-drawer">
      <summary>
        <span>New Task</span>
        <span class="muted small">Create work only when the queue needs a human-authored item</span>
      </summary>
      <form class="action-form" data-action="taskCreate">
        <label>Title <input name="title" required></label>
        <label>Description <textarea name="description"></textarea></label>
        <label>Project <input name="project" value="${escapeHtml(state.projectFilter === "all" ? "" : state.projectFilter)}"></label>
        <label>Priority <input name="priority" type="number" value="0"></label>
        <label>Capabilities <input name="required_capabilities" placeholder="python,deploy"></label>
        <label>Dependencies <input name="dependencies" placeholder="task_a,task_b"></label>
        <label>Metadata JSON <textarea name="metadata" placeholder="{}"></textarea></label>
        <button type="submit">Create</button>
      </form>
    </details>
    <section class="task-lanes">
      ${TASK_STATES.filter((taskState) => state.taskFilter === "all" || state.taskFilter === taskState)
        .map((taskState) => taskLane(taskState, tasks, data.agents))
        .join("")}
    </section>
    ${taskInspector(tasks, data)}
  `;
}

function renderWorkflows(): string {
  const data = mustData();
  const running = Number(data.workflow_runs.counts?.running || 0);
  const pendingDrafts = data.workflow_drafts.filter((draft) => draft.status !== "compiled" && draft.status !== "cancelled");
  // wf-05: pick which workflow's graph + inspector to render. Default to
  // the first definition (legacy behavior) but let the operator switch.
  const selectedId = state.selectedWorkflowId || (data.workflows[0]?.id as string | undefined) || "";
  const selectedWorkflow = data.workflows.find((wf) => String(wf.id) === String(selectedId)) || data.workflows[0];
  return `
    <section class="workflow-planner-console">
      <div class="surface">
        <div class="surface-heading">
          <h2>Plan Workflow</h2>
          ${chip("model draft", "info")}
        </div>
        ${workflowPlanPromptForm(data)}
      </div>
      <div class="surface">
        <div class="surface-heading">
          <h2>Proposed Task Graph</h2>
          ${state.workflowPlanDraft ? chip(`${state.workflowPlanDraft.nodes.length} proposed`, "info") : chip("no draft", "warn")}
        </div>
        ${workflowPlanDraftPanel()}
      </div>
    </section>
    <section class="metric-grid">
      ${metric("Definitions", data.workflows.length, `${data.workflow_runs.total || 0} total runs`)}
      ${metric("Running", running, "active workflow runs")}
      ${metric("Drafts", data.workflow_drafts.length, `${pendingDrafts.length} pending`)}
      ${metric("Notifier Channels", data.notifier_channels.length, "task progress sinks")}
    </section>
    <section class="split workflow-stage">
      <div class="surface">
        <div class="surface-header">
          <h2>Workflow Graph</h2>
          ${workflowSelector(data.workflows, selectedWorkflow?.id)}
        </div>
        ${workflowLegend()}
        ${workflowGraph(selectedWorkflow)}
        ${workflowInspector(selectedWorkflow)}
      </div>
      <div class="surface">
        <h2>Definition Draft Builder</h2>
        ${draftCreationForm()}
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Workflows</h2>
        <div class="record-list">
          ${data.workflows.length ? data.workflows.map(workflowRecord).join("") : `<div class="empty-state">No workflows</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Drafts</h2>
        <div class="record-list">
          ${data.workflow_drafts.length ? data.workflow_drafts.map(workflowDraftRecord).join("") : `<div class="empty-state">No workflow drafts</div>`}
        </div>
      </div>
    </section>
    <section class="surface">
      <h2>Notifier Channels</h2>
      <form class="action-form compact" data-action="notifierConfigure">
        <label>Name <input name="name" placeholder="ops-slack"></label>
        <label>Type ${select("channel_type", ["slack", "telegram", "hermes"], "slack")}</label>
        <label>Events <input name="event_types" value="task.*"></label>
        <label>Target JSON <textarea name="target" placeholder='{"platform":"slack"}'></textarea></label>
        <button type="submit">Save Channel</button>
      </form>
      <form class="action-form compact" data-action="notifierDeliver">
        <label>Limit <input name="limit" type="number" min="1" value="50"></label>
        <button type="submit">Deliver Pending</button>
      </form>
      <div class="record-list">
        ${data.notifier_channels.length ? data.notifier_channels.map(notifierChannelRecord).join("") : `<div class="empty-state">No notifier channels</div>`}
      </div>
    </section>
  `;
}

function workflowPlanPromptForm(data: DashboardData): string {
  const writable = canWrite(data);
  const disabled = disabledAttr(!writable);
  const projects = projectFilterOptions(data);
  const defaultProject = state.projectFilter === "all" ? (projects[0]?.project || "") : state.projectFilter;
  const projectOptions = projects.map((project) => option(project.project, project.project, defaultProject)).join("");
  return `
    <form class="action-form workflow-plan-form" data-action="workflowPlanPreview">
      <label class="field-full">Task Request <textarea name="goal" rows="5" required ${disabled}></textarea></label>
      <label>Project
        <select name="project" ${disabled}>
          ${option("", "No project", defaultProject)}
          ${projectOptions}
        </select>
      </label>
      <label>Capabilities <input name="required_capabilities" placeholder="ops,python,review" ${disabled}></label>
      <label>Max Tasks <input name="max_tasks" type="number" min="1" max="20" value="6" ${disabled}></label>
      <label>Model <input name="model" value="*" ${disabled}></label>
      <label class="field-full">Planning Prompt <textarea name="prompt" rows="4" placeholder="Constraints, acceptance criteria, sequencing, or preferred roles" ${disabled}></textarea></label>
      <div class="field-full form-actions"><button type="submit" ${disabled}>Generate Plan</button></div>
    </form>
  `;
}

function workflowPlanDraftPanel(): string {
  const draft = state.workflowPlanDraft;
  if (!draft) return `<div class="empty-state">No proposed task graph</div>`;
  return `
    <div class="workflow-plan-draft" data-workflow-plan-draft>
      <div class="row-grid compact-grid">
        ${field("Plan", draft.plan_id)}
        ${field("Project", draft.project || "none")}
        ${field("Source", draft.source || "model")}
        ${field("Goal", truncate(draft.goal, 120))}
      </div>
      ${workflowPlanDraftGraph(draft)}
      <div class="workflow-plan-editor">
        ${draft.nodes.map((node, index) => workflowPlanNodeEditor(node, index, draft.nodes.length)).join("")}
      </div>
      <div class="workflow-plan-actions">
        <button type="button" data-action="workflowPlanNodeAdd">Add Task</button>
        <button type="button" class="primary-button" data-action="workflowPlanAccept">Accept</button>
        <button type="button" class="secondary-button" data-action="workflowPlanCancel">Cancel</button>
      </div>
    </div>
  `;
}

function workflowPlanDraftGraph(draft: WorkflowPlanDraft): string {
  const graphNodes = draft.nodes.map((node) => ({
    node_key: node.node_id,
    node_type: "task",
    role_required: node.required_capabilities.join(","),
  })) as JsonObject[];
  const edges = draft.nodes.flatMap((node) =>
    node.depends_on
      .filter((dep) => draft.nodes.some((candidate) => candidate.node_id === dep))
      .map((dep) => ({
        from_node_key: dep,
        to_node_key: node.node_id,
        condition: "success",
      })),
  ) as JsonObject[];
  if (!graphNodes.length) return `<div class="empty-state">No proposed nodes</div>`;
  const positions = layoutWorkflowNodes(graphNodes, edges);
  let maxX = 0;
  let maxY = 0;
  positions.forEach((pos) => {
    if (pos.x > maxX) maxX = pos.x;
    if (pos.y > maxY) maxY = pos.y;
  });
  const width = Math.max(560, maxX + 140);
  const height = Math.max(180, maxY + 80);
  const edgeSvg = edges.map((edge) => {
    const from = positions.get(String(edge.from_node_key || ""));
    const to = positions.get(String(edge.to_node_key || ""));
    if (!from || !to) return "";
    return `<path class="graph-edge graph-edge-success" d="M${from.x + 82},${from.y} C${from.x + 150},${from.y} ${to.x - 150},${to.y} ${to.x - 82},${to.y}"></path>`;
  }).join("");
  const nodeSvg = graphNodes.map((node) => {
    const key = String(node.node_key);
    const pos = positions.get(key) || { x: 120, y: 60 };
    return `
      <g class="graph-node graph-node-task" transform="translate(${pos.x},${pos.y})">
        <rect x="-86" y="-24" width="172" height="48" rx="8"></rect>
        ${workflowNodeIcon("task")}
        <text text-anchor="middle" y="-3" x="14">${escapeHtml(truncate(key, 18))}</text>
        <text class="graph-column-label" text-anchor="middle" y="15" x="14">${escapeHtml(truncate(String(node.role_required || ""), 18))}</text>
      </g>
    `;
  }).join("");
  return `
    <div class="graph-wrap workflow-plan-graph-wrap">
      <svg class="relationship-graph workflow-graph" viewBox="0 0 ${width} ${height}" role="img" aria-label="Proposed task graph">
        ${edgeSvg}
        ${nodeSvg}
      </svg>
    </div>
  `;
}

function workflowPlanNodeEditor(node: WorkflowPlanNode, index: number, total: number): string {
  return `
    <article class="workflow-plan-node" data-plan-node-id="${escapeHtml(node.node_id)}">
      <div class="record-header">
        <div>
          <h3>${escapeHtml(node.title || node.node_id)}</h3>
          <p class="muted small mono">${escapeHtml(node.node_id)}</p>
        </div>
        <div class="table-actions">
          <button type="button" data-action="workflowPlanNodeMove" data-direction="up" ${index <= 0 ? "disabled" : ""}>Up</button>
          <button type="button" data-action="workflowPlanNodeMove" data-direction="down" ${index >= total - 1 ? "disabled" : ""}>Down</button>
          <button type="button" class="danger-button" data-action="workflowPlanNodeDelete">Delete</button>
        </div>
      </div>
      <div class="action-form workflow-plan-node-form">
        <label>Node ID <input data-plan-field="node_id" value="${escapeHtml(node.node_id)}"></label>
        <label>Priority <input data-plan-field="priority" type="number" value="${escapeHtml(node.priority || 0)}"></label>
        <label>Capabilities <input data-plan-field="required_capabilities" value="${escapeHtml(node.required_capabilities.join(","))}"></label>
        <label>Depends On <input data-plan-field="depends_on" value="${escapeHtml(node.depends_on.join(","))}"></label>
        <label class="field-full">Title <input data-plan-field="title" value="${escapeHtml(node.title)}"></label>
        <label class="field-full">Description <textarea data-plan-field="description" rows="4">${escapeHtml(node.description)}</textarea></label>
      </div>
    </article>
  `;
}

function normalizeWorkflowPlanDraft(value: unknown): WorkflowPlanDraft {
  const record = value && typeof value === "object" ? value as JsonObject : {};
  const rawNodes = Array.isArray(record.nodes) ? record.nodes : [];
  const nodes = rawNodes
    .filter((item): item is JsonObject => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item, index) => normalizeWorkflowPlanNode(item, index + 1));
  if (!nodes.length) throw new Error("workflow planner returned no proposed tasks");
  return {
    schema: String(record.schema || "mac.dashboard.workflow_plan.v1"),
    plan_id: String(record.plan_id || workflowPlanLocalId(String(record.goal || ""), nodes)),
    goal: String(record.goal || ""),
    project: emptyToNull(record.project),
    source: String(record.source || "model"),
    created_at: String(record.created_at || ""),
    nodes,
  };
}

function normalizeWorkflowPlanNode(value: JsonObject, index: number): WorkflowPlanNode {
  const nodeId = String(value.node_id || value.id || value.key || `task_${index}`).trim() || `task_${index}`;
  const title = String(value.title || value.name || `Task ${index}`).trim() || `Task ${index}`;
  const metadata = value.metadata && typeof value.metadata === "object" && !Array.isArray(value.metadata)
    ? value.metadata as JsonObject
    : {};
  return {
    node_id: nodeId,
    title,
    description: String(value.description || value.summary || ""),
    required_capabilities: listValue(value.required_capabilities || value.capabilities),
    depends_on: listValue(value.depends_on || value.dependencies || value.parents),
    priority: numberValue(value.priority, 0),
    metadata,
  };
}

function listValue(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
  return csvList(value);
}

function workflowPlanLocalId(goal: string, nodes: WorkflowPlanNode[]): string {
  const seed = `${goal}:${nodes.map((node) => node.node_id).join(",")}`;
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = ((hash << 5) - hash + seed.charCodeAt(i)) | 0;
  }
  return `plan_${Math.abs(hash).toString(16)}`;
}

function syncWorkflowPlanDraftFromDom(): WorkflowPlanDraft | null {
  const draft = state.workflowPlanDraft;
  const root = nodes.content.querySelector<HTMLElement>("[data-workflow-plan-draft]");
  if (!draft || !root) return draft;
  const editedNodes = Array.from(root.querySelectorAll<HTMLElement>("[data-plan-node-id]")).map((row, index) => {
    const current = draft.nodes[index] || {
      node_id: `task_${index + 1}`,
      title: `Task ${index + 1}`,
      description: "",
      required_capabilities: [],
      depends_on: [],
      priority: 0,
      metadata: {},
    };
    const field = (name: string): string => {
      const control = row.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[data-plan-field='${name}']`);
      return control ? control.value : "";
    };
    return {
      ...current,
      node_id: field("node_id").trim() || current.node_id,
      title: field("title").trim() || current.title,
      description: field("description"),
      required_capabilities: csvList(field("required_capabilities")),
      depends_on: csvList(field("depends_on")),
      priority: numberValue(field("priority"), current.priority || 0),
    };
  });
  state.workflowPlanDraft = { ...draft, nodes: normalizeWorkflowPlanDependencies(editedNodes) };
  return state.workflowPlanDraft;
}

function normalizeWorkflowPlanDependencies(planNodes: WorkflowPlanNode[]): WorkflowPlanNode[] {
  const ids = new Set(planNodes.map((node) => node.node_id));
  return planNodes.map((node) => ({
    ...node,
    depends_on: node.depends_on.filter((dep) => ids.has(dep)),
  }));
}

function workflowPlanNodeIndex(button: HTMLElement): number {
  const row = button.closest<HTMLElement>("[data-plan-node-id]");
  if (!row) return -1;
  const rows = Array.from(nodes.content.querySelectorAll<HTMLElement>("[data-plan-node-id]"));
  return rows.indexOf(row);
}

function addWorkflowPlanNode(): void {
  const draft = syncWorkflowPlanDraftFromDom();
  if (!draft) return;
  const used = new Set(draft.nodes.map((node) => node.node_id));
  let index = draft.nodes.length + 1;
  let nodeId = `task_${index}`;
  while (used.has(nodeId)) {
    index += 1;
    nodeId = `task_${index}`;
  }
  state.workflowPlanDraft = {
    ...draft,
    nodes: [
      ...draft.nodes,
      {
        node_id: nodeId,
        title: `Task ${index}`,
        description: "",
        required_capabilities: [],
        depends_on: [],
        priority: 0,
        metadata: {},
      },
    ],
  };
  render();
}

function deleteWorkflowPlanNode(button: HTMLElement): void {
  const draft = syncWorkflowPlanDraftFromDom();
  const index = workflowPlanNodeIndex(button);
  if (!draft || index < 0) return;
  const removed = draft.nodes[index]?.node_id;
  state.workflowPlanDraft = {
    ...draft,
    nodes: draft.nodes
      .filter((_, candidateIndex) => candidateIndex !== index)
      .map((node) => ({ ...node, depends_on: node.depends_on.filter((dep) => dep !== removed) })),
  };
  render();
}

function moveWorkflowPlanNode(button: HTMLElement): void {
  const draft = syncWorkflowPlanDraftFromDom();
  const index = workflowPlanNodeIndex(button);
  if (!draft || index < 0) return;
  const direction = button.dataset.direction === "down" ? 1 : -1;
  const nextIndex = index + direction;
  if (nextIndex < 0 || nextIndex >= draft.nodes.length) return;
  const nextNodes = [...draft.nodes];
  const [node] = nextNodes.splice(index, 1);
  nextNodes.splice(nextIndex, 0, node);
  state.workflowPlanDraft = { ...draft, nodes: nextNodes };
  render();
}

function cancelWorkflowPlan(): void {
  state.workflowPlanDraft = null;
  state.actionMessage = "Workflow plan cancelled";
  render();
}

async function acceptWorkflowPlan(button: HTMLButtonElement): Promise<void> {
  const draft = syncWorkflowPlanDraftFromDom();
  if (!draft) return;
  if (!draft.nodes.length) {
    state.actionMessage = "Workflow plan accept failed: no proposed tasks";
    render();
    return;
  }
  button.disabled = true;
  try {
    const result = await postJSON("/dashboard/workflow-plan/accept", {
      goal: draft.goal,
      project: draft.project || null,
      plan_id: draft.plan_id,
      nodes: draft.nodes,
      actor: "human",
      metadata: { source: "dashboard_workflow_planner" },
    });
    state.workflowPlanDraft = null;
    state.actionMessage = actionSuccessMessage("workflowPlanAccept", result);
    await loadDashboard();
  } catch (error) {
    state.actionMessage = `Workflow plan accept failed: ${error instanceof Error ? error.message : String(error)}`;
    render();
  } finally {
    button.disabled = false;
  }
}

function workflowPlanningTaskForm(data: DashboardData): string {
  const writable = canWrite(data);
  const disabled = disabledAttr(!writable);
  const defaultProject = state.projectFilter === "all" ? "" : state.projectFilter;
  const metadata = JSON.stringify(
    {
      origin: { type: "dashboard_workflow_planning" },
      workflow: {
        type: "task_chain",
        role: "planning",
        status: "planning",
      },
    },
    null,
    2,
  );
  return `
    <form class="action-form workflow-planning-form" data-action="workflowPlanningTaskCreate">
      <label class="field-full">Goal <textarea name="goal" rows="4" required ${disabled}></textarea></label>
      <label>Project <input name="project" value="${escapeHtml(defaultProject)}" ${disabled}></label>
      <label>Priority <input name="priority" type="number" value="0" ${disabled}></label>
      <label>Capabilities <input name="required_capabilities" placeholder="leave blank for any agent" ${disabled}></label>
      <label>Title <input name="title" placeholder="Plan workflow: ..." ${disabled}></label>
      <label class="field-full">Planning Instructions <textarea name="description" rows="4" placeholder="What should the planning task decompose into child tasks?" ${disabled}></textarea></label>
      <label class="field-full">Metadata JSON <textarea class="json-editor" name="metadata" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(metadata)}</textarea></label>
      <div class="field-full form-actions"><button type="submit" ${disabled}>Create Planning Task</button></div>
    </form>
  `;
}

function selectedWorkflowChainTask(data: DashboardData): TaskDetail | null {
  const selected = selectedTaskDetail(data);
  if (selected) return selected;
  return data.tasks.find((detail) => {
    const workflow = detail.task.metadata?.workflow as JsonObject | undefined;
    return workflow && String(workflow.type || "") === "task_chain";
  }) || data.tasks.find((detail) => !TERMINAL_TASK_STATES.has(detail.task.state)) || data.tasks[0] || null;
}

function workflowTaskChainPanel(data: DashboardData, detail: TaskDetail | null): string {
  if (!detail) {
    return `<div class="empty-state">Create a planning task to start a task-chain workflow.</div>`;
  }
  const task = detail.task;
  const writable = canWrite(data);
  const disabled = disabledAttr(!writable || TERMINAL_TASK_STATES.has(task.state));
  const relationships = (task.metadata?.relationships as JsonObject | undefined) || {};
  const childIds = Array.isArray(relationships.child_task_ids)
    ? relationships.child_task_ids.map(String)
    : [];
  const parentId = relationships.parent_task_id ? String(relationships.parent_task_id) : "";
  const children = childIds
    .map((id) => taskDetailById(data, id)?.task)
    .filter((child): child is TaskRecord => !!child);
  const taskOptions = data.tasks
    .slice(0, 120)
    .map((candidate) => {
      const label = `${candidate.task.title} (${candidate.task.state})`;
      return option(candidate.task.id, label, task.id);
    })
    .join("");
  const metadata = JSON.stringify(
    {
      origin: { type: "dashboard_workflow_chain", parent_task_id: task.id },
      workflow: {
        type: "task_chain",
        role: "step",
        parent_task_id: task.id,
      },
    },
    null,
    2,
  );
  return `
    <div class="workflow-chain-panel">
      <label class="workflow-task-selector">
        <span class="muted small">Selected task</span>
        <select data-action="workflowTaskSelect">${taskOptions}</select>
      </label>
      <div class="row-grid compact-grid">
        ${field("Task", task.title)}
        ${field("Project", taskProject(task))}
        ${field("Parent", parentId || "none")}
        ${field("Children", childIds.length)}
      </div>
      <div class="workflow-chain-list">
        <div class="workflow-chain-node is-current">
          <strong>${escapeHtml(task.title)}</strong>
          <span class="muted small mono">${escapeHtml(task.id)}</span>
        </div>
        ${children.map((child) => `
          <button class="workflow-chain-node" type="button" data-task-open="${escapeHtml(child.id)}">
            <strong>${escapeHtml(child.title)}</strong>
            <span class="muted small">${escapeHtml(child.state)}</span>
          </button>
        `).join("")}
      </div>
      <form class="action-form workflow-chain-form" data-action="workflowChainTaskAdd" data-task-id="${escapeHtml(task.id)}">
        <label>Next task title <input name="title" required ${disabled}></label>
        <label>Project <input name="project" value="${escapeHtml(taskProject(task))}" ${disabled}></label>
        <label>Priority <input name="priority" type="number" value="${escapeHtml(task.priority || 0)}" ${disabled}></label>
        <label>Capabilities <input name="required_capabilities" value="${escapeHtml((task.required_capabilities || []).join(","))}" ${disabled}></label>
        <label class="field-full">Description <textarea name="description" rows="4" ${disabled}></textarea></label>
        <label class="field-full">Metadata JSON <textarea class="json-editor" name="metadata" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(metadata)}</textarea></label>
        <div class="field-full form-actions"><button type="submit" ${disabled}>Add Task To Chain</button></div>
      </form>
    </div>
  `;
}

function workflowRecord(workflow: ApiRecord): string {
  return `
    <article class="record compact ${selectedClass(String(workflow.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(workflow.name || workflow.slug || workflow.id)}</h3><p class="muted small mono">${escapeHtml(workflow.id)}</p></div>
        <div class="chip-row">${chip(`v${workflow.version || 1}`, "info")}${chip(workflow.workflow_type || "workflow", "good")}</div>
      </div>
      <div class="row-grid compact-grid">
        ${field("Slug", workflow.slug)}
        ${field("Tenant", workflow.tenant_id || "global")}
        ${field("Nodes", ((workflow.definition as JsonObject | undefined)?.nodes as unknown[] | undefined)?.length || 0)}
        ${field("Enabled", workflow.enabled ? "yes" : "no")}
      </div>
      <form class="action-form compact" data-action="workflowPreview" data-workflow-id="${escapeHtml(workflow.id)}">
        <label>Input JSON <textarea name="input" placeholder="{}"></textarea></label>
        <button type="submit">Preview</button>
      </form>
      <form class="action-form compact" data-action="workflowStart" data-workflow-id="${escapeHtml(workflow.id)}">
        <label>Started by <input name="started_by" value="human"></label>
        <label>Input JSON <textarea name="input" placeholder="{}"></textarea></label>
        <button type="submit">Start</button>
      </form>
    </article>
  `;
}

function workflowDraftRecord(draft: WorkflowDraftRecord): string {
  return `
    <article class="record compact ${selectedClass(String(draft.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(draft.goal)}</h3><p class="muted small mono">${escapeHtml(draft.id)}</p></div>
        ${chip(draft.status, draft.status === "compiled" ? "good" : "warn")}
      </div>
      <div class="row-grid compact-grid">
        ${field("Steps", draft.proposed_steps?.length || 0)}
        ${field("Questions", draft.questions?.length || 0)}
        ${field("Compiled", draft.compiled_workflow_id || "none")}
        ${field("Updated", formatAge(draft.updated_at))}
      </div>
      <form class="action-form compact" data-action="workflowDraftPreview" data-draft-id="${escapeHtml(draft.id)}">
        <label>Input JSON <textarea name="input" placeholder="{}"></textarea></label>
        <button type="submit">Preview</button>
      </form>
      <form class="action-form compact" data-action="workflowDraftApprove" data-draft-id="${escapeHtml(draft.id)}">
        <label>Slug <input name="slug" value="${escapeHtml(String(draft.goal || draft.id).toLowerCase().replaceAll(" ", "-").replace(/[^a-z0-9-]/g, ""))}"></label>
        <label>Name <input name="name" value="${escapeHtml(draft.goal)}"></label>
        <button type="submit">Approve</button>
      </form>
    </article>
  `;
}

function workflowSelector(workflows: ApiRecord[], selectedId: unknown): string {
  if (!workflows.length) return "";
  const current = String(selectedId || workflows[0]?.id || "");
  const options = workflows
    .map(
      (wf) =>
        `<option value="${escapeHtml(String(wf.id))}"${
          String(wf.id) === current ? " selected" : ""
        }>${escapeHtml(String(wf.name || wf.slug || wf.id))}</option>`,
    )
    .join("");
  return `
    <label class="workflow-selector">
      <span class="muted small">Workflow</span>
      <select data-action="workflowGraphSelect">${options}</select>
    </label>
  `;
}

const WORKFLOW_NODE_LEGEND: { type: string; label: string }[] = [
  { type: "task", label: "Task" },
  { type: "approval", label: "Approval (needs human)" },
  { type: "plan", label: "Plan" },
  { type: "commit", label: "Commit" },
  { type: "verify", label: "Verify" },
];

function workflowLegend(): string {
  const items = WORKFLOW_NODE_LEGEND.map(
    ({ type, label }) =>
      `<span class="workflow-legend-item workflow-legend-${type}"><span class="workflow-legend-swatch"></span>${escapeHtml(
        label,
      )}</span>`,
  ).join("");
  return `<div class="workflow-legend">${items}</div>`;
}

// Layered DAG layout: rank(node) = 1 + max(rank(predecessor)); start nodes
// rank=0. Within a rank, distribute vertically. Beats the modulo-3 grid
// for everything but the trivial linear case and still works there.
function layoutWorkflowNodes(
  nodes: JsonObject[],
  edges: JsonObject[],
): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  if (!nodes.length) return positions;
  const ranks = new Map<string, number>();
  nodes.forEach((node) => ranks.set(String(node.node_key), 0));
  // Iterate until stable (small graphs converge in <= node-count passes).
  for (let pass = 0; pass < nodes.length + 1; pass++) {
    let changed = false;
    edges.forEach((edge) => {
      const from = String(edge.from_node_key || "");
      const to = String(edge.to_node_key || "");
      if (!from || !to) return;
      const fromRank = ranks.get(from);
      const toRank = ranks.get(to);
      if (fromRank === undefined || toRank === undefined) return;
      const want = fromRank + 1;
      if (want > toRank) {
        ranks.set(to, want);
        changed = true;
      }
    });
    if (!changed) break;
  }
  const byRank = new Map<number, string[]>();
  nodes.forEach((node) => {
    const key = String(node.node_key);
    const rank = ranks.get(key) ?? 0;
    if (!byRank.has(rank)) byRank.set(rank, []);
    byRank.get(rank)!.push(key);
  });
  const colWidth = 200;
  const rowHeight = 90;
  const xOrigin = 120;
  const yOrigin = 60;
  byRank.forEach((keys, rank) => {
    keys.forEach((key, row) => {
      positions.set(key, {
        x: xOrigin + rank * colWidth,
        y: yOrigin + row * rowHeight,
      });
    });
  });
  return positions;
}

function workflowGraph(workflow: ApiRecord | undefined): string {
  const definition = workflow?.definition as JsonObject | undefined;
  const nodes = (definition?.nodes as JsonObject[] | undefined) || [];
  const edges = (definition?.edges as JsonObject[] | undefined) || [];
  if (!workflow || !nodes.length) return `<div class="empty-state">No workflow graph</div>`;
  const positions = layoutWorkflowNodes(nodes, edges);
  let maxX = 0;
  let maxY = 0;
  positions.forEach((pos) => {
    if (pos.x > maxX) maxX = pos.x;
    if (pos.y > maxY) maxY = pos.y;
  });
  const width = Math.max(560, maxX + 140);
  const height = Math.max(180, maxY + 80);
  const edgeSvg = edges
    .map((edge) => {
      const from = positions.get(String(edge.from_node_key || ""));
      const to = positions.get(String(edge.to_node_key || ""));
      if (!from || !to) return "";
      const condition = String(edge.condition || "success");
      const edgeClass = `graph-edge graph-edge-${condition}`;
      return `<path class="${edgeClass}" d="M${from.x + 82},${from.y} C${
        from.x + 150
      },${from.y} ${to.x - 150},${to.y} ${to.x - 82},${to.y}"></path>`;
    })
    .join("");
  const selectedNodeKey = state.selectedNodeKey || "";
  const nodeSvg = nodes
    .map((node) => {
      const key = String(node.node_key);
      const type = String(node.node_type || "task").toLowerCase();
      const pos = positions.get(key) || { x: 120, y: 60 };
      const selected = key === selectedNodeKey ? " is-selected" : "";
      const pressed = key === selectedNodeKey ? "true" : "false";
      return `
        <g class="graph-node graph-node-${type}${selected}" transform="translate(${pos.x},${pos.y})"
           data-action="workflowNodeOpen" data-node-key="${escapeHtml(key)}"
           tabindex="0" role="button" aria-pressed="${pressed}"
           aria-label="${escapeHtml(`${key} (${type})`)}">
          <rect x="-86" y="-24" width="172" height="48" rx="8"></rect>
          ${workflowNodeIcon(type)}
          <text text-anchor="middle" y="-3" x="14">${escapeHtml(truncate(key, 18))}</text>
          <text class="graph-column-label" text-anchor="middle" y="15" x="14">${escapeHtml(
            truncate(String(node.role_required || ""), 18),
          )}</text>
        </g>
      `;
    })
    .join("");
  return `
    <div class="graph-wrap workflow-graph-wrap">
      <svg class="relationship-graph workflow-graph" viewBox="0 0 ${width} ${height}" role="img" aria-label="Workflow graph">
        ${edgeSvg}
        ${nodeSvg}
      </svg>
    </div>
    <div class="mobile-card-list graph-mobile-list">
      ${nodes.map((node) => `
        <button class="mobile-object-card compact ${state.selectedNodeKey === String(node.node_key) ? "is-selected" : ""}" type="button" data-action="workflowNodeOpen" data-node-key="${escapeHtml(String(node.node_key))}">
          <span><strong>${escapeHtml(String(node.node_key))}</strong></span>
          <span class="muted small">${escapeHtml(String(node.node_type || "task"))} / ${escapeHtml(String(node.role_required || "any role"))}</span>
        </button>
      `).join("")}
    </div>
  `;
}

function workflowNodeIcon(nodeType: string): string {
  // Small inline SVG glyph per node type, anchored at left of the rect.
  const cx = -64;
  const cy = 0;
  const r = 8;
  const stroke = "currentColor";
  switch (nodeType) {
    case "approval":
      return `<g class="graph-node-icon"><circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${stroke}" stroke-width="2"></circle><path d="M${cx - 3},${cy} l3,3 l5,-6" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></g>`;
    case "plan":
      return `<g class="graph-node-icon"><rect x="${cx - 6}" y="${cy - 6}" width="12" height="12" rx="2" fill="none" stroke="${stroke}" stroke-width="2"></rect><line x1="${cx - 3}" y1="${cy - 2}" x2="${cx + 3}" y2="${cy - 2}" stroke="${stroke}" stroke-width="1.5"></line><line x1="${cx - 3}" y1="${cy + 1}" x2="${cx + 3}" y2="${cy + 1}" stroke="${stroke}" stroke-width="1.5"></line><line x1="${cx - 3}" y1="${cy + 4}" x2="${cx + 1}" y2="${cy + 4}" stroke="${stroke}" stroke-width="1.5"></line></g>`;
    case "verify":
      return `<g class="graph-node-icon"><path d="M${cx - 5},${cy} l4,4 l6,-8" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></g>`;
    case "commit":
      return `<g class="graph-node-icon"><circle cx="${cx}" cy="${cy}" r="4" fill="none" stroke="${stroke}" stroke-width="2"></circle><line x1="${cx - 9}" y1="${cy}" x2="${cx - 4}" y2="${cy}" stroke="${stroke}" stroke-width="2"></line><line x1="${cx + 4}" y1="${cy}" x2="${cx + 9}" y2="${cy}" stroke="${stroke}" stroke-width="2"></line></g>`;
    default:
      return `<g class="graph-node-icon"><circle cx="${cx}" cy="${cy}" r="3" fill="${stroke}"></circle></g>`;
  }
}

function workflowInspector(workflow: ApiRecord | undefined): string {
  if (!workflow) return "";
  const selectedKey = state.selectedNodeKey;
  if (!selectedKey) {
    return `
      <aside class="workflow-inspector workflow-inspector-empty">
        <p class="muted small">Click a node to inspect it.</p>
      </aside>
    `;
  }
  const definition = workflow.definition as JsonObject | undefined;
  const nodes = (definition?.nodes as JsonObject[] | undefined) || [];
  const node = nodes.find((n) => String(n.node_key) === selectedKey);
  if (!node) {
    return `
      <aside class="workflow-inspector">
        <header class="workflow-inspector-header">
          <h3>Node not found</h3>
          <button type="button" data-action="workflowNodeClose" aria-label="Close inspector">×</button>
        </header>
      </aside>
    `;
  }
  const type = String(node.node_type || "task").toLowerCase();
  const instructions = String(node.instructions || "(no instructions)");
  const questions = workflowDraftQuestionsForNode(workflow, String(node.node_key));
  const questionFields = questions.length
    ? `
      <section class="workflow-inspector-section">
        <h4>Questions bound to this node (wf-01)</h4>
        ${questions
          .map(
            (q) => `
            <div class="workflow-question">
              <p class="workflow-question-text">${escapeHtml(q.text)}${
              q.required ? ' <span class="chip warn">required</span>' : ""
            }</p>
              <p class="muted small mono">id: ${escapeHtml(q.id)}${
              q.binds_to_param ? ` → ${escapeHtml(q.binds_to_param)}` : ""
            }</p>
            </div>
          `,
          )
          .join("")}
      </section>
    `
    : "";
  return `
    <aside class="workflow-inspector workflow-inspector-${type} is-open">
      <header class="workflow-inspector-header">
        <div>
          <span class="chip workflow-inspector-type-chip workflow-legend-${type}">${escapeHtml(type)}</span>
          <h3>${escapeHtml(String(node.node_key))}</h3>
        </div>
        <button type="button" data-action="workflowNodeClose" aria-label="Close inspector">×</button>
      </header>
      <div class="row-grid compact-grid">
        ${field("Role", String(node.role_required || ""))}
        ${field("Max attempts", String(node.max_attempts || 1))}
        ${field("Timeout (min)", String(node.timeout_minutes || 0))}
        ${field("Persona", String(node.persona_hint || "(default)"))}
      </div>
      <section class="workflow-inspector-section">
        <h4>Instructions</h4>
        <pre class="workflow-instructions">${escapeHtml(instructions)}</pre>
      </section>
      ${questionFields}
    </aside>
  `;
}

function workflowDraftQuestionsForNode(
  workflow: ApiRecord,
  nodeKey: string,
): { id: string; text: string; required: boolean; binds_to_param?: string }[] {
  // Compiled definitions stash the draft's normalized questions in
  // definition.metadata.questions (wf-02 wired this in definition_from_draft).
  const definition = workflow.definition as JsonObject | undefined;
  const meta = (definition?.metadata as JsonObject | undefined) || {};
  const questions = (meta.questions as JsonObject[] | undefined) || [];
  return questions
    .filter((q) => String(q.binds_to_node || "") === nodeKey)
    .map((q) => ({
      id: String(q.id || ""),
      text: String(q.text || ""),
      required: Boolean(q.required),
      binds_to_param: q.binds_to_param ? String(q.binds_to_param) : undefined,
    }));
}

function draftCreationForm(): string {
  // wf-05 part 3: stepped form with repeating-row editors for Steps
  // and Questions, and auto-rendered Answer inputs for required
  // questions. Hidden `proposed_steps` / `questions` / `answers`
  // textareas are populated by JS at submit time so the existing
  // runAction("workflowDraftCreate") handler reads them unchanged.
  // The whole form keeps the JSON-tape escape hatch under a "Raw JSON"
  // details block so power users (and AI agents) can paste full
  // payloads if a row editor doesn't fit their case.
  return `
    <form class="action-form workflow-draft-form" data-action="workflowDraftCreate" data-draft-builder>
      <label>Goal <textarea name="goal" required></textarea></label>

      <fieldset class="workflow-draft-rows" data-rows="steps">
        <legend>Steps</legend>
        <div class="workflow-draft-row" data-row>
          <div class="row-grid compact-grid">
            <label>Key <input name="step_node_key" placeholder="step_1"></label>
            <label>Role <input name="step_role_required" placeholder="dev"></label>
            <label>Type
              <select name="step_node_type">
                <option value="task" selected>task</option>
                <option value="approval">approval</option>
                <option value="plan">plan</option>
                <option value="commit">commit</option>
                <option value="verify">verify</option>
              </select>
            </label>
          </div>
          <label>Instructions <textarea name="step_instructions" rows="2"></textarea></label>
          <button type="button" class="workflow-draft-remove" data-action="draftRowRemove">Remove step</button>
        </div>
        <button type="button" class="workflow-draft-add" data-action="draftRowAdd" data-rows-target="steps">+ Add step</button>
      </fieldset>

      <fieldset class="workflow-draft-rows" data-rows="questions">
        <legend>Questions (front-loaded human input)</legend>
        <div class="workflow-draft-row" data-row>
          <label>Question text <input name="question_text" placeholder="What scope?"></label>
          <div class="row-grid compact-grid">
            <label>Binds to step <input name="question_binds_to_node" placeholder="step_1" list="workflow-draft-step-keys"></label>
            <label>Binds to param <input name="question_binds_to_param" placeholder="(default = appended to instructions)"></label>
            <label class="workflow-draft-checkbox">
              <input type="checkbox" name="question_required"> Required
            </label>
          </div>
          <button type="button" class="workflow-draft-remove" data-action="draftRowRemove">Remove question</button>
        </div>
        <button type="button" class="workflow-draft-add" data-action="draftRowAdd" data-rows-target="questions">+ Add question</button>
      </fieldset>

      <datalist id="workflow-draft-step-keys"></datalist>

      <fieldset class="workflow-draft-answers" data-rows="answers">
        <legend>Answers</legend>
        <p class="muted small">Auto-rendered from required questions above. Fill before approving.</p>
        <div class="workflow-draft-answer-list" data-answer-list>
          <p class="muted small">(no required questions yet)</p>
        </div>
      </fieldset>

      <details class="workflow-draft-raw">
        <summary>Raw JSON (advanced)</summary>
        <p class="muted small">These hidden fields are populated automatically on submit. You can also paste a full payload here to override the row editors above.</p>
        <label>Steps JSON <textarea name="proposed_steps" placeholder="[]"></textarea></label>
        <label>Questions JSON <textarea name="questions" placeholder="[]"></textarea></label>
        <label>Answers JSON <textarea name="answers" placeholder="{}"></textarea></label>
      </details>

      <button type="submit">Create Draft</button>
    </form>
  `;
}

// wf-05 part 3: row-editor lifecycle. We render the form once via the
// template, then DOM-mutate to add/remove rows. On submit, we walk the
// rows and serialize into the JSON shape the existing
// workflowDraftCreate handler expects.

function draftBuilderCollectSteps(form: HTMLFormElement): JsonObject[] {
  const rows = form.querySelectorAll<HTMLElement>(
    "[data-rows='steps'] [data-row]",
  );
  const out: JsonObject[] = [];
  rows.forEach((row, index) => {
    const get = (name: string): string => {
      const el = row.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
        `[name='${name}']`,
      );
      return (el?.value || "").trim();
    };
    const key = get("step_node_key") || `step_${index + 1}`;
    const role = get("step_role_required");
    const instructions = get("step_instructions");
    const nodeType = get("step_node_type") || "task";
    if (!role && !instructions) return; // skip blank rows
    const node: JsonObject = { node_key: key };
    if (role) node.role_required = role;
    if (instructions) node.instructions = instructions;
    if (nodeType && nodeType !== "task") node.node_type = nodeType;
    out.push(node);
  });
  return out;
}

function draftBuilderCollectQuestions(form: HTMLFormElement): JsonObject[] {
  const rows = form.querySelectorAll<HTMLElement>(
    "[data-rows='questions'] [data-row]",
  );
  const out: JsonObject[] = [];
  rows.forEach((row, index) => {
    const get = (name: string): string => {
      const el = row.querySelector<HTMLInputElement | HTMLTextAreaElement>(
        `[name='${name}']`,
      );
      return (el?.value || "").trim();
    };
    const required =
      row.querySelector<HTMLInputElement>("[name='question_required']")?.checked ||
      false;
    const text = get("question_text");
    const bindsToNode = get("question_binds_to_node");
    const bindsToParam = get("question_binds_to_param");
    if (!text) return;
    const id = slugifyQuestion(text, index);
    const question: JsonObject = { id, text };
    if (required) question.required = true;
    if (bindsToNode) question.binds_to_node = bindsToNode;
    if (bindsToParam) question.binds_to_param = bindsToParam;
    out.push(question);
  });
  return out;
}

function draftBuilderCollectAnswers(form: HTMLFormElement): JsonObject {
  const result: JsonObject = {};
  form.querySelectorAll<HTMLInputElement>("[data-answer-input]").forEach((input) => {
    const id = input.dataset.questionId || "";
    const value = (input.value || "").trim();
    if (id && value) result[id] = value;
  });
  return result;
}

function slugifyQuestion(text: string, index: number): string {
  const cleaned = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 32);
  return cleaned || `q_${index + 1}`;
}

function draftBuilderRefreshDerived(form: HTMLFormElement): void {
  // Refresh the binds_to_node `<datalist>` to reflect the current step
  // keys, and re-render the Answers section to mirror current
  // questions. Called after any row add/remove or text change in the
  // step-key or question-text inputs.
  const datalist = form.querySelector<HTMLDataListElement>("#workflow-draft-step-keys");
  if (datalist) {
    const stepKeys = draftBuilderCollectSteps(form).map((s) => String(s.node_key));
    datalist.innerHTML = stepKeys
      .map((k) => `<option value="${escapeHtml(k)}"></option>`)
      .join("");
  }
  const answerList = form.querySelector<HTMLElement>("[data-answer-list]");
  if (!answerList) return;
  const questions = draftBuilderCollectQuestions(form);
  // Preserve in-progress answer values keyed by id across re-renders.
  const existingValues = new Map<string, string>();
  answerList
    .querySelectorAll<HTMLInputElement>("[data-answer-input]")
    .forEach((input) => {
      const id = input.dataset.questionId || "";
      if (id) existingValues.set(id, input.value);
    });
  const renderable = questions.filter((q) => q.required);
  if (!renderable.length) {
    answerList.innerHTML = `<p class="muted small">(no required questions yet)</p>`;
    return;
  }
  answerList.innerHTML = renderable
    .map((q) => {
      const id = String(q.id || "");
      const value = existingValues.get(id) || "";
      return `
        <label class="workflow-draft-answer">
          <span>${escapeHtml(String(q.text))} <span class="chip warn">required</span></span>
          <input type="text" data-answer-input data-question-id="${escapeHtml(id)}" value="${escapeHtml(value)}">
        </label>
      `;
    })
    .join("");
}

function draftBuilderAddRow(button: HTMLElement): void {
  const target = button.dataset.rowsTarget || "";
  const form = button.closest<HTMLFormElement>("form");
  if (!form) return;
  const fieldset = form.querySelector<HTMLElement>(
    `[data-rows='${target}']`,
  );
  if (!fieldset) return;
  const existing = fieldset.querySelector<HTMLElement>("[data-row]");
  if (!existing) return;
  const clone = existing.cloneNode(true) as HTMLElement;
  clone.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>(
    "input, textarea",
  ).forEach((el) => {
    if (el.type === "checkbox") (el as HTMLInputElement).checked = false;
    else el.value = "";
  });
  fieldset.insertBefore(clone, button);
  draftBuilderRefreshDerived(form);
}

function draftBuilderRemoveRow(button: HTMLElement): void {
  const row = button.closest<HTMLElement>("[data-row]");
  const form = button.closest<HTMLFormElement>("form");
  if (!row || !form) return;
  const fieldset = row.parentElement as HTMLElement | null;
  if (fieldset && fieldset.querySelectorAll("[data-row]").length <= 1) {
    // Don't allow removing the last row — leave it blank instead.
    row
      .querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")
      .forEach((el) => {
        if (el.type === "checkbox") (el as HTMLInputElement).checked = false;
        else el.value = "";
      });
  } else {
    row.remove();
  }
  draftBuilderRefreshDerived(form);
}

function notifierChannelRecord(channel: NotifierChannelRecord): string {
  return `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(channel.name)}</h3><p class="muted small mono">${escapeHtml(channel.id)}</p></div>
        <div class="chip-row">${chip(channel.channel_type, "info")}${chip(channel.enabled ? "enabled" : "disabled", channel.enabled ? "good" : "warn")}</div>
      </div>
      <p class="muted small">${escapeHtml((channel.event_types || []).join(", ") || "task.*")}</p>
      <p class="muted small mono">${escapeHtml(jsonSummary(channel.target))}</p>
    </article>
  `;
}

function renderHermes(): string {
  const data = mustData();
  const contexts = Object.values(data.hermes_work_contexts || {});
  const proofs = Object.values(data.hermes_runtime_proofs || {});
  const surfaces = data.hermes_config_surfaces || [];
  const selectedSurface = selectedHermesSurface(data);
  const readyProofs = proofs.filter((proof) => proof.ready).length;
  return `
    <section class="metric-grid">
      ${metric("Tenants", data.tenants.length, `${data.users.length} users`)}
      ${metric("Personas", data.personas.length, "soul refs only")}
      ${metric("Instances", data.hermes_instances.length, `${data.platform_bindings.length} bindings`)}
      ${metric("Interaction Tasks", data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id).length, "from Hermes")}
      ${metric("Context Projects", new Set(contexts.flatMap((context) => context.projects.map((project) => project.project))).size, `${contexts.reduce((sum, context) => sum + context.task_count, 0)} visible tasks`)}
      ${metric("Runtime Proof", `${readyProofs}/${proofs.length}`, proofs.length === readyProofs ? "ready" : "degraded")}
      ${metric("Config Surface", surfaces.length, selectedSurface ? `${selectedSurface.plugins.length} plugins, ${selectedSurface.skills.length} skills` : "no fleet")}
    </section>
    ${hermesStartupPanel(data.hermes_startup)}
    ${hermesConfigSurfacePanel(data)}
    <section class="record-list">
      ${data.hermes_instances.length ? data.hermes_instances.map((instance) => hermesRecord(instance, data)).join("") : `<div class="empty-state">No Hermes instances</div>`}
    </section>
  `;
}

function hermesStartupPanel(startup?: HermesStartup | null): string {
  if (!startup) {
    return `<section class="surface"><h2>Startup Health</h2><div class="empty-state">No startup report</div></section>`;
  }
  const operator = startup.operator_health || {};
  const security = (startup.security?.secret_redaction || {}) as JsonObject;
  const slack = startup.slack || {};
  const logs = startup.logs || {};
  const runtime = startup.task_project_runtime || {};
  const runtimeAuthority = (runtime.authority || {}) as JsonObject;
  const promptBridge = (runtime.prompt_bridge || {}) as JsonObject;
  const warnings = startup.warnings || [];
  return `
    <section class="surface">
      <h2>Startup Health</h2>
      <div class="chip-row">
        ${chip(String(operator.status || (startup.ready ? "healthy" : "degraded")), startup.ready ? "good" : "bad")}
        ${chip(`redaction ${security.effective === false ? "off" : "on"}`, security.effective === false ? "bad" : "good")}
        ${chip(`logs ${Number(logs.actionable_count || 0)}`, Number(logs.actionable_count || 0) ? "bad" : "good")}
        ${chip(`runtime ${String(runtime.status || operator.task_project_runtime_status || "unknown")}`, runtime.ready === false ? "bad" : "good")}
      </div>
      <div class="row-grid">
        ${field("State refs", operator.state_refs_existing ?? 0)}
        ${field("Slack activation", slack.activation_source || operator.slack_activation_source || "unknown")}
        ${field("Redaction source", security.source || "unknown")}
        ${field("Log classes", (logs.classes as unknown[] | undefined)?.length ?? 0)}
        ${field("Hermes instance", runtime.hermes_instance_id || operator.task_project_runtime_hermes_instance_id || "unbound")}
        ${field("MAC authority", `tasks ${runtimeAuthority.tasks || "?"}, projects ${runtimeAuthority.projects || "?"}`)}
        ${field("Prompt bridge", promptBridge.present ? "active" : "missing")}
      </div>
      ${warnings.length ? `<div class="timeline">${warnings.map((warning) => timelineItem("warning", warning, "")).join("")}</div>` : ""}
    </section>
  `;
}

function selectedHermesSurface(data: DashboardData): HermesConfigSurface | null {
  const surfaces = data.hermes_config_surfaces || [];
  if (!surfaces.length) return null;
  const selected = String(state.selectedId || "");
  return surfaces.find((surface) =>
    String(surface.fleet_id || "") === selected
    || String(surface.fleet_name || "") === selected
    || String(surface.registry_key || "") === selected
  ) || surfaces[0];
}

function hermesConfigSurfacePanel(data: DashboardData): string {
  const surfaces = data.hermes_config_surfaces || [];
  if (!surfaces.length) {
    return `<section class="surface"><h2>Hermes Configuration</h2><div class="empty-state">No fleet configuration surfaces</div></section>`;
  }
  const surface = selectedHermesSurface(data) || surfaces[0];
  const writable = canWrite(data);
  const disabled = disabledAttr(!writable);
  const surfaceId = String(surface.fleet_id || surface.fleet_name || surface.registry_key || "");
  const runtime = surface.runtime || {};
  const configField = surface.config_fields.find((field) => field.desired) || surface.config_fields[0];
  const envField = surface.env_vars.find((field) => field.desired) || surface.env_vars.find((field) => field.required) || surface.env_vars[0];
  const desiredPlugins = (surface.desired?.plugins || {}) as JsonObject;
  const desiredSkills = (surface.desired?.skills || {}) as JsonObject;
  return `
    <section class="surface">
      <div class="surface-heading">
        <div>
          <h2>Hermes Configuration</h2>
          <p class="muted small mono">${escapeHtml(surface.registry_path || "")}</p>
        </div>
        <div class="chip-row">
          ${chip(surface.fleet_name || surface.fleet_id, "info")}
          ${chip(`${surface.agent_count} agents`, surface.agent_count ? "good" : "warn")}
          ${chip(writable ? "write" : "read only", writable ? "good" : "warn")}
        </div>
      </div>
      <div class="toolbar">
        <label>Fleet
          <select id="hermesFleetSelect">
            ${surfaces.map((item) => option(String(item.fleet_id || item.fleet_name || item.registry_key), String(item.fleet_name || item.fleet_id || item.registry_key), surfaceId)).join("")}
          </select>
        </label>
        ${field("Hermes home", surface.hermes_home || "unknown")}
        ${field("Config fields", surface.config_fields.length)}
        ${field("Env vars", surface.env_vars.length)}
      </div>
      <form class="action-form" data-action="hermesRuntimeUpdate" data-fleet-id="${escapeHtml(surfaceId)}">
        <label>Gateway Model <input name="gateway_model" value="${escapeHtml(runtime.gateway_model || "")}" ${disabled}></label>
        <label>Gateway Provider <input name="gateway_provider" value="${escapeHtml(runtime.gateway_provider || "")}" ${disabled}></label>
        <label>Gateway Base URL <input name="gateway_base_url" value="${escapeHtml(runtime.gateway_base_url || "")}" ${disabled}></label>
        <label>Slack Channel <input name="slack_home_channel_name" value="${escapeHtml(runtime.slack_home_channel_name || "")}" ${disabled}></label>
        <label class="inline-checkbox toolbar-checkbox"><input type="checkbox" name="apply_local" checked ${disabled}>Apply Local</label>
        <div class="form-actions"><button type="submit" ${disabled}>Save Runtime</button></div>
      </form>
      <section class="split compact-split">
        <div class="record-section">
          <h3>Config</h3>
          <form class="action-form compact" data-action="hermesConfigSet" data-fleet-id="${escapeHtml(surfaceId)}">
            <label>Key <select name="config_key" ${disabled}>${hermesConfigFieldOptions(surface, configField?.key || "")}</select></label>
            <label class="field-full">Value JSON <textarea class="json-editor" name="value_json" rows="4" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(jsonValue(configField?.value ?? ""))}</textarea></label>
            <label class="inline-checkbox toolbar-checkbox"><input type="checkbox" name="remove" ${disabled}>Remove</label>
            <div class="form-actions"><button type="submit" ${disabled}>Save Config</button></div>
          </form>
        </div>
        <div class="record-section">
          <h3>Environment</h3>
          <form class="action-form compact" data-action="hermesEnvSet" data-fleet-id="${escapeHtml(surfaceId)}">
            <label>Variable <select name="env_key" ${disabled}>${hermesEnvOptions(surface, envField?.name || "")}</select></label>
            <label>Value <input name="value" type="${envField?.password ? "password" : "text"}" autocomplete="off" ${disabled}></label>
            <label class="inline-checkbox toolbar-checkbox"><input type="checkbox" name="remove" ${disabled}>Remove</label>
            <div class="form-actions"><button type="submit" ${disabled}>Save Env</button></div>
          </form>
        </div>
      </section>
      <section class="split compact-split">
        <div class="record-section">
          <h3>Plugins</h3>
          <form class="action-form compact" data-action="hermesPluginsUpdate" data-fleet-id="${escapeHtml(surfaceId)}">
            <label class="field-full">Enabled <textarea name="enabled" rows="3" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(listFromUnknown(desiredPlugins.enabled).join(", "))}</textarea></label>
            <label class="field-full">Disabled <textarea name="disabled" rows="3" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(listFromUnknown(desiredPlugins.disabled).join(", "))}</textarea></label>
            <div class="form-actions"><button type="submit" ${disabled}>Save Plugins</button></div>
          </form>
        </div>
        <div class="record-section">
          <h3>Skills</h3>
          <form class="action-form compact" data-action="hermesSkillsUpdate" data-fleet-id="${escapeHtml(surfaceId)}">
            <label class="field-full">Disabled <textarea name="disabled" rows="3" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(listFromUnknown(desiredSkills.disabled).join(", "))}</textarea></label>
            <div class="form-actions"><button type="submit" ${disabled}>Save Skills</button></div>
          </form>
        </div>
      </section>
      ${hermesSurfaceInspectorTables(surface)}
    </section>
  `;
}

function hermesConfigFieldOptions(surface: HermesConfigSurface, selected: string): string {
  const fields = surface.config_fields.length ? surface.config_fields : [{ key: "" }];
  return fields.map((field) => option(String(field.key), String(field.key || "Select key"), selected)).join("");
}

function hermesEnvOptions(surface: HermesConfigSurface, selected: string): string {
  const fields = surface.env_vars.length ? surface.env_vars : [{ name: "" }];
  return fields.map((field) => option(String(field.name), String(field.name || "Select variable"), selected)).join("");
}

function hermesSurfaceInspectorTables(surface: HermesConfigSurface): string {
  return `
    <section class="record-section">
      <h3>Config Fields</h3>
      <div class="table-wrap responsive-table">
        <table class="data-table compact-table">
          <thead><tr><th>Key</th><th>Value</th><th>Source</th><th>Type</th></tr></thead>
          <tbody>${surface.config_fields.slice(0, 80).map(hermesConfigFieldRow).join("")}</tbody>
        </table>
      </div>
    </section>
    <section class="record-section">
      <h3>Environment Variables</h3>
      <div class="table-wrap responsive-table">
        <table class="data-table compact-table">
          <thead><tr><th>Name</th><th>Category</th><th>State</th><th>Source</th><th>Value</th></tr></thead>
          <tbody>${surface.env_vars.slice(0, 80).map(hermesEnvRow).join("")}</tbody>
        </table>
      </div>
    </section>
    <section class="record-section">
      <h3>Plugins</h3>
      <div class="table-wrap responsive-table">
        <table class="data-table compact-table">
          <thead><tr><th>Key</th><th>Kind</th><th>State</th><th>Env</th><th>Tools</th></tr></thead>
          <tbody>${surface.plugins.slice(0, 100).map(hermesPluginRow).join("")}</tbody>
        </table>
      </div>
    </section>
    <section class="record-section">
      <h3>Skills</h3>
      <div class="table-wrap responsive-table">
        <table class="data-table compact-table">
          <thead><tr><th>Name</th><th>Category</th><th>State</th><th>Env</th><th>Description</th></tr></thead>
          <tbody>${surface.skills.slice(0, 120).map(hermesSkillRow).join("")}</tbody>
        </table>
      </div>
    </section>
  `;
}

function hermesConfigFieldRow(field: HermesConfigField): string {
  return `
    <tr>
      <td class="mono">${escapeHtml(field.key)}</td>
      <td>${escapeHtml(truncate(jsonValue(field.value), 120))}</td>
      <td>${chip(field.source || "unknown", field.desired ? "good" : "info")}</td>
      <td>${escapeHtml(field.type || "")}</td>
    </tr>
  `;
}

function hermesEnvRow(item: HermesEnvVar): string {
  return `
    <tr>
      <td class="mono">${escapeHtml(item.name)}</td>
      <td>${escapeHtml(item.category || "")}${item.required ? ` ${chip("required", "warn")}` : ""}</td>
      <td>${chip(item.present ? "present" : "missing", item.present ? "good" : (item.required ? "bad" : "warn"))}</td>
      <td>${chip(item.source || "unknown", item.desired ? "good" : "info")}</td>
      <td class="mono">${escapeHtml(item.redacted_value || "")}</td>
    </tr>
  `;
}

function hermesPluginRow(item: HermesPluginRecord): string {
  const envCount = (item.requires_env || []).length + (item.optional_env || []).length;
  return `
    <tr>
      <td><span class="mono">${escapeHtml(item.key)}</span><br><span class="muted small">${escapeHtml(item.label || item.name)}</span></td>
      <td>${escapeHtml(item.kind || "")}<br><span class="muted small">${escapeHtml(item.source || "")}</span></td>
      <td>${chip(item.state || "auto", hermesStateTone(item.state || ""))}<br><span class="muted small">${escapeHtml(item.state_source || "")}</span></td>
      <td>${envCount}</td>
      <td>${escapeHtml((item.provides_tools || []).slice(0, 4).join(", "))}</td>
    </tr>
  `;
}

function hermesSkillRow(item: HermesSkillRecord): string {
  return `
    <tr>
      <td><span class="mono">${escapeHtml(item.name)}</span><br><span class="muted small">${escapeHtml(item.key)}</span></td>
      <td>${escapeHtml(item.category || item.source || "")}</td>
      <td>${chip(item.state || (item.enabled === false ? "disabled" : "enabled"), hermesStateTone(item.state || ""))}<br><span class="muted small">${escapeHtml(item.state_source || "")}</span></td>
      <td>${(item.required_environment_variables || []).length}</td>
      <td>${escapeHtml(truncate(item.description || "", 120))}</td>
    </tr>
  `;
}

function hermesStateTone(stateValue: string): Tone {
  if (stateValue === "disabled") return "warn";
  if (stateValue === "enabled" || stateValue === "auto_enabled") return "good";
  return "info";
}

function renderOperations(): string {
  const data = mustData();
  const writable = canWrite(data);
  const workflowCounts = data.workflow_runs.counts || {};
  const pendingProvisioning = data.provisioning_requests.filter((item) => item.status === "pending");
  const openStreams = data.agentbus_streams.filter((item) => item.status === "open");
  const operationContexts = Object.entries(data.hermes_work_contexts || {})
    .map(([instanceId, context]) => ({ instanceId, context }))
    .filter(({ context }) => !!context?.operations);
  return `
    <section class="metric-grid">
      ${metric("Roles", data.roles.length, "agent personas and constraints")}
      ${metric("Provisioning", pendingProvisioning.length, "pending agent requests")}
      ${metric("Workflows", data.workflows.length, `${data.workflow_runs.total || 0} runs`)}
      ${metric("Operations", operationContexts.length, "Hermes operation contracts")}
      ${metric("AgentBus", openStreams.length, `${data.agentbus_streams.length} recent streams`)}
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Operation Contracts</h2>
        ${chip(`${operationContexts.length}`, operationContexts.length ? "good" : "warn")}
      </div>
      <div class="record-list">
        ${operationContexts.length
          ? operationContexts.map(({ instanceId, context }) => operationContractRecord(instanceId, context)).join("")
          : `<div class="empty-state">No Hermes operation contracts</div>`}
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Workflow Runs</h2>
        ${stateBars(Object.keys(workflowCounts).sort(), workflowCounts, Number(data.workflow_runs.total || 0), "No workflow runs")}
        <div class="record-list">
          ${(data.workflow_runs.latest || []).length ? (data.workflow_runs.latest || []).map(workflowRunRecord).join("") : `<div class="empty-state">No workflow run records</div>`}
        </div>
      </div>
      <div class="surface">
        <div class="surface-heading">
          <h2>Provisioning Requests</h2>
          ${chip(`${pendingProvisioning.length} pending`, pendingProvisioning.length ? "warn" : "good")}
        </div>
        <div class="record-list">
          ${data.provisioning_requests.length ? data.provisioning_requests.map(provisioningRecord).join("") : `<div class="empty-state">No provisioning requests</div>`}
        </div>
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <div class="surface-heading">
          <h2>Roles</h2>
          <form class="inline-form" data-action="roleSeed">
            <label class="inline-checkbox"><input type="checkbox" name="replace" ${disabledAttr(!writable)}> Replace</label>
            <button type="submit" ${disabledAttr(!writable)}>Seed Defaults</button>
          </form>
        </div>
        <div class="record-list">
          ${data.roles.length ? data.roles.map(roleRecord).join("") : `<div class="empty-state">No roles</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>AgentBus And Messages</h2>
        <div class="record-list">
          ${data.agentbus_streams.length ? data.agentbus_streams.slice(0, 40).map(agentBusRecord).join("") : `<div class="empty-state">No AgentBus streams</div>`}
          ${data.messages.length ? data.messages.slice(0, 20).map(messageRecord).join("") : ""}
        </div>
      </div>
    </section>
    <section class="surface">
      <h2>Nap Schedules</h2>
      <div class="record-list">
        ${data.nap_schedules.length || data.nap_runs.length
          ? [...data.nap_schedules.map(napScheduleRecord), ...data.nap_runs.slice(0, 20).map(napRunRecord)].join("")
          : `<div class="empty-state">No nap activity</div>`}
      </div>
    </section>
  `;
}

function renderIntegrations(): string {
  const data = mustData();
  const failingEvalRuns = data.eval_runs.filter((run) => run.passed === false);
  return `
    <section class="metric-grid">
      ${metric("Legacy Repos", data.beads_repositories.length, "retired import sources")}
      ${metric("Imported Items", data.bridge_items.length, "project items from import bridges")}
      ${metric("Service UIs", data.service_links.length, "linked control surfaces")}
      ${metric("Artifacts", data.artifacts.length, "registered outputs")}
      ${metric("Eval Runs", data.eval_runs.length, `${failingEvalRuns.length} failing`)}
    </section>
    <section class="surface">
      <h2>Service UIs</h2>
      ${serviceLinksTable(data.service_links)}
    </section>
    <section class="split">
      <div class="surface">
        <h2>Legacy Imports</h2>
        <div class="record-list">
          ${data.beads_repositories.length ? data.beads_repositories.map(beadsRepositoryRecord).join("") : `<div class="empty-state">No legacy repositories</div>`}
          ${data.bridge_items.length ? data.bridge_items.slice(0, 30).map(bridgeItemRecord).join("") : ""}
        </div>
      </div>
      <div class="surface">
        <h2>Artifacts</h2>
        <div class="record-list">
          ${data.artifacts.length ? data.artifacts.slice(0, 40).map(artifactRecord).join("") : `<div class="empty-state">No artifacts</div>`}
        </div>
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <h2>Evaluations</h2>
        <div class="record-list">
          ${data.eval_sets.length ? data.eval_sets.map(evalSetRecord).join("") : `<div class="empty-state">No eval sets</div>`}
          ${data.eval_runs.length ? data.eval_runs.slice(0, 40).map(evalRunRecord).join("") : ""}
        </div>
      </div>
      <div class="surface">
        <h2>Memory</h2>
        <div class="record-list">
          ${data.memory_records.length ? data.memory_records.slice().reverse().slice(0, 40).map(memoryRecord).join("") : `<div class="empty-state">No memory records</div>`}
        </div>
      </div>
    </section>
  `;
}

function renderRuntime(): string {
  const data = mustData();
  const writable = canWrite(data);
  const disabled = disabledAttr(!writable);
  const activeRollouts = data.rollouts.filter((item) => ["planned", "canarying", "paused", "rescuing"].includes(String(item.rollout.status))).length;
  const runningRuns = data.runtime_runs.filter((run) => run.status === "running").length;
  const activeDeltas = data.runtime_deltas.filter((delta) => ["proposed", "validated"].includes(String(delta.status))).length;
  return `
    <section class="metric-grid">
      ${metric("Runtimes", data.runtimes.length, "execution environments")}
      ${metric("Runtime Runs", data.runtime_runs.length, `${runningRuns} running`)}
      ${metric("Runtime Deltas", data.runtime_deltas.length, `${activeDeltas} pending`)}
      ${metric("Rollouts", data.rollouts.length, `${activeRollouts} active`)}
      ${metric("Eval Gates", data.eval_sets.length, "available rollout gates")}
    </section>
    <section class="command-drawer-grid">
      <details class="surface action-drawer">
        <summary>
          <span>Create Runtime</span>
          <span class="muted small">Register a new execution environment manifest</span>
        </summary>
        <form class="action-form aligned-form" data-action="runtimeCreate">
          <label>Name <input name="name" required ${disabled}></label>
          <label>Created by <input name="created_by" value="human" ${disabled}></label>
          <label>Manifest JSON <textarea class="json-editor" name="manifest" placeholder="{}" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}></textarea></label>
          <button type="submit" ${disabled}>Create Runtime</button>
        </form>
      </details>
      <details class="surface action-drawer">
        <summary>
          <span>Create Rollout</span>
          <span class="muted small">Stage a canary, full promotion, or rescue rollout</span>
        </summary>
        <form class="action-form aligned-form" data-action="rolloutCreate">
          <label>Version <input name="version" required placeholder="2026.06.03" ${disabled}></label>
          <label>Strategy ${select("strategy", ["canary", "full", "rescue"], "canary", !writable)}</label>
          <label>Target % <input name="target_percent" type="number" min="0" max="100" value="10" ${disabled}></label>
          <label>Runtime ${runtimeSelect("runtime_environment_id", data.runtimes, "", !writable)}</label>
          <label>Tenant ID <input name="tenant_id" placeholder="global" ${disabled}></label>
          <label>Channel <input name="channel" value="fleet" ${disabled}></label>
          <label>Artifact URI <input name="artifact_uri" placeholder="artifact://..." ${disabled}></label>
          <label>Artifact hash <input name="artifact_hash" placeholder="sha256:..." ${disabled}></label>
          <label>Eval gate ${evalSetSelect("required_eval_set_id", data.eval_sets, "", !writable)}</label>
          <label>Created by <input name="created_by" value="human" ${disabled}></label>
          <label>Health policy JSON <textarea class="json-editor" name="health_policy" placeholder="{}" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}></textarea></label>
          <button type="submit" ${disabled}>Create Rollout</button>
        </form>
      </details>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Runtime Deltas</h2>
        ${chip(`${activeDeltas} pending`, activeDeltas ? "warn" : "good")}
      </div>
      <details class="action-drawer inline-drawer">
        <summary>
          <span>Propose Runtime Delta</span>
          <span class="muted small">Record a task-local environment extension for validation</span>
        </summary>
        <form class="action-form aligned-form" data-action="runtimeDeltaPropose">
          <label>Task ${taskSelect("task_id", data.tasks, "", !writable)}</label>
          <label>Agent ${agentSelect("agent_id", data.agents, "", !writable)}</label>
          <label>Package manager ${select("package_manager", ["pip", "uv", "npm", "pnpm"], "uv", !writable)}</label>
          <label>Base runtime ${runtimeSelect("base_runtime_id", data.runtimes, "", !writable)}</label>
          <label>Project <input name="project" placeholder="optional" ${disabled}></label>
          <label>Lockfile path <input name="lockfile_path" placeholder="uv.lock, package-lock.json" ${disabled}></label>
          <label>Lockfile digest <input name="lockfile_digest" placeholder="sha256:..." ${disabled}></label>
          <label>Evidence ID <input name="evidence_id" placeholder="optional" ${disabled}></label>
          <label>Reason <input name="reason" required placeholder="why this dependency is needed" ${disabled}></label>
          <label>Commands JSON <textarea class="json-editor" name="commands" placeholder='["uv add httpx==0.28.1"]' spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}></textarea></label>
          <label>Dependencies JSON <textarea class="json-editor" name="added_dependencies" placeholder='["httpx==0.28.1"]' spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}></textarea></label>
          <button type="submit" ${disabled}>Propose Delta</button>
        </form>
      </details>
      <div class="runtime-list">
        ${data.runtime_deltas.length ? data.runtime_deltas.map((delta) => runtimeDeltaRecord(delta, data)).join("") : `<div class="empty-state">No runtime deltas</div>`}
      </div>
    </section>
    <section class="surface">
      <details class="action-drawer inline-drawer">
        <summary>
          <span>Runtime Run Controls</span>
          <span class="muted small">Start or complete run records manually</span>
        </summary>
        <div class="runtime-control-grid">
        <form class="action-form aligned-form" data-action="runtimeRunCreate">
          <label>Task ${taskSelect("task_id", data.tasks, "", !writable)}</label>
          <label>Agent ${agentSelect("agent_id", data.agents, "", !writable)}</label>
          <label>Runtime ${runtimeSelect("environment_id", data.runtimes, "", !writable)}</label>
          <button type="submit" ${disabled}>Start Run</button>
        </form>
        <form class="action-form aligned-form" data-action="runtimeRunComplete">
          <label>Run ${runtimeRunSelect("run_id", data.runtime_runs, "", !writable)}</label>
          <label>Evidence ID <input name="evidence_id" required ${disabled}></label>
          <label>Status ${select("status", ["completed", "failed", "cancelled"], "completed", !writable)}</label>
          <button type="submit" ${disabled}>Complete Run</button>
        </form>
        </div>
      </details>
      <div class="mobile-card-list always-visible">
        ${data.runtime_runs.length ? data.runtime_runs.slice(0, 12).map((run) => runtimeRunCard(run, data)).join("") : `<div class="empty-state">No runtime runs</div>`}
      </div>
    </section>
    <section class="split">
      <div class="surface">
        <div class="surface-heading">
          <h2>Runtime Environments</h2>
          ${chip(`${data.runtimes.length} configured`, data.runtimes.length ? "info" : "warn")}
        </div>
        <div class="runtime-list">
          ${data.runtimes.length ? data.runtimes.map((runtime) => runtimeRecord(runtime, data)).join("") : `<div class="empty-state">No runtimes</div>`}
        </div>
      </div>
      <div class="surface">
        <div class="surface-heading">
          <h2>Rollouts</h2>
          ${chip(`${data.rollouts.length} tracked`, data.rollouts.length ? "info" : "warn")}
        </div>
        <div class="rollout-list">
          ${data.rollouts.length ? data.rollouts.map((status) => rolloutRecord(status, data)).join("") : `<div class="empty-state">No rollouts</div>`}
        </div>
      </div>
    </section>
    ${runtimeInspector(data)}
  `;
}

function renderSecrets(): string {
  const data = mustData();
  const grantedAudits = data.secret_audits.filter((audit) => audit.result === "granted").length;
  return `
    <section class="metric-grid">
      ${metric("Secrets", data.secrets.length, "redacted records")}
      ${metric("Enabled", data.secrets.filter((secret) => secret.enabled).length, "available for scoped access")}
      ${metric("Audit Events", data.secret_audits.length, `${grantedAudits} granted`)}
      ${metric("Agents", data.agents.length, "eligible accessors")}
    </section>
    <details class="surface action-drawer">
      <summary>
        <span>Create Secret</span>
        <span class="muted small">Add a scoped, audited secret record</span>
      </summary>
      <form class="action-form aligned-form" data-action="secretCreate">
        <label>Name <input name="name" required></label>
        <label>Created by <input name="created_by" value="human"></label>
        <label>Value <input name="value" type="password" required autocomplete="new-password"></label>
        <label>Scopes JSON <textarea class="json-editor" name="scopes" placeholder='{"agents":[]}' spellcheck="false" autocomplete="off" autocapitalize="off"></textarea></label>
        <button type="submit">Create Secret</button>
      </form>
    </details>
    <section class="split">
      <div class="surface">
        <div class="surface-heading">
          <h2>Secrets</h2>
          ${chip(`${data.secrets.length} records`, data.secrets.length ? "info" : "warn")}
        </div>
        <div class="record-list">
          ${data.secrets.length ? data.secrets.map((secret) => secretRecord(secret, data.agents)).join("") : `<div class="empty-state">No secrets</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Access Audit</h2>
        <div class="record-list">
          ${data.secret_audits.length ? data.secret_audits.map(secretAuditRecord).join("") : `<div class="empty-state">No audit records</div>`}
        </div>
      </div>
    </section>
    ${secretInspector(data)}
  `;
}

function renderObservability(): string {
  const data = mustData();
  const observability = data.observability || {
    counts: {},
    levels: {},
    layers: {},
    latest: [],
    latest_metrics: [],
  };
  const counts = observability.counts || {};
  const auditEvents = filterAuditEvents(data.events || []);
  const commandAudit = filterCommandAudit(data.command_audit || []);
  const notifications = data.notifications || [];
  const integrationFindings = data.integration_findings || [];
  const openIntegrationFindings = integrationFindings.filter((item) => item.status === "open");
  const pendingNotifications = notifications.filter((item) => item.status === "pending").length;
  const observationEvents = uniqueObservations([...state.observabilityLive, ...(observability.latest || [])]);
  const llmRoutes = filterObservability(observationEvents.filter((item) => item.name === "llm.route"));
  const live = filterObservability(observationEvents);
  const layerTotal = Object.values(observability.layers || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  const levelTotal = Object.values(observability.levels || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  return `
    <section class="metric-grid">
      ${metric("Observations", counts.events || 0, `${counts.logs || 0} logs, ${counts.metrics || 0} metrics`)}
      ${metric("Warnings", counts.warnings || 0, "warning observations")}
      ${metric("Errors", counts.errors || 0, "error observations")}
      ${metric("Notifications", notifications.length, `${pendingNotifications} pending`)}
      ${metric("Integration Findings", integrationFindings.length, `${openIntegrationFindings.length} open`)}
      ${metric("Stream", state.observabilityStreamStatus, `${state.observabilityLive.length} live item(s)`)}
    </section>
    ${auditFilterToolbar(data)}
    <section class="split">
      <div class="surface">
        <h2>Metric Snapshot</h2>
        <div class="metric-list">
          ${(observability.latest_metrics || []).length
            ? observability.latest_metrics.map(observationMetric).join("")
            : `<div class="empty-state">No metrics</div>`}
        </div>
      </div>
      <div class="surface">
        <h2>Distribution</h2>
        ${stateBars(Object.keys(observability.layers || {}).sort(), observability.layers || {}, layerTotal, "No layers")}
        ${stateBars(Object.keys(observability.levels || {}).sort(), observability.levels || {}, levelTotal, "No levels")}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Notifications</h2>
        ${chip(`${pendingNotifications} pending`, pendingNotifications ? "warn" : "good")}
      </div>
      <div class="observability-feed">
        ${notifications.length ? notifications.slice(0, 80).map(notificationRecord).join("") : `<div class="empty-state">No notifications</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Integration Findings</h2>
        ${chip(`${openIntegrationFindings.length} open`, openIntegrationFindings.length ? "warn" : "good")}
      </div>
      <div class="observability-feed">
        ${integrationFindings.length
          ? integrationFindings.slice(0, 80).map(integrationFindingRecord).join("")
          : `<div class="empty-state">No integration findings</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Unified Events</h2>
        ${chip(`${auditEvents.length}`, auditEvents.length ? "info" : "warn")}
      </div>
      <div class="observability-feed">
        ${auditEvents.length ? auditEvents.slice(0, 120).map(auditEventRecord).join("") : `<div class="empty-state">No matching audit events</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>LLM Routes</h2>
        ${chip(`${llmRoutes.length}`, llmRoutes.length ? "info" : "warn")}
      </div>
      <div class="observability-feed">
        ${llmRoutes.length ? llmRoutes.slice(0, 80).map(llmRouteRecord).join("") : `<div class="empty-state">No LLM route records</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Command Audit</h2>
        ${chip(`${commandAudit.length}`, commandAudit.length ? "info" : "warn")}
      </div>
      <div class="observability-feed">
        ${commandAudit.length ? commandAudit.slice(0, 80).map(commandAuditRecord).join("") : `<div class="empty-state">No command audit records</div>`}
      </div>
    </section>
    <section class="surface">
      <div class="surface-heading">
        <h2>Live Stream</h2>
        ${chip(state.observabilityStreamStatus, state.observabilityStreamStatus === "connected" ? "good" : state.observabilityStreamStatus === "error" ? "bad" : "info")}
      </div>
      <div class="observability-feed">
        ${live.length ? live.slice(0, 80).map(observationRecord).join("") : `<div class="empty-state">No observations</div>`}
      </div>
    </section>
  `;
}

function auditFilterToolbar(data: DashboardData): string {
  const projectValues = ["", ...projectFilterOptions(data).map((item) => item.project).filter((item) => item && item !== "unassigned")];
  const fleetValues = ["", ...data.fleets.map((item) => item.name || item.id)];
  const agentOptions = `<option value="">Any agent</option>${data.agents.map((item) => option(item.agent.id, item.agent.name, state.auditAgentId)).join("")}`;
  const taskOptions = `<option value="">Any task</option>${data.tasks.map((item) => option(item.task.id, item.task.title, state.auditTaskId)).join("")}`;
  return `
    <section class="toolbar audit-toolbar">
      <select id="auditSubjectType">
        ${AUDIT_SUBJECT_TYPES.map((value) => option(value, value ? labelize(value) : "Any subject", state.auditSubjectType)).join("")}
      </select>
      <input id="auditSubjectId" value="${escapeHtml(state.auditSubjectId)}" placeholder="Subject id">
      <input id="auditEventPrefix" value="${escapeHtml(state.auditEventPrefix)}" placeholder="Event prefix">
      <input id="auditActor" value="${escapeHtml(state.auditActor)}" placeholder="Actor">
      <input id="auditLayer" value="${escapeHtml(state.auditLayer)}" placeholder="Layer">
      <select id="auditLevel">
        ${OBSERVABILITY_LEVELS.map((value) => option(value, value ? labelize(value) : "Any level", state.auditLevel)).join("")}
      </select>
      <select id="auditAgentId">${agentOptions}</select>
      <select id="auditTaskId">${taskOptions}</select>
      <select id="auditProject">${projectValues.map((value) => option(value, value || "Any project", state.auditProject)).join("")}</select>
      <select id="auditFleet">${fleetValues.map((value) => option(value, value || "Any fleet", state.auditFleet)).join("")}</select>
      <input id="auditSince" value="${escapeHtml(state.auditSince)}" placeholder="Since ISO">
      <input id="auditUntil" value="${escapeHtml(state.auditUntil)}" placeholder="Until ISO">
      <button type="button" id="clearAuditFilters">Clear</button>
    </section>
  `;
}

function filterAuditEvents(events: AuditEvent[]): AuditEvent[] {
  return events.filter((item) => {
    if (state.auditSubjectType && item.subject_type !== state.auditSubjectType) return false;
    if (state.auditSubjectId && item.subject_id !== state.auditSubjectId.trim()) return false;
    if (state.auditEventPrefix && !item.event_type.startsWith(state.auditEventPrefix.trim())) return false;
    if (state.auditActor && item.actor !== state.auditActor.trim()) return false;
    if (state.auditAgentId && !eventReferences(item, state.auditAgentId)) return false;
    if (state.auditTaskId && !eventReferences(item, state.auditTaskId)) return false;
    if (state.auditProject && !eventReferences(item, state.auditProject)) return false;
    if (state.auditFleet && !eventReferences(item, state.auditFleet)) return false;
    if (state.auditSince && item.created_at < state.auditSince.trim()) return false;
    if (state.auditUntil && item.created_at > state.auditUntil.trim()) return false;
    return true;
  });
}

function filterCommandAudit(records: CommandAuditRecord[]): CommandAuditRecord[] {
  return records.filter((item) => {
    if (state.auditAgentId && item.agent_id !== state.auditAgentId) return false;
    if (state.auditTaskId && item.task_id !== state.auditTaskId) return false;
    if (state.auditSubjectType === "agent" && state.auditSubjectId && item.agent_id !== state.auditSubjectId.trim()) return false;
    if (state.auditSubjectType === "task" && state.auditSubjectId && item.task_id !== state.auditSubjectId.trim()) return false;
    if (state.auditEventPrefix && !`command.${item.phase}`.startsWith(state.auditEventPrefix.trim())) return false;
    if (state.auditSince && item.created_at < state.auditSince.trim()) return false;
    if (state.auditUntil && item.created_at > state.auditUntil.trim()) return false;
    return true;
  });
}

function filterObservability(events: ObservabilityEvent[]): ObservabilityEvent[] {
  return events.filter((item) => {
    if (state.auditLayer && item.layer !== state.auditLayer.trim()) return false;
    if (state.auditLevel && item.level !== state.auditLevel.trim()) return false;
    if (state.auditSubjectType && item.subject_type !== state.auditSubjectType) return false;
    if (state.auditSubjectId && item.subject_id !== state.auditSubjectId.trim()) return false;
    if (state.auditEventPrefix && !item.name.startsWith(state.auditEventPrefix.trim())) return false;
    if (state.auditAgentId && !observationReferences(item, state.auditAgentId)) return false;
    if (state.auditTaskId && !observationReferences(item, state.auditTaskId)) return false;
    if (state.auditProject && !observationReferences(item, state.auditProject)) return false;
    if (state.auditFleet && !observationReferences(item, state.auditFleet)) return false;
    if (state.auditSince && item.created_at < state.auditSince.trim()) return false;
    if (state.auditUntil && item.created_at > state.auditUntil.trim()) return false;
    return true;
  });
}

function eventReferences(item: AuditEvent, value: string): boolean {
  const needle = value.trim();
  if (!needle) return true;
  if (item.subject_id === needle || item.actor === needle) return true;
  return JSON.stringify(item.detail || {}).includes(needle);
}

function observationReferences(item: ObservabilityEvent, value: string): boolean {
  const needle = value.trim();
  if (!needle) return true;
  if (item.subject_id === needle || item.source === needle) return true;
  return JSON.stringify(item.detail || {}).includes(needle);
}

function llmRouteEvents(data: DashboardData): ObservabilityEvent[] {
  return uniqueObservations([
    ...state.observabilityLive,
    ...((data.observability?.latest || []) as ObservabilityEvent[]),
  ]).filter((item) => item.name === "llm.route");
}

function llmRoutesForTask(data: DashboardData, taskId: string): ObservabilityEvent[] {
  return llmRouteEvents(data).filter((item) => observationReferences(item, taskId));
}

function integrationFindingRecord(item: IntegrationFinding): string {
  const repo = item.detail?.repository as JsonObject | undefined;
  const sourceLabel = typeof repo?.name === "string" ? repo.name : item.source_id;
  return `
    <article class="feed-item">
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <p class="muted small">${escapeHtml(item.finding_type)} · ${escapeHtml(sourceLabel)} · ${escapeHtml(formatAge(item.last_seen_at))}</p>
        <p class="muted small mono">${escapeHtml(item.fingerprint.slice(0, 16))}</p>
      </div>
      <div class="chip-row">
        ${chip(item.status, item.status === "open" ? "warn" : "good")}
        ${chip(item.severity, item.severity === "critical" || item.severity === "error" ? "bad" : item.severity === "warning" ? "warn" : "info")}
      </div>
    </article>
  `;
}

function serviceLinksTable(services: ServiceLinkRecord[]): string {
  if (!services.length) {
    return `<div class="empty-state">No service UI links are configured</div>`;
  }
  return `
    <div class="table-wrap">
      <table class="data-table compact-table">
        <thead><tr><th>Service</th><th>Open</th><th>Status</th><th>Auth</th><th>Credentials</th></tr></thead>
        <tbody>
          ${services.map(serviceLinkRow).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function serviceLinkRow(service: ServiceLinkRecord): string {
  const auth = service.auth || {};
  const openUrl = String(auth.credential_pass_through && auth.pass_through_url ? auth.pass_through_url : service.ui_url || service.url || "");
  const healthUrl = String(service.health_url || "");
  const openLabel = auth.credential_pass_through ? "Open SSO" : "Open";
  const credentials = service.credentials || [];
  return `
    <tr>
      <td><strong>${escapeHtml(service.name)}</strong><br><span class="muted small">${escapeHtml(service.role)}</span></td>
      <td>
        <div class="chip-row">
          ${openUrl ? `<a class="pill tone-info" href="${escapeHtml(openUrl)}" target="_blank" rel="noreferrer">${escapeHtml(openLabel)}</a>` : chip("no ui", "warn")}
          ${healthUrl ? `<a class="pill" href="${escapeHtml(healthUrl)}" target="_blank" rel="noreferrer">Health</a>` : ""}
        </div>
      </td>
      <td>${chip(service.status || "unknown", healthTone(service.status))}<br><span class="muted small">${escapeHtml(service.kind)}</span></td>
      <td><span class="mono small">${escapeHtml(auth.type || "none")}</span><br><span class="muted small">${escapeHtml(auth.notes || "")}</span></td>
      <td>${credentials.length ? credentials.map(serviceCredentialLine).join("<br>") : `<span class="muted small">none</span>`}</td>
    </tr>
  `;
}

function serviceCredentialLine(ref: ServiceCredentialRef): string {
  const tone = ref.present ? "good" : "warn";
  const redacted = ref.redacted_value ? ` ${ref.redacted_value}` : "";
  return `${chip(ref.name || "credential", tone)} <span class="muted small mono">${escapeHtml(ref.source || "not_configured")}${escapeHtml(redacted)}</span>`;
}

function notificationRecord(item: OperatorNotification): string {
  return `
    <article class="feed-item">
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.body)}</p>
        <p class="muted small">${escapeHtml(item.event_type)} · ${escapeHtml(item.created_at)}</p>
      </div>
      <div class="chip-row">
        ${chip(item.status, item.status === "pending" ? "warn" : item.status === "failed" ? "bad" : "good")}
        ${(item.channels || []).map((channel) => chip(channel, "info")).join("")}
      </div>
    </article>
  `;
}

function dispatchRecord(item: DispatchTask): string {
  return `
    <article class="record compact ${selectedClass(item.task.id)}">
      <div class="record-header">
        <div><h3>${escapeHtml(item.task.title)}</h3><p class="muted small mono">${escapeHtml(item.task.id)}</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(item.task.id)}">Select</button>
      </div>
      <div class="chip-row">
        ${chip(`${item.eligible_agent_count} eligible`, item.eligible_agent_count ? "good" : "bad")}
        ${item.candidates.slice(0, 8).map((candidate) => chip(candidate.agent_name, candidate.eligible ? "good" : "warn")).join("")}
      </div>
    </article>
  `;
}

function taskDependencyRecords(data: DashboardData): string {
  const tasksById = new Map(data.tasks.map((detail) => [detail.task.id, detail.task]));
  const edges = data.tasks.flatMap((detail) =>
    (detail.task.dependencies || []).map((dependencyId) => ({ task: detail.task, dependency: tasksById.get(dependencyId), dependencyId }))
  );
  if (!edges.length) return `<div class="empty-state">No task dependencies</div>`;
  return edges.slice(0, 40).map((edge) => `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(edge.dependency?.title || edge.dependencyId)}</h3><p class="muted small">blocks</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(edge.task.id)}">Select child</button>
      </div>
      <p>${escapeHtml(edge.task.title)}</p>
      <p class="muted small mono">${escapeHtml(edge.dependencyId)} -> ${escapeHtml(edge.task.id)}</p>
    </article>
  `).join("");
}

function roleRecord(role: ApiRecord): string {
  return `
    <article class="record compact ${selectedClass(String(role.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(role.display_name || role.name || role.slug || role.id)}</h3><p class="muted small mono">${escapeHtml(role.id)}</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(role.id)}">Select</button>
      </div>
      <div class="chip-row">
        ${chip(role.level || "role", "info")}
        ${(role.required_capabilities as string[] | undefined || []).slice(0, 6).map((cap) => chip(cap, "good")).join("")}
      </div>
      <p class="muted small">${escapeHtml(role.description || "")}</p>
    </article>
  `;
}

function operationContractRecord(instanceId: string, context: HermesWorkContext): string {
  const operations = context.operations || { api: [], mac_cli: [], mac_hermes_cli: [], hgmac_cli: [] };
  const apiOps = (operations.api || []) as JsonObject[];
  const macCli = operations.mac_cli || [];
  const hermesCli = operations.mac_hermes_cli || [];
  const hgmacCli = operations.hgmac_cli || [];
  const dashboard = ((operations as JsonObject).dashboard || {}) as JsonObject;
  const dashboardViews = arrayOfStrings(dashboard.views);
  const transitions = operations.task_state_transitions || {};
  const transitionCount = Object.values(transitions).reduce((sum, targets) => (
    sum + (Array.isArray(targets) ? targets.length : 0)
  ), 0);
  return `
    <article class="record compact ${selectedClass(instanceId)}">
      <div class="record-header">
        <div>
          <h3>${escapeHtml(context.hermes_instance?.name || instanceId)}</h3>
          <p class="muted small mono">${escapeHtml(instanceId)}</p>
        </div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(instanceId)}">Select</button>
      </div>
      <div class="row-grid compact-grid">
        ${field("API operations", apiOps.length)}
        ${field("MAC CLI", macCli.length)}
        ${field("Hermes CLI", hermesCli.length)}
        ${field("hgmac CLI", hgmacCli.length)}
        ${field("Dashboard views", dashboardViews.length)}
        ${field("Transitions", transitionCount)}
      </div>
      <div class="chip-row">
        ${apiOps.slice(0, 12).map((op) => chip(`${op.method || "API"} ${op.name || op.path || ""}`, "info")).join("") || chip("api missing", "bad")}
      </div>
      <div class="chip-row">
        ${dashboardViews.slice(0, 12).map((view) => chip(view, view === "ops" ? "good" : "info")).join("") || chip("dashboard contract missing", "bad")}
      </div>
      <div class="observation-detail mono">${escapeHtml([...hermesCli, ...hgmacCli].slice(0, 8).join(" | ") || "No CLI operations")}</div>
    </article>
  `;
}

function provisioningRecord(item: ApiRecord): string {
  return `
    <article class="record compact ${selectedClass(String(item.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(item.reason || item.id)}</h3><p class="muted small mono">${escapeHtml(item.id)}</p></div>
        ${chip(item.status, item.status === "pending" ? "warn" : item.status === "fulfilled" ? "good" : "bad")}
      </div>
      <div class="chip-row">
        ${(item.capabilities as string[] | undefined || []).map((cap) => chip(cap, "info")).join("")}
        ${item.role_slug ? chip(item.role_slug, "good") : ""}
        ${item.task_id ? chip(`task ${shortHash(String(item.task_id))}`, "info") : ""}
      </div>
    </article>
  `;
}

function workflowRunRecord(run: ApiRecord): string {
  return `
    <article class="record compact ${selectedClass(String(run.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(run.workflow_id || run.id)}</h3><p class="muted small mono">${escapeHtml(run.id)}</p></div>
        ${chip(run.state, run.state === "completed" ? "good" : run.state === "failed" ? "bad" : "info")}
      </div>
      <div class="row-grid compact-grid">
        ${field("Tenant", run.tenant_id || "global")}
        ${field("Started", formatAge(String(run.started_at || run.created_at || "")))}
        ${field("Current node", run.current_node_key || "none")}
        ${field("Task", run.task_id || "none")}
      </div>
    </article>
  `;
}

function agentBusRecord(stream: ApiRecord): string {
  return `
    <article class="record compact ${selectedClass(String(stream.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(stream.topic || stream.content_type || stream.id)}</h3><p class="muted small mono">${escapeHtml(stream.id)}</p></div>
        ${chip(stream.status, stream.status === "open" ? "good" : "info")}
      </div>
      <p class="muted small">${escapeHtml(stream.sender_agent_id || "unknown")} -> ${escapeHtml(stream.recipient_agent_id || "broadcast")}</p>
    </article>
  `;
}

function messageRecord(message: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(message.message_type || message.id)}</h3><p class="muted small mono">${escapeHtml(message.id)}</p></div>${chip(message.status, message.status === "pending" ? "warn" : "good")}</div>
      <p class="muted small">${escapeHtml(message.sender_agent_id || "unknown")} -> ${escapeHtml(message.recipient_agent_id || "broadcast")}</p>
    </article>
  `;
}

function napScheduleRecord(schedule: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(schedule.agent_id)}</h3><p class="muted small mono">${escapeHtml(schedule.id)}</p></div>${chip(schedule.enabled ? "enabled" : "disabled", schedule.enabled ? "good" : "warn")}</div>
      <div class="row-grid compact-grid">
        ${field("Offset", schedule.offset_minutes)}
        ${field("Window", schedule.window_minutes)}
        ${field("Updated", formatAge(String(schedule.updated_at || "")))}
        ${field("Actor", schedule.actor || "agent")}
      </div>
    </article>
  `;
}

function napRunRecord(run: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(run.agent_id)}</h3><p class="muted small mono">${escapeHtml(run.id)}</p></div>${chip(run.status, run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "info")}</div>
      <p class="muted small">${escapeHtml(formatAge(String(run.started_at || run.created_at || "")))}</p>
    </article>
  `;
}

function beadsRepositoryRecord(repo: ApiRecord): string {
  const metadata = (repo.metadata && typeof repo.metadata === "object" ? repo.metadata : {}) as JsonObject;
  const health = (metadata.health && typeof metadata.health === "object" ? metadata.health : {}) as ApiRecord;
  const healthStatus = String(health.status || (repo.last_error ? "unhealthy" : "healthy"));
  const healthReason = String(health.reason || repo.last_error || "canonical");
  return `
    <article class="record compact ${selectedClass(String(repo.id))}">
      <div class="record-header"><div><h3>${escapeHtml(repo.name)}</h3><p class="muted small mono">${escapeHtml(repo.id)}</p></div><div class="chip-row">${chip(repo.enabled ? "enabled" : "disabled", repo.enabled ? "good" : "warn")}${chip(healthStatus, healthTone(healthStatus))}</div></div>
      <div class="row-grid compact-grid">
        ${field("Project", repo.project || "none")}
        ${field("Source", repo.source || "none")}
        ${field("Poll", `${repo.poll_interval_seconds || 0}s`)}
        ${field("Health", healthReason)}
        ${field("Path", repo.path || "none")}
      </div>
    </article>
  `;
}

function bridgeItemRecord(item: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(item.title || item.external_id)}</h3><p class="muted small mono">${escapeHtml(item.id)}</p></div>${chip(item.status || "imported", "info")}</div>
      <p class="muted small">${escapeHtml(item.source || "source")} / ${escapeHtml(item.project || "")}</p>
    </article>
  `;
}

function artifactRecord(artifact: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(artifact.kind || "artifact")}</h3><p class="muted small mono">${escapeHtml(artifact.id)}</p></div>${chip(shortHash(String(artifact.digest || "")), "good")}</div>
      <p class="muted small">${escapeHtml(artifact.uri || "")}</p>
    </article>
  `;
}

function evalSetRecord(evalSet: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(evalSet.name)}</h3><p class="muted small mono">${escapeHtml(evalSet.id)}</p></div>${chip(evalSet.scoring || "eval", "info")}</div>
      <div class="row-grid compact-grid">
        ${field("Baseline", evalSet.baseline_score ?? "none")}
        ${field("Regression", evalSet.regression_threshold ?? "none")}
        ${field("Created", formatAge(String(evalSet.created_at || "")))}
        ${field("Updated", formatAge(String(evalSet.updated_at || "")))}
      </div>
    </article>
  `;
}

function evalRunRecord(run: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(run.target_kind || "target")} ${escapeHtml(run.target_id || "")}</h3><p class="muted small mono">${escapeHtml(run.id)}</p></div>${chip(run.passed ? "passed" : "failed", run.passed ? "good" : "bad")}</div>
      <div class="score-line"><span class="bar-track"><span class="bar-fill" style="width:${Math.max(2, Math.min(100, Number(run.score || 0) * 100))}%"></span></span><span class="mono small">${escapeHtml(run.score ?? "n/a")}</span></div>
    </article>
  `;
}

function memoryRecord(memory: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(memory.record_type || "memory")}</h3><p class="muted small mono">${escapeHtml(memory.id)}</p></div>${chip(memory.subject_type || "memory", "info")}</div>
      <p>${escapeHtml(memory.content || "")}</p>
      <p class="muted small">${escapeHtml(memory.task_id || "")} ${escapeHtml(formatAge(String(memory.created_at || "")))}</p>
    </article>
  `;
}

function selectedProjectSummary(data: DashboardData): ProjectSummary | null {
  const projects = visibleProjectSummaries(data);
  if (state.projectFilter !== "all") {
    return projects.find((project) => project.project === state.projectFilter) || null;
  }
  if (state.selectedId) {
    const selectedTask = taskDetailById(data, state.selectedId);
    if (selectedTask) {
      const project = taskProject(selectedTask.task);
      return projects.find((item) => item.project === project) || null;
    }
  }
  return projects[0] || null;
}

function selectedTaskDetail(data: DashboardData): TaskDetail | null {
  if (!state.selectedId) return null;
  return taskDetailById(data, state.selectedId);
}

function taskDetailById(data: DashboardData, taskId: string): TaskDetail | null {
  return data.tasks.find((detail) => detail.task.id === taskId) || null;
}

function taskProject(task: TaskRecord): string {
  if (task.project) return String(task.project);
  const metadata = task.metadata || {};
  for (const key of ["project", "repository", "repo"]) {
    const value = metadata[key];
    if (value) return String(value);
  }
  const origin = metadata.origin as JsonObject | undefined;
  if (origin) {
    for (const key of ["project", "repository", "repo", "source"]) {
      const value = origin[key];
      if (value) return String(value);
    }
  }
  return "unassigned";
}

function projectFrontierRecord(project: ProjectSummary): string {
  const ready = project.frontier_tasks.slice(0, 4);
  return `
    <article class="project-row ${state.projectFilter === project.project ? "is-selected" : ""}">
      <div>
        <div class="record-header">
          <div><h3>${escapeHtml(project.project)}</h3><p class="muted small">${project.task_count} stories, ${project.active_agent_ids.length} active agents</p></div>
          <button class="link-button" type="button" data-project-focus="${escapeHtml(project.project)}">Scope</button>
        </div>
        <div class="chip-row">
          ${chip(`${project.ready_count} ready`, project.ready_count ? "good" : "info")}
          ${chip(`${project.blocked_count} blocked`, project.blocked_count ? "warn" : "good")}
          ${chip(`${project.review_count} review`, project.review_count ? "warn" : "info")}
          ${project.cross_project_dependency_count ? chip(`${project.cross_project_dependency_count} cross-project`, "warn") : ""}
        </div>
      </div>
      <div class="story-stack">
        ${ready.length ? ready.map((task) => storyButton(task)).join("") : `<span class="muted small">No ready stories</span>`}
      </div>
    </article>
  `;
}

function projectTable(projects: ProjectSummary[], data: DashboardData): string {
  if (!projects.length) return `<div class="empty-state">No projects</div>`;
  return `
    <div class="table-wrap responsive-table">
      <table class="data-table project-table">
        <thead>
          <tr><th>Project</th><th>Status</th><th>Description</th><th>Stories</th><th>Agents</th><th>Repositories</th><th></th></tr>
        </thead>
        <tbody>${projects.map((project) => projectTableRow(project, data)).join("")}</tbody>
      </table>
    </div>
    <div class="mobile-card-list">
      ${projects.map((project) => projectMobileCard(project, data)).join("")}
    </div>
  `;
}

function projectTableRow(project: ProjectSummary, data: DashboardData): string {
  const writable = canWrite(data);
  const durable = !!project.project_id;
  const description = String(project.description || "");
  const status = String(project.status || (durable ? "active" : "derived"));
  return `
    <tr class="${state.projectFilter === project.project ? "is-selected" : ""}">
      <td>
        <strong>${escapeHtml(project.project)}</strong>
        <br><span class="muted small mono">${escapeHtml(project.project_id || "derived")}</span>
        <div class="chip-row">
          ${chip(durable ? "record" : "derived", durable ? "good" : "warn")}
          ${writable ? chip("writable", "good") : chip("read-only", "warn")}
        </div>
      </td>
      <td>${chip(status, durable ? projectTone(status) : "warn")}</td>
      <td>${escapeHtml(description || "none")}</td>
      <td>
        <span class="mono">${project.task_count}</span>
        <div class="chip-row">
          ${chip(`${project.ready_count} ready`, project.ready_count ? "good" : "info")}
          ${chip(`${project.blocked_count} blocked`, project.blocked_count ? "warn" : "good")}
        </div>
      </td>
      <td>${escapeHtml(project.active_agent_names.join(", ") || "none")}</td>
      <td>${escapeHtml(String(project.repository_count || 0))}</td>
      <td>
        <div class="table-actions">
          <button class="link-button" type="button" data-select-id="${escapeHtml(project.project)}">Inspect</button>
          <button class="link-button" type="button" data-project-focus="${escapeHtml(project.project)}">Scope</button>
        </div>
      </td>
    </tr>
  `;
}

function projectMobileCard(project: ProjectSummary, data: DashboardData): string {
  const durable = !!project.project_id;
  const status = String(project.status || (durable ? "active" : "derived"));
  return `
    <article class="mobile-object-card ${state.projectFilter === project.project ? "is-selected" : ""}">
      <div class="record-header">
        <div>
          <h3>${escapeHtml(project.project)}</h3>
          <p class="muted small mono">${escapeHtml(project.project_id || "derived")}</p>
        </div>
        ${chip(status, durable ? projectTone(status) : "warn")}
      </div>
      <div class="chip-row">
        ${chip(`${project.task_count} stories`, "info")}
        ${chip(`${project.ready_count} ready`, project.ready_count ? "good" : "info")}
        ${chip(`${project.active_agent_ids.length} agents`, project.active_agent_ids.length ? "good" : "warn")}
        ${chip(`${project.repository_count} repos`, project.repository_count ? "info" : "warn")}
      </div>
      <p class="muted small">${escapeHtml(project.description || "No description")}</p>
      <div class="mobile-card-actions">
        <button class="link-button" type="button" data-select-id="${escapeHtml(project.project)}">Inspect</button>
        <button class="link-button" type="button" data-project-focus="${escapeHtml(project.project)}">Scope</button>
      </div>
    </article>
  `;
}

function projectInspector(projects: ProjectSummary[], data: DashboardData): string {
  if (!projects.length) return "";
  const selected = projects.find((project) => project.project === state.selectedId)
    || projects.find((project) => project.project === state.projectFilter)
    || projects[0];
  if (!selected) return "";
  const writable = canWrite(data);
  const durable = !!selected.project_id;
  const editable = writable && durable;
  const status = String(selected.status || (durable ? "active" : "derived"));
  const statusValue = ["active", "inactive", "archived"].includes(status) ? status : "active";
  const metadata = selected.metadata && typeof selected.metadata === "object" ? JSON.stringify(selected.metadata, null, 2) : "{}";
  const disabled = disabledAttr(!editable);
  return `
    <section class="object-inspector">
      <div class="object-inspector-header">
        <div>
          <p class="eyebrow">Project Inspector</p>
          <h2>${escapeHtml(selected.project)}</h2>
          <p class="muted small mono">${escapeHtml(selected.project_id || "derived project")}</p>
        </div>
        <div class="chip-row">
          ${chip(durable ? "record-backed" : "derived-only", durable ? "good" : "warn")}
          ${writable ? chip("write token", "good") : chip("read-only token", "warn")}
        </div>
      </div>
      <div class="row-grid">
        ${field("Stories", selected.task_count)}
        ${field("Ready", selected.ready_count)}
        ${field("Blocked", selected.blocked_count)}
        ${field("Review", selected.review_count)}
        ${field("Active agents", selected.active_agent_names.join(", ") || "none")}
        ${field("Dependencies", selected.dependency_edge_count)}
        ${field("Cross-project", selected.cross_project_dependency_count)}
        ${field("Repositories", selected.repository_count)}
      </div>
      ${projectTaskComposer(selected, data)}
      ${projectTaskList(selected, data)}
      <form class="action-form inspector-form" data-action="projectUpdate" data-project="${escapeHtml(selected.project)}">
        <label>Name <input name="name" value="${escapeHtml(selected.project)}" ${disabled}></label>
        <label>Status ${select("status", ["active", "inactive", "archived"], statusValue, !editable)}</label>
        <label>Description <textarea name="description" ${disabled}>${escapeHtml(String(selected.description || ""))}</textarea></label>
        <label>Metadata JSON <textarea class="json-editor" name="metadata" placeholder="{}" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(metadata)}</textarea></label>
        <button type="submit" ${disabled}>Save Project</button>
      </form>
      <div class="record-section danger-zone">
        <div class="record-header">
          <div>
            <h3>Delete Project</h3>
            <p class="muted small">Deletes the durable project record. Derived projects cannot be deleted from this surface.</p>
          </div>
          <button class="danger-button" type="button" data-project-delete="${escapeHtml(selected.project)}" ${disabled}>Delete</button>
        </div>
      </div>
    </section>
  `;
}

function projectTaskComposer(project: ProjectSummary, data: DashboardData): string {
  const writable = canWrite(data);
  const disabled = disabledAttr(!writable);
  const metadata = JSON.stringify(
    {
      origin: {
        type: "dashboard_project",
        project: project.project,
      },
    },
    null,
    2,
  );
  return `
    <section class="record-section project-task-composer">
      <div class="record-header">
        <div>
          <h3>New Task in Project</h3>
          <p class="muted small">Creates a ledger task with project pre-filled.</p>
        </div>
        ${chip(project.project, "info")}
      </div>
      <form class="action-form aligned-form project-task-form" data-action="taskCreate">
        <input type="hidden" name="project" value="${escapeHtml(project.project)}">
        <label>Title <input name="title" required ${disabled}></label>
        <label>Priority <input name="priority" type="number" value="0" ${disabled}></label>
        <label>Capabilities <input name="required_capabilities" value="${escapeHtml(project.required_capabilities.join(","))}" placeholder="python,deploy" ${disabled}></label>
        <label>Dependencies <input name="dependencies" placeholder="task_a,task_b" ${disabled}></label>
        <label class="field-full">Description <textarea name="description" rows="4" ${disabled}></textarea></label>
        <label class="field-full">Metadata JSON <textarea class="json-editor" name="metadata" spellcheck="false" autocomplete="off" autocapitalize="off" ${disabled}>${escapeHtml(metadata)}</textarea></label>
        <div class="field-full form-actions"><button type="submit" ${disabled}>Create Task</button></div>
      </form>
    </section>
  `;
}

function projectTaskList(project: ProjectSummary, data: DashboardData): string {
  const tasks = data.tasks
    .filter((detail) => taskProject(detail.task) === project.project)
    .sort((a, b) => {
      const activeDelta = Number(TERMINAL_TASK_STATES.has(a.task.state)) - Number(TERMINAL_TASK_STATES.has(b.task.state));
      if (activeDelta) return activeDelta;
      return String(b.task.last_updated_at || b.task.updated_at || "").localeCompare(String(a.task.last_updated_at || a.task.updated_at || ""));
    })
    .slice(0, 8);
  return `
    <section class="record-section">
      <div class="record-header">
        <div>
          <h3>Project Tasks</h3>
          <p class="muted small">${project.task_count} total in the ledger</p>
        </div>
        ${chip(`${tasks.length} shown`, "info")}
      </div>
      <div class="project-task-list">
        ${tasks.length ? tasks.map((detail) => projectTaskButton(detail.task)).join("") : `<div class="empty-state compact">No tasks in this project</div>`}
      </div>
    </section>
  `;
}

function projectTaskButton(task: TaskRecord): string {
  return `
    <button class="project-task-button" type="button" data-task-open="${escapeHtml(task.id)}">
      <span>
        <strong>${escapeHtml(task.title)}</strong>
        <span class="muted small mono">${escapeHtml(task.id)}</span>
      </span>
      <span class="chip ${statusTone(task.state)}">${escapeHtml(task.state)}</span>
    </button>
  `;
}

function fleetMembershipSummary(data: DashboardData): string {
  if (!data.fleets.length) return `<div class="empty-state">No fleets</div>`;
  const agentsById = new Map(data.agents.map((item) => [item.agent.id, item]));
  return `
    <div class="bucket-list">
      ${data.fleets.map((fleet) => {
        const members = (fleet.agent_ids || []).map((agentId) => agentsById.get(agentId)?.agent.name || agentId);
        return `
          <button class="bucket-row" type="button" data-select-id="${escapeHtml(fleet.id)}">
            <span>${escapeHtml(fleet.name)}</span>
            <span class="bar-track"><span class="bar-fill" style="width:${Math.max(4, Math.min(100, members.length * 12))}%"></span></span>
            <span class="mono small">${members.length}</span>
          </button>
        `;
      }).join("")}
    </div>
  `;
}

function fleetRecord(fleet: FleetRecord, data: DashboardData): string {
  const agentsById = new Map(data.agents.map((item) => [item.agent.id, item.agent.name]));
  const memberNames = (fleet.agent_ids || []).map((agentId) => agentsById.get(agentId) || agentId);
  const observedNames = (fleet.observed_agent_ids || []).map((agentId) => agentsById.get(agentId) || agentId);
  const unmanagedNames = (fleet.unmanaged_agent_ids || []).map((agentId) => agentsById.get(agentId) || agentId);
  return `
    <article class="record ${selectedClass(fleet.id)}">
      <div class="record-header">
        <div><h3>${escapeHtml(fleet.name)}</h3><p class="muted small mono">${escapeHtml(fleet.id)}</p></div>
        <button class="link-button" type="button" data-select-id="${escapeHtml(fleet.id)}">Select</button>
      </div>
      <div class="chip-row">
        ${chip(fleet.status, fleet.status === "active" ? "good" : "warn")}
        ${chip(`${(fleet.agent_ids || []).length} configured`, "info")}
        ${chip(`${(fleet.observed_agent_ids || []).length} observed`, "good")}
        ${(fleet.unmanaged_agent_ids || []).length ? chip(`${(fleet.unmanaged_agent_ids || []).length} unmanaged`, "warn") : ""}
        ${fleet.tenant_id ? chip(fleet.tenant_id, "info") : chip("global", "info")}
      </div>
      <p class="muted small">${escapeHtml(fleet.description || "")}</p>
      <div class="row-grid">
        ${field("Configured", memberNames.join(", ") || "none")}
        ${field("Observed", observedNames.join(", ") || "none")}
        ${field("Unmanaged", unmanagedNames.join(", ") || "none")}
        ${field("Metadata", jsonSummary(fleet.metadata))}
      </div>
    </article>
  `;
}

function selectedFleetRecord(data: DashboardData): FleetRecord | null {
  return data.fleets.find((fleet) => fleet.id === state.selectedId || fleet.name === state.selectedId) || null;
}

function fleetDetail(fleet: FleetRecord, data: DashboardData): string {
  const agentsById = new Map(data.agents.map((item) => [item.agent.id, item]));
  const members = (fleet.agent_ids || []).map((agentId) => agentsById.get(agentId)).filter(Boolean) as AgentItem[];
  return `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(fleet.name)}</h3><p class="muted small mono">${escapeHtml(fleet.id)}</p></div>
        ${chip(fleet.status, fleet.status === "active" ? "good" : "warn")}
      </div>
      <div class="row-grid">
        ${field("Tenant", fleet.tenant_id || "global")}
        ${field("Members", String(members.length))}
        ${field("Description", fleet.description || "none")}
        ${field("Metadata", jsonSummary(fleet.metadata))}
      </div>
      <div class="agent-list">
        ${members.length ? members.map((item) => agentPill(item, data)).join("") : `<div class="empty-state">No members</div>`}
      </div>
    </article>
  `;
}

function agentFleetNames(data: DashboardData, agentId: string): string[] {
  return data.fleets
    .filter((fleet) => (fleet.agent_ids || []).includes(agentId))
    .map((fleet) => fleet.name || fleet.id);
}

function agentFleetLabel(data: DashboardData, agentId: string): string {
  return agentFleetNames(data, agentId).join(", ") || "unassigned";
}

function roleLabel(data: DashboardData, roleId?: string | null): string {
  if (!roleId) return "unassigned";
  const role = data.roles.find((item) => item.id === roleId || item.slug === roleId);
  if (!role) return roleId;
  return String(role.display_name || role.name || role.slug || role.id || roleId);
}

function agentPill(item: AgentItem, data: DashboardData): string {
  return `
    <button class="agent-pill ${selectedClass(item.agent.id)}" type="button" data-select-id="${escapeHtml(item.agent.id)}">
      <span class="mono">${escapeHtml(item.agent.name)}</span>
      <span>${escapeHtml(agentFleetLabel(data, item.agent.id))}</span>
      <span>${escapeHtml(item.agent.status)} / ${escapeHtml(item.agent.health_status)}</span>
    </button>
  `;
}

function storyButton(task: TaskRecord): string {
  return `<button class="story-button ${selectedClass(task.id)}" type="button" data-select-id="${escapeHtml(task.id)}"><span>${escapeHtml(task.title)}</span><span class="mono small">${escapeHtml(task.id)}</span></button>`;
}

function storyScopePanel(data: DashboardData, detail: TaskDetail): string {
  const task = detail.task;
  const related = relatedAgentsForTask(data, detail);
  const dependencyDetails = (task.dependencies || []).map((id) => taskDetailById(data, id)).filter(Boolean) as TaskDetail[];
  const dependents = data.tasks.filter((candidate) => (candidate.task.dependencies || []).includes(task.id));
  return `
    <div class="story-scope">
      <div>
        <h3>${escapeHtml(task.title)}</h3>
        <p class="muted small mono">${escapeHtml(task.id)} / ${escapeHtml(taskProject(task))}</p>
        <div class="chip-row">
          ${chip(task.state, statusTone(task.state))}
          ${chip(`P${task.priority || 0}`, "info")}
          ${(task.required_capabilities || []).map((capability) => chip(capability, "info")).join("")}
        </div>
      </div>
      <div class="relationship-strip">
        ${related.length ? related.map(({ item, relation }) => scopedAgentPill(item, relation)).join("") : `<div class="empty-state">No agents attached to this story yet</div>`}
      </div>
      <div class="split compact-split">
        <div>
          <h3>Blocks This Story</h3>
          <div class="story-stack">${dependencyDetails.length ? dependencyDetails.map((item) => storyButton(item.task)).join("") : `<span class="muted small">No dependencies</span>`}</div>
        </div>
        <div>
          <h3>Unblocks Next</h3>
          <div class="story-stack">${dependents.length ? dependents.slice(0, 8).map((item) => storyButton(item.task)).join("") : `<span class="muted small">No dependents</span>`}</div>
        </div>
      </div>
    </div>
  `;
}

function relatedAgentsForTask(data: DashboardData, detail: TaskDetail): Array<{ item: AgentItem; relation: string }> {
  const relations = new Map<string, Set<string>>();
  const add = (agentId: unknown, relation: string) => {
    const id = String(agentId || "").trim();
    if (!id) return;
    if (!relations.has(id)) relations.set(id, new Set());
    relations.get(id)?.add(relation);
  };
  add(detail.task.owner_agent_id, "writing");
  for (const review of detail.reviews || []) add(review.reviewer_agent_id, "reviewing");
  for (const evidence of detail.evidence || []) {
    const kind = String(evidence.kind || "");
    add(evidence.created_by, kind === "test" ? "testing" : kind === "publication" ? "deploying" : "evidence");
  }
  for (const event of detail.history || []) add(event.actor, "history");
  for (const dependencyId of detail.task.dependencies || []) {
    const dependency = taskDetailById(data, dependencyId);
    add(dependency?.task.owner_agent_id, "dependency");
  }
  const byId = new Map(data.agents.map((item) => [item.agent.id, item]));
  return Array.from(relations.entries())
    .map(([agentId, relationSet]) => {
      const item = byId.get(agentId);
      return item ? { item, relation: Array.from(relationSet).join(", ") } : null;
    })
    .filter(Boolean) as Array<{ item: AgentItem; relation: string }>;
}

function scopedAgentPill(item: AgentItem, relation: string): string {
  return `
    <button class="agent-pill ${selectedClass(item.agent.id)}" type="button" data-select-id="${escapeHtml(item.agent.id)}">
      <span class="mono">${escapeHtml(item.agent.name)}</span>
      <span>${escapeHtml(relation)}</span>
      <span>${escapeHtml(item.agent.status)} / ${escapeHtml(item.agent.health_status)}</span>
    </button>
  `;
}

function projectAgentsPanel(data: DashboardData, project: ProjectSummary | null): string {
  const projectName = project?.project || "all";
  const agents = data.agents.filter((item) =>
    projectName === "all" ? item.active_tasks.length : (item.active_projects || []).includes(projectName)
  );
  if (!agents.length) return `<div class="empty-state">No active agents in this scope</div>`;
  return agentTable(agents.slice(0, 40), data, true);
}

function dependencyOrderPanel(data: DashboardData, project: ProjectSummary | null): string {
  const projects = project ? [project] : visibleProjectSummaries(data, false);
  const waiting = projects.flatMap((item) => item.waiting_tasks.map((task) => ({ project: item.project, task }))).slice(0, 12);
  const edges = projects.flatMap((item) => item.cross_project_edges.map((edge) => ({ project: item.project, edge }))).slice(0, 12);
  return `
    <div class="record-list">
      ${waiting.map(({ project: projectName, task }) => `
        <article class="record compact">
          <div class="record-header"><div><h3>${escapeHtml(task.title)}</h3><p class="muted small">${escapeHtml(projectName)}</p></div>${chip("waiting", "warn")}</div>
          <p class="muted small mono">${escapeHtml((task.waiting_on || []).join(" -> "))}</p>
        </article>
      `).join("")}
      ${edges.map(({ project: projectName, edge }) => `
        <article class="record compact">
          <div class="record-header"><div><h3>${escapeHtml(String(edge.from_project || "project"))} -> ${escapeHtml(projectName)}</h3><p class="muted small">${escapeHtml(String(edge.from_task_title || edge.from_task_id || ""))}</p></div>${chip("cross-project", "warn")}</div>
          <p class="muted small">${escapeHtml(String(edge.to_task_title || edge.to_task_id || ""))}</p>
        </article>
      `).join("")}
      ${!waiting.length && !edges.length ? `<div class="empty-state">No dependency waits in scope</div>` : ""}
    </div>
  `;
}

function filteredAgents(data: DashboardData): AgentItem[] {
  const query = state.agentQuery.trim().toLowerCase();
  const agents = data.agents.filter((item) => {
    const projects = item.active_projects || [];
    const fleetLabel = agentFleetLabel(data, item.agent.id);
    const role = roleLabel(data, item.agent.role_id);
    const haystack = [
      item.agent.name,
      item.agent.id,
      item.machine?.hostname || "",
      fleetLabel,
      role,
      item.agent.status,
      item.agent.health_status,
      ...projects,
      ...(item.agent.capabilities || []),
    ].join(" ").toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    const matchesFilter =
      state.agentFilter === "all" ||
      (state.agentFilter === "eligible" && item.availability.eligible) ||
      (state.agentFilter === "blocked" && !item.availability.eligible) ||
      item.agent.status === state.agentFilter ||
      item.agent.health_status === state.agentFilter;
    return matchesQuery && matchesFilter;
  });
  return agents.sort(agentSort);
}

function agentSort(left: AgentItem, right: AgentItem): number {
  const data = mustData();
  if (state.agentSort === "fleet") return compareText(agentFleetLabel(data, left.agent.id), agentFleetLabel(data, right.agent.id)) || compareText(left.agent.name, right.agent.name);
  if (state.agentSort === "status") return compareText(`${left.agent.status} ${left.agent.name}`, `${right.agent.status} ${right.agent.name}`);
  if (state.agentSort === "project") return compareText((left.active_projects || []).join(",") || "idle", (right.active_projects || []).join(",") || "idle") || compareText(left.agent.name, right.agent.name);
  if (state.agentSort === "capacity") return (right.capacity - right.active_lease_count) - (left.capacity - left.active_lease_count) || compareText(left.agent.name, right.agent.name);
  if (state.agentSort === "last_seen") return compareText(String(right.agent.last_seen_at || ""), String(left.agent.last_seen_at || ""));
  return compareText(left.agent.name, right.agent.name);
}

function compareText(left: string, right: string): number {
  return left.localeCompare(right, undefined, { sensitivity: "base", numeric: true });
}

function agentTable(agents: AgentItem[], data: DashboardData, compact = false): string {
  if (!agents.length) return `<div class="empty-state">No matching agents</div>`;
  return `
    <div class="table-wrap responsive-table">
      <table class="data-table ${compact ? "compact-table" : ""}">
        <thead><tr><th>Agent</th><th>Fleet</th><th>Role</th><th>Project</th><th>Status</th><th>Health</th><th>Capacity</th><th>Machine</th><th>Last Seen</th><th>Capabilities</th><th>Task</th><th></th></tr></thead>
        <tbody>
          ${agents.map((item) => agentRow(item, data)).join("")}
        </tbody>
      </table>
    </div>
    <div class="mobile-card-list">
      ${agents.map((item) => agentMobileCard(item, data)).join("")}
    </div>
  `;
}

function agentRow(item: AgentItem, data: DashboardData): string {
  const task = item.active_tasks[0];
  const writable = canWrite(data);
  return `
    <tr class="${selectedClass(item.agent.id)}">
      <td><button class="link-button mono" type="button" data-select-id="${escapeHtml(item.agent.id)}">${escapeHtml(item.agent.name)}</button><br><span class="muted small">${escapeHtml(item.agent.id)}</span></td>
      <td>${escapeHtml(agentFleetLabel(data, item.agent.id))}</td>
      <td>${escapeHtml(roleLabel(data, item.agent.role_id))}</td>
      <td>${escapeHtml((item.active_projects || []).join(", ") || "idle")}</td>
      <td>${chip(item.agent.status, statusTone(item.agent.status))}</td>
      <td>${chip(item.agent.health_status, healthTone(item.agent.health_status))}</td>
      <td class="mono">${item.active_lease_count} / ${item.capacity}</td>
      <td>${escapeHtml(item.machine?.hostname || "missing")}</td>
      <td>${escapeHtml(formatAge(item.agent.last_seen_at))}</td>
      <td>${escapeHtml((item.agent.capabilities || []).slice(0, 8).join(", ") || "none")}</td>
      <td>${task ? storyButton(task) : `<span class="muted small">none</span>`}</td>
      <td>
        <div class="table-actions">
          <button class="link-button" type="button" data-select-id="${escapeHtml(item.agent.id)}">Inspect</button>
          <form class="inline-form" data-action="agentBulkUpdate">
            <input type="hidden" name="agent_ids" value="${escapeHtml(item.agent.id)}">
            <input type="hidden" name="status" value="draining">
            <button type="submit" ${disabledAttr(!writable)}>Drain</button>
          </form>
        </div>
      </td>
    </tr>
  `;
}

function agentMobileCard(item: AgentItem, data: DashboardData): string {
  const task = item.active_tasks[0];
  const writable = canWrite(data);
  return `
    <article class="mobile-object-card ${item.availability.eligible ? "" : "is-blocked"} ${selectedClass(item.agent.id)}">
      <div class="record-header">
        <div>
          <h3>${escapeHtml(item.agent.name)}</h3>
          <p class="muted small mono">${escapeHtml(item.agent.id)}</p>
        </div>
        <div class="chip-row">${chip(item.agent.status, statusTone(item.agent.status))}${chip(item.agent.health_status, healthTone(item.agent.health_status))}</div>
      </div>
      <div class="row-grid compact-grid">
        ${field("Fleet", agentFleetLabel(data, item.agent.id))}
        ${field("Role", roleLabel(data, item.agent.role_id))}
        ${field("Capacity", `${item.active_lease_count} / ${item.capacity}`)}
        ${field("Machine", item.machine?.hostname || "missing")}
      </div>
      <div class="chip-row">
        ${(item.agent.capabilities || []).slice(0, 6).map((capability) => chip(capability, "info")).join("")}
        ${task ? chip(task.title, "warn") : chip("idle", "good")}
      </div>
      <div class="mobile-card-actions">
        <button class="link-button" type="button" data-select-id="${escapeHtml(item.agent.id)}">Inspect</button>
        <form class="inline-form" data-action="agentBulkUpdate">
          <input type="hidden" name="agent_ids" value="${escapeHtml(item.agent.id)}">
          <input type="hidden" name="status" value="draining">
          <button type="submit" ${disabledAttr(!writable)}>Drain</button>
        </form>
      </div>
    </article>
  `;
}

function agentInspector(data: DashboardData): string {
  const agents = filteredAgents(data);
  const item = agents.find((candidate) => candidate.agent.id === state.selectedId)
    || data.agents.find((candidate) => candidate.agent.id === state.selectedId)
    || agents[0];
  if (!item) return "";
  const agent = item.agent;
  const writable = canWrite(data);
  const reasons = item.availability.eligible
    ? chip("dispatch eligible", "good")
    : item.availability.reasons.map((reason) => chip(reason, "bad")).join("");
  return `
    <section class="object-inspector">
      <div class="object-inspector-header">
        <div>
          <p class="eyebrow">Agent Inspector</p>
          <h2>${escapeHtml(agent.name)}</h2>
          <p class="muted small mono">${escapeHtml(agent.id)}</p>
        </div>
        <div class="chip-row">${chip(agent.status, statusTone(agent.status))}${chip(agent.health_status, healthTone(agent.health_status))}${writable ? chip("write token", "good") : chip("read-only token", "warn")}</div>
      </div>
      <div class="row-grid">
        ${field("Fleet", agentFleetLabel(data, agent.id))}
        ${field("Role", roleLabel(data, agent.role_id))}
        ${field("Machine", item.machine?.hostname || "missing")}
        ${field("Trusted", item.machine?.trusted ? "yes" : "no")}
        ${field("Last seen", formatAge(agent.last_seen_at))}
        ${field("Capacity", `${item.active_lease_count} / ${item.capacity}`)}
        ${field("Hermes", agent.hermes_instance_id || "unbound")}
        ${field("Projects", (item.active_projects || []).join(", ") || "idle")}
        ${field("Current task", item.active_tasks[0]?.title || agent.current_task_id || "none")}
        ${field("Machine resources", jsonSummary(item.machine?.resources))}
      </div>
      <div class="chip-row">${reasons}</div>
      <form class="action-form inspector-form" data-action="agentUpdate" data-agent-id="${escapeHtml(agent.id)}">
        <label>Name <input name="name" value="${escapeHtml(agent.name)}" ${disabledAttr(!writable)}></label>
        <label>Status ${select("status", ["idle", "busy", "draining", "offline"], agent.status, !writable)}</label>
        <label>Health ${select("health_status", ["healthy", "degraded", "unhealthy"], agent.health_status, !writable)}</label>
        <label>Hermes Instance ID <input name="hermes_instance_id" value="${escapeHtml(agent.hermes_instance_id || "")}" ${disabledAttr(!writable)}></label>
        <label>Capabilities <input name="capabilities" value="${escapeHtml((agent.capabilities || []).join(","))}" ${disabledAttr(!writable)}></label>
        <label>Resources JSON <textarea class="json-editor" name="resources" ${disabledAttr(!writable)}>${escapeHtml(JSON.stringify(agent.resources || {}, null, 2))}</textarea></label>
        <button type="submit" ${disabledAttr(!writable)}>Save Agent</button>
      </form>
      <div class="record-section danger-zone">
        <div class="record-header">
          <div>
            <h3>Delete Agent</h3>
            <p class="muted small">Removes the agent inventory record and any associated view state.</p>
          </div>
          <button class="danger-button" type="button" data-agent-delete="${escapeHtml(agent.id)}" ${disabledAttr(!writable)}>Delete</button>
        </div>
      </div>
    </section>
  `;
}

function swarmBuckets(items: Array<{ key: string; count: number }>): string {
  if (!items.length) return `<div class="empty-state">No data</div>`;
  const total = items.reduce((sum, item) => sum + item.count, 0) || 1;
  return `
    <div class="bucket-list">
      ${items.slice(0, 16).map((item) => `
        <button class="bucket-row" type="button" data-agent-filter-value="${escapeHtml(item.key)}">
          <span>${escapeHtml(item.key)}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${Math.max(2, (item.count / total) * 100)}%"></span></span>
          <span class="mono small">${item.count}</span>
        </button>
      `).join("")}
    </div>
  `;
}

function agentCard(item: AgentItem, data: DashboardData): string {
  const agent = item.agent;
  const machine = item.machine;
  const reasons = item.availability.eligible
    ? chip("dispatch eligible", "good")
    : item.availability.reasons.map((reason) => chip(reason, "bad")).join("");
  return `
    <article class="agent-card ${item.availability.eligible ? "" : "is-blocked"} ${selectedClass(agent.id)}">
      <div class="agent-header">
        <div><h2 class="mono">${escapeHtml(agent.name)}</h2><p class="muted small">${escapeHtml(agent.id)}</p></div>
        <div class="chip-row">${chip(agent.status, statusTone(agent.status))}${chip(agent.health_status, healthTone(agent.health_status))}<button class="link-button" type="button" data-select-id="${escapeHtml(agent.id)}">Select</button></div>
      </div>
      <div class="row-grid">
        ${field("Fleet", agentFleetLabel(data, agent.id))}
        ${field("Role", roleLabel(data, agent.role_id))}
        ${field("Machine", machine?.hostname || "missing")}
        ${field("Trusted", machine?.trusted ? "yes" : "no")}
        ${field("Last seen", formatAge(agent.last_seen_at))}
        ${field("Capacity", `${item.active_lease_count} / ${item.capacity}`)}
        ${field("Hermes", agent.hermes_instance_id || "unbound")}
        ${field("Current task", item.active_tasks[0]?.title || agent.current_task_id || "none")}
        ${field("Capabilities", (agent.capabilities || []).join(", ") || "none")}
        ${field("Resources", jsonSummary(agent.resources))}
        ${field("Machine resources", jsonSummary(machine?.resources))}
      </div>
      <div class="chip-row">${reasons}</div>
    </article>
  `;
}

function taskLane(taskState: string, tasks: TaskDetail[], agents: AgentItem[]): string {
  const laneTasks = tasks.filter((detail) => detail.task.state === taskState);
  return `
    <div class="task-lane status-${escapeHtml(taskState)}">
      <h2><span class="lane-title">${escapeHtml(labelize(taskState))}</span><span class="pill lane-count">${laneTasks.length}</span></h2>
      ${laneTasks.length ? laneTasks.map((detail) => taskCard(detail, agents)).join("") : `<div class="empty-state">Empty</div>`}
    </div>
  `;
}

function taskCard(detail: TaskDetail, agents: AgentItem[]): string {
  const task = detail.task;
  const owner = agents.find((item) => item.agent.id === task.owner_agent_id)?.agent;
  const origin = taskOrigin(task);
  const isSelected = state.selectedId === task.id;
  const recentHistory = detail.history.slice(-3);
  const summaryText = String(detail.summary?.summary || "");
  return `
    <article class="task-card status-${escapeHtml(task.state)} ${selectedClass(task.id)}">
      <div class="record-header">
        <div class="task-card-heading"><h3>${escapeHtml(task.title)}</h3><p class="muted small mono task-id" title="${escapeHtml(task.id)}">${escapeHtml(task.id)}</p></div>
        <button class="select-button${isSelected ? " is-selected" : ""}" type="button" data-select-id="${escapeHtml(task.id)}" aria-pressed="${isSelected ? "true" : "false"}">${isSelected ? "Selected" : "Inspect"}</button>
      </div>
      <div class="chip-row">
        ${chip(`P${task.priority || 0}`, "info")}
        ${chip(`${task.attempt_count || 0}/${task.max_attempts || 0} attempts`, (task.attempt_count || 0) >= (task.max_attempts || 1) ? "bad" : "good")}
        ${owner ? chip(owner.name, "info") : chip("unowned", "warn")}
        ${origin.hermes_instance_id ? chip("Hermes origin", "info") : ""}
      </div>
      <div class="time-summary">
        <span class="time-cell"><span class="time-label">Started</span><span class="time-value">${escapeHtml(task.started_at ? formatAge(task.started_at) : "not started")}</span></span>
        <span class="time-cell"><span class="time-label">Completed</span><span class="time-value">${escapeHtml(task.completed_at ? formatAge(task.completed_at) : "not completed")}</span></span>
        <span class="time-cell"><span class="time-label">Updated</span><span class="time-value">${escapeHtml(formatAge(task.last_updated_at || task.updated_at))}</span></span>
      </div>
      ${summaryText ? `<p class="small muted">${escapeHtml(summaryText)}</p>` : ""}
      ${recentHistory.length ? `<details class="activity-disclosure">
        <summary><span class="activity-show">Show activity</span><span class="activity-hide">Hide activity</span></summary>
        <div class="timeline">
          ${recentHistory.map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("")}
        </div>
      </details>` : ""}
      <div class="record-actions">
        <button class="link-button" type="button" data-select-id="${escapeHtml(task.id)}">Inspect</button>
      </div>
    </article>
  `;
}

function taskInspector(tasks: TaskDetail[], data: DashboardData): string {
  if (!tasks.length) return "";
  const detail = tasks.find((candidate) => candidate.task.id === state.selectedId)
    || data.tasks.find((candidate) => candidate.task.id === state.selectedId)
    || tasks[0];
  if (!detail) return "";
  const task = detail.task;
  const owner = data.agents.find((item) => item.agent.id === task.owner_agent_id)?.agent;
  const evidenceOptions = detail.evidence.map((item) => option(String(item.id), String(item.id), "")).join("");
  const pendingReviews = detail.reviews.filter((review) => review.status === "pending");
  const llmRoutes = llmRoutesForTask(data, task.id);
  return `
    <section class="object-inspector">
      <div class="object-inspector-header">
        <div>
          <p class="eyebrow">Task Inspector</p>
          <h2>${escapeHtml(task.title)}</h2>
          <p class="muted small mono">${escapeHtml(task.id)}</p>
        </div>
        <div class="chip-row">
          ${chip(task.state, statusTone(task.state))}
          ${chip(`P${task.priority || 0}`, "info")}
          ${owner ? chip(owner.name, "info") : chip("unowned", "warn")}
        </div>
      </div>
      <div class="row-grid">
        ${field("Project", taskProject(task))}
        ${field("Owner", owner?.name || task.owner_agent_id || "none")}
        ${field("Attempts", `${task.attempt_count || 0} / ${task.max_attempts || 0}`)}
        ${field("Dependencies", (task.dependencies || []).join(", ") || "none")}
        ${field("Required", (task.required_capabilities || []).join(", ") || "none")}
        ${field("Evidence", detail.evidence.length)}
        ${field("Reviews", detail.reviews.length)}
        ${field("Updated", formatAge(task.last_updated_at || task.updated_at))}
      </div>
      <form class="action-form inspector-form" data-action="taskUpdate" data-task-id="${escapeHtml(task.id)}">
        <label>Title <input name="title" value="${escapeHtml(task.title)}"></label>
        <label>Project <input name="project" value="${escapeHtml(task.project || "")}"></label>
        <label>Priority <input name="priority" type="number" value="${escapeHtml(task.priority || 0)}"></label>
        <label>Capabilities <input name="required_capabilities" value="${escapeHtml((task.required_capabilities || []).join(","))}"></label>
        <label>Dependencies <input name="dependencies" value="${escapeHtml((task.dependencies || []).join(","))}"></label>
        <label>Description <textarea name="description">${escapeHtml(String(task.description || ""))}</textarea></label>
        <label>Metadata JSON <textarea class="json-editor" name="metadata">${escapeHtml(JSON.stringify(task.metadata || {}, null, 2))}</textarea></label>
        <button type="submit">Save Task</button>
      </form>
      <details class="action-box action-drawer inline-drawer">
        <summary>
          <span>Lifecycle Actions</span>
          <span class="muted small">Claim, start, review, or transition this task</span>
        </summary>
        <form class="action-form compact" data-action="taskClaim" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", data.agents, task.owner_agent_id || "")}</label>
          <label>Lease seconds <input name="lease_seconds" type="number" value="900" min="1"></label>
          <button type="submit">Claim</button>
        </form>
        <form class="action-form compact" data-action="taskStart" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", data.agents, task.owner_agent_id || "")}</label>
          <button type="submit">Start</button>
        </form>
        <form class="action-form compact" data-action="taskSubmitReview" data-task-id="${escapeHtml(task.id)}">
          <label>Agent ${agentSelect("agent_id", data.agents, task.owner_agent_id || "")}</label>
          <button type="submit">Submit Review</button>
        </form>
        <form class="action-form" data-action="taskTransition" data-task-id="${escapeHtml(task.id)}">
          <label>State ${select("target_state", TASK_STATES, task.state)}</label>
          <label>Actor <input name="actor" value="human"></label>
          <label>Detail JSON <textarea class="json-editor" name="detail" placeholder="{}"></textarea></label>
          <button type="submit">Transition</button>
        </form>
      </details>
      <details class="action-box action-drawer inline-drawer">
        <summary>
          <span>Children, Evidence, Reviews</span>
          <span class="muted small">Attach proof or create follow-on work</span>
        </summary>
        <form class="action-form compact" data-action="taskAddChild" data-task-id="${escapeHtml(task.id)}">
          <label>Child title <input name="title" required></label>
          <label>Description <textarea name="description"></textarea></label>
          <label>Project <input name="project" value="${escapeHtml(task.project || "")}"></label>
          <label>Capabilities <input name="required_capabilities" value="${escapeHtml((task.required_capabilities || []).join(","))}"></label>
          <label>Dependencies <input name="dependencies"></label>
          <label>Actor <input name="actor" value="human"></label>
          <button type="submit">Add Child</button>
        </form>
        <form class="action-form" data-action="addEvidence" data-task-id="${escapeHtml(task.id)}">
          <label>Kind ${select("kind", ["test", "review", "artifact", "publication", "log", "eval"], "test")}</label>
          <label>URI <input name="uri" placeholder="artifact://..."></label>
          <label>Summary <input name="summary" placeholder="What this proves"></label>
          <label>Checksum <input name="checksum" placeholder="optional"></label>
          <label>Created by <input name="created_by" value="${escapeHtml(task.owner_agent_id || "human")}"></label>
          <button type="submit">Add Evidence</button>
        </form>
        <form class="action-form compact" data-action="requestReview" data-task-id="${escapeHtml(task.id)}">
          <label>Reviewer ${agentSelect("reviewer_agent_id", data.agents, "")}</label>
          <label>Actor <input name="actor" value="dispatcher"></label>
          <button type="submit">Request Review</button>
        </form>
        ${pendingReviews.map((review) => `
          <form class="action-form" data-action="reviewDecision" data-review-id="${escapeHtml(review.id)}">
            <label>Status ${select("status", ["approved", "changes_requested", "rejected"], "approved")}</label>
            <label>Reviewer <input name="reviewer_agent_id" value="${escapeHtml(review.reviewer_agent_id)}"></label>
            <label>Evidence <select name="evidence_id"><option value="">None</option>${evidenceOptions}</select></label>
            <label>Reason <input name="reason" placeholder="optional"></label>
            <button type="submit">Submit Review</button>
          </form>`).join("")}
        <form class="action-form compact" data-action="publishTask">
          <input type="hidden" name="task_id" value="${escapeHtml(task.id)}">
          <label>Target <input name="target" placeholder="release://..."></label>
          <label>Created by <input name="created_by" value="human"></label>
          <label>Evidence <select name="evidence_id"><option value="">None</option>${evidenceOptions}</select></label>
          <button type="submit">Publish</button>
        </form>
      </details>
      <div class="record-section">
        <h3>LLM Routes</h3>
        <div class="observability-feed">
          ${llmRoutes.length ? llmRoutes.slice(0, 8).map(llmRouteRecord).join("") : `<div class="empty-state">No LLM routes recorded for this task</div>`}
        </div>
      </div>
      <div class="record-section">
        <h3>History</h3>
        <div class="timeline">
          ${detail.history.length ? detail.history.slice(-8).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("") : `<div class="empty-state">No history</div>`}
        </div>
      </div>
      <div class="record-section danger-zone">
        <div class="record-header">
          <div>
            <h3>Delete Task</h3>
            <p class="muted small">Deletes the selected task record.</p>
          </div>
          <button class="danger-button" type="button" data-task-delete="${escapeHtml(task.id)}">Delete</button>
        </div>
      </div>
    </section>
  `;
}

function hermesRecord(instance: ApiRecord, data: DashboardData): string {
  const tenant = data.tenants.find((item) => item.id === instance.tenant_id);
  const persona = data.personas.find((item) => item.id === instance.persona_id);
  const bindings = data.platform_bindings.filter((binding) => binding.hermes_instance_id === instance.id);
  const tasks = data.tasks.filter((detail) => taskOrigin(detail.task).hermes_instance_id === instance.id);
  const context = data.hermes_work_contexts?.[String(instance.id)];
  const proof = data.hermes_runtime_proofs?.[String(instance.id)];
  const contextProjects = context?.projects || [];
  const contextAgents = context?.agents || [];
  const hermesBridgeCommands = context?.operations.mac_hermes_cli || [];
  const operationCount = (context?.operations.api || []).length + hermesBridgeCommands.length;
  const proofEvidence = (proof?.evidence || {}) as JsonObject;
  const proofUi = (proofEvidence.ui || {}) as JsonObject;
  const proofRuntime = (proofEvidence.hermes_runtime || {}) as JsonObject;
  const proofWork = (proofEvidence.work_context || {}) as JsonObject;
  const proofApi = (proofEvidence.api || {}) as JsonObject;
  const liveAlignment = (proofEvidence.live_alignment || {}) as JsonObject;
  const dashboardUrlContract = (proofUi.dashboard_url_contract || {}) as JsonObject;
  const dashboardOperationContract = (proofUi.dashboard_operation_contract || {}) as JsonObject;
  const proofObjects = (proofEvidence.first_class_objects || {}) as Record<string, JsonObject>;
  const proofObjectEntries = Object.entries(proofObjects);
  const readyObjectCount = proofObjectEntries.filter(([, value]) => Boolean(value.ready)).length;
  const proofSessionCapabilities = (proofRuntime.session_capability_names || []) as unknown[];
  const proofSessionAvailability = (proofRuntime.session_capability_availability || {}) as JsonObject;
  const unavailableSessionCapabilities = (proofSessionAvailability.missing || []) as unknown[];
  const unavailableSessionCapabilityNames = new Set(unavailableSessionCapabilities.map((item) => String(item)));
  const availableSessionCapabilityCount = Math.max(0, proofSessionCapabilities.length - unavailableSessionCapabilities.length);
  const taskOperationCount = ((proofApi.task_operation_names || []) as unknown[]).length;
  const projectOperationCount = ((proofApi.project_operation_names || []) as unknown[]).length;
  const agentOperationCount = ((proofApi.agent_operation_names || []) as unknown[]).length;
  const proofMissing = proof?.missing || [];
  return `
    <article class="record">
      <div class="record-header"><div><h2>${escapeHtml(instance.name)}</h2><p class="muted small mono">${escapeHtml(instance.id)}</p></div>${chip(instance.status, instance.status === "active" ? "good" : "warn")}</div>
      <div class="row-grid">
        ${field("Tenant", tenant?.name || instance.tenant_id)}
        ${field("Persona", persona?.name || "none")}
        ${field("Soul ref", persona?.soul_ref || "none")}
        ${field("Memory scope", persona?.memory_scope || "none")}
        ${field("Home", instance.home_ref || "none")}
        ${field("Bindings", bindings.length)}
        ${field("Interaction tasks", tasks.length)}
        ${field("Last seen", formatAge(String(instance.last_seen_at || "")))}
      </div>
      <div class="chip-row">${bindings.length ? bindings.map((binding) => chip(`${binding.platform}:${binding.display_name || binding.external_id}`, "info")).join("") : chip("no platform bindings", "warn")}</div>
      ${context ? `
        <div class="record-section">
          <h3>Work Context</h3>
          <div class="row-grid">
            ${field("Task authority", context.authority.tasks || "mac")}
            ${field("Project authority", context.authority.projects || "mac")}
            ${field("Visible tasks", context.task_count)}
            ${field("Projects", contextProjects.length)}
            ${field("Agents", contextAgents.length)}
            ${field("Operations", operationCount)}
          </div>
          <div class="chip-row">${contextProjects.slice(0, 8).map((project) => chip(`${project.project}:${project.active_count}/${project.task_count}`, project.active_count ? "info" : "good")).join("") || chip("no projects", "warn")}</div>
          <h4>Bridge Commands</h4>
          <div class="chip-row">
            ${hermesBridgeCommands.some((command) => command.includes("project")) ? chip("project bridge", "good") : chip("project bridge missing", "bad")}
            ${hermesBridgeCommands.some((command) => command.includes("claim") || command.includes("task")) ? chip("task lifecycle", "good") : chip("task lifecycle missing", "bad")}
            ${hermesBridgeCommands.some((command) => command.includes("agents") || command.includes("agent-")) ? chip("agent view", "good") : chip("agent view missing", "bad")}
            ${hermesBridgeCommands.some((command) => command.includes("command-audit")) ? chip("command audit", "good") : chip("command audit missing", "bad")}
            ${hermesBridgeCommands.some((command) => command.includes("web-search")) ? chip("web research", "good") : chip("web research missing", "bad")}
          </div>
          <div class="timeline">
            ${hermesBridgeCommands.slice(0, 12).map((command) => timelineItem("mac-hermes", command, "bridge command")).join("")}
          </div>
          <div class="timeline">
            ${(context.tasks || []).slice(0, 4).map((task) => timelineItem(task.state, task.title, `${task.project || taskProject(task)} / ${task.id}`)).join("") || timelineItem("idle", "No visible tasks", "")}
          </div>
        </div>
      ` : ""}
      ${proof ? `
        <div class="record-section">
          <h3>Runtime Proof</h3>
          <div class="chip-row">
            ${chip(proof.ready ? "ready" : "degraded", proof.ready ? "good" : "bad")}
            ${proofMissing.slice(0, 4).map((item) => chip(item, "warn")).join("")}
          </div>
          <div class="row-grid">
            ${field("Schema", proof.schema)}
            ${field("Live alignment", liveAlignment.ready ? "aligned" : "not proven")}
            ${field("Runtime", proofRuntime.status || "not required")}
            ${field("Prompt bridge", ((proofRuntime.prompt_bridge || {}) as JsonObject).present ? "active" : "not required")}
            ${field("Dashboard URLs", dashboardUrlContract.ready ? "ready" : "not proven")}
            ${field("URL params", dashboardParameterNames(dashboardUrlContract).length || dashboardParameterNames(dashboardOperationContract).length)}
            ${field("Session caps", `${availableSessionCapabilityCount}/${proofSessionCapabilities.length}`)}
            ${field("Objects", `${readyObjectCount}/${proofObjectEntries.length || 3}`)}
            ${field("Task ops", String(taskOperationCount))}
            ${field("Project ops", String(projectOperationCount))}
            ${field("Agent ops", String(agentOperationCount))}
            ${field("Project links", `${proofWork.project_bridge_item_count || 0}/${proofWork.beads_repository_count || 0}`)}
            ${field("Bound agents", String(((proofWork.bound_agent_ids || []) as unknown[]).length))}
          </div>
          <h4>First-Class Objects</h4>
          <div class="chip-row">${proofObjectEntries.map(([name, value]) => chip(`${name}:${value.authority || "?"}`, value.ready ? "good" : "bad")).join("") || chip("object proof missing", "warn")}</div>
          ${firstClassCouplingMatrix(proofObjectEntries)}
          <h4>Dashboard Links</h4>
          ${dashboardUrlContractPanel(dashboardUrlContract)}
          <h4>Session Capabilities</h4>
          <div class="chip-row">
            ${proofSessionCapabilities.map((name) => {
              const label = String(name);
              return chip(label, unavailableSessionCapabilityNames.has(label) ? "bad" : "good");
            }).join("") || chip("session capability proof missing", "warn")}
          </div>
        </div>
      ` : ""}
    </article>
  `;
}

function firstClassCouplingMatrix(entries: Array<[string, JsonObject]>): string {
  if (!entries.length) return `<div class="empty-state">First-class coupling proof missing</div>`;
  return `
    <div class="table-wrap">
      <table class="data-table compact-table">
        <thead>
          <tr>
            <th>Object</th>
            <th>API</th>
            <th>MAC CLI</th>
            <th>Hermes CLI</th>
            <th>UI Projection</th>
            <th>Runtime</th>
          </tr>
        </thead>
        <tbody>
          ${entries.map(([name, proof]) => firstClassCouplingRow(name, proof)).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function firstClassCouplingRow(name: string, proof: JsonObject): string {
  const dashboardProjection = (proof.dashboard_projection || {}) as JsonObject;
  const dashboardFields = arrayOfStrings(dashboardProjection.fields).slice(0, 4);
  const dashboardUrls = arrayOfStrings(dashboardProjection.urls).slice(0, 3);
  const stateKey = String(dashboardProjection.state_key || "dashboard");
  return `
    <tr>
      <td>${chip(name, proof.ready ? "good" : "bad")}</td>
      <td>${proofList(proof, "api_operations", "api")}</td>
      <td>${proofList(proof, "mac_cli_commands", "mac")}</td>
      <td>${proofList(proof, "mac_hermes_cli_commands", "hermes")}</td>
      <td>
        ${chip(stateKey, proof.dashboard_ready ? "good" : "bad")}
        <div class="chip-row">${dashboardFields.map((fieldName) => chip(fieldName, "info")).join("") || chip("fields missing", "bad")}</div>
        <div class="chip-row">${dashboardLinkChips(dashboardUrls, "urls missing")}</div>
      </td>
      <td>${proofList(proof, "runtime_capabilities", "runtime")}</td>
    </tr>
  `;
}

function proofList(proof: JsonObject, key: string, emptyLabel: string): string {
  const values = arrayOfStrings(proof[key]).slice(0, 4);
  if (!values.length) return chip(`${emptyLabel} missing`, "bad");
  return `<div class="chip-row">${values.map((value) => chip(value, "info")).join("")}</div>`;
}

function dashboardUrlContractPanel(contract: JsonObject): string {
  if (!contract.schema) return `<div class="empty-state">Dashboard URL proof missing</div>`;
  const objectLinks = (contract.object_deep_links || {}) as Record<string, JsonObject>;
  const params = dashboardParameterNames(contract);
  const missing = arrayOfStrings(contract.missing);
  return `
    <div class="row-grid">
      ${field("Entrypoint", contract.entrypoint || "/ui")}
      ${field("Contract", contract.ready ? "ready" : "degraded")}
      ${field("Views", arrayOfStrings(contract.required_views).join(", ") || "none")}
      ${field("Parameters", params.join(", ") || "none")}
    </div>
    ${missing.length ? `<div class="chip-row">${missing.map((item) => chip(item, "warn")).join("")}</div>` : ""}
    <div class="record-list">
      ${Object.entries(objectLinks).map(([name, links]) => dashboardDeepLinkRecord(name, links)).join("") || `<div class="empty-state">No dashboard links</div>`}
    </div>
  `;
}

function dashboardDeepLinkRecord(name: string, links: JsonObject): string {
  const templates = arrayOfStrings(links.templates).slice(0, 4);
  const samples = arrayOfStrings(links.samples).slice(0, 4);
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(labelize(name))}</h3><p class="muted small">${templates.length} templates, ${samples.length} samples</p></div>${chip(links.ready ? "ready" : "missing", links.ready ? "good" : "bad")}</div>
      <div class="chip-row">${dashboardLinkChips(samples, "samples missing")}</div>
      <div class="chip-row">${templates.map((url) => chip(url, "info")).join("") || chip("templates missing", "bad")}</div>
    </article>
  `;
}

function dashboardParameterNames(contract: JsonObject): string[] {
  const params = contract.url_state_parameters;
  if (!Array.isArray(params)) return [];
  return params
    .map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && "name" in item) return String((item as JsonObject).name || "");
      return "";
    })
    .filter((item) => item.trim());
}

function dashboardLinkChips(urls: string[], emptyLabel: string): string {
  if (!urls.length) return chip(emptyLabel, "bad");
  return urls.map((url) => dashboardLinkChip(url)).join("");
}

function dashboardLinkChip(url: string): string {
  return `<a class="chip tone-info" href="${escapeHtml(url)}" title="${escapeHtml(url)}">${escapeHtml(shortDashboardLink(url))}</a>`;
}

function shortDashboardLink(url: string): string {
  const query = url.includes("?") ? url.split("?", 2)[1] : "";
  const params = new URLSearchParams(query);
  const view = params.get("view") || "ui";
  const scope = params.get("project") || params.get("task_state") || params.get("selected") || "";
  return scope ? truncate(`${view}:${scope}`, 42) : truncate(view, 42);
}

function arrayOfStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item)).filter((item) => item.trim())
    : [];
}

function dependencySummary(value: unknown): string {
  if (!Array.isArray(value) || !value.length) return "none";
  const rendered = value.slice(0, 3).map((item) => {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      const dep = item as JsonObject;
      return String(dep.requirement || dep.name || JSON.stringify(dep));
    }
    return String(item);
  }).filter((item) => item.trim());
  const suffix = value.length > rendered.length ? ` +${value.length - rendered.length}` : "";
  return rendered.join(", ") + suffix;
}

function runtimeRecord(runtime: ApiRecord, data: DashboardData): string {
  const rollouts = data.rollouts.filter((item) => item.rollout.runtime_environment_id === runtime.id);
  const runs = data.runtime_runs.filter((run) => run.environment_id === runtime.id);
  const deltas = data.runtime_deltas.filter((delta) =>
    delta.base_runtime_id === runtime.id
    || delta.base_runtime_digest === runtime.digest
    || delta.promoted_runtime_environment_id === runtime.id
  );
  return `
    <article class="runtime-record ${selectedClass(String(runtime.id))}">
      <div class="runtime-header">
        <div><h3>${escapeHtml(runtime.name)}</h3><p class="muted small mono">${escapeHtml(runtime.id)}</p></div>
        <div class="chip-row">${chip(shortHash(runtime.digest), "good")}<button class="link-button" type="button" data-select-id="${escapeHtml(runtime.id)}">Inspect</button></div>
      </div>
      <div class="row-grid">
        ${field("Created by", runtime.created_by)}
        ${field("Created", formatAge(String(runtime.created_at || "")))}
        ${field("Rollouts", rollouts.length)}
        ${field("Runs", runs.length)}
        ${field("Deltas", deltas.length)}
        ${field("Manifest", jsonSummary(runtime.manifest))}
      </div>
    </article>
  `;
}

function runtimeDeltaRecord(delta: ApiRecord, data: DashboardData): string {
  const task = data.tasks.find((detail) => detail.task.id === delta.task_id)?.task;
  const agent = data.agents.find((item) => item.agent.id === delta.agent_id)?.agent;
  const base = data.runtimes.find((runtime) =>
    runtime.id === delta.base_runtime_id || runtime.digest === delta.base_runtime_digest
  );
  const status = String(delta.status || "proposed");
  const canValidate = status === "proposed" || status === "rejected";
  const canPromote = status === "validated";
  const disabled = disabledAttr(!canWrite(data));
  const validateDisabled = disabled || disabledAttr(!canValidate);
  const promoteDisabled = disabled || disabledAttr(!canPromote);
  const rejectDisabled = disabled || disabledAttr(status === "promoted");
  return `
    <article class="runtime-record ${selectedClass(String(delta.id))}">
      <div class="runtime-header">
        <div>
          <h3>${escapeHtml(task?.title || delta.reason || delta.id)}</h3>
          <p class="muted small mono">${escapeHtml(delta.id)}</p>
        </div>
        <div class="chip-row">${chip(status, runtimeDeltaTone(status))}<button class="link-button" type="button" data-select-id="${escapeHtml(String(delta.id))}">Inspect</button></div>
      </div>
      <div class="row-grid">
        ${field("Project", delta.project || task?.project || "default")}
        ${field("Agent", agent?.name || delta.agent_id)}
        ${field("Base runtime", base?.name || shortHash(delta.base_runtime_digest))}
        ${field("Package manager", delta.package_manager)}
        ${field("Dependencies", dependencySummary(delta.added_dependencies))}
        ${field("Lockfile", delta.lockfile_path || "none")}
      </div>
      <div class="record-actions">
        <form class="inline-form" data-action="runtimeDeltaValidate" data-delta-id="${escapeHtml(String(delta.id))}">
          <input type="hidden" name="actor" value="operator">
          <button type="submit" ${validateDisabled}>Validate</button>
        </form>
        <form class="inline-form" data-action="runtimeDeltaPromote" data-delta-id="${escapeHtml(String(delta.id))}">
          <input type="hidden" name="actor" value="operator">
          <button type="submit" ${promoteDisabled}>Promote</button>
        </form>
        <form class="inline-form" data-action="runtimeDeltaReject" data-delta-id="${escapeHtml(String(delta.id))}">
          <input name="reason" placeholder="Reason" ${rejectDisabled}>
          <input type="hidden" name="actor" value="operator">
          <button type="submit" ${rejectDisabled}>Reject</button>
        </form>
      </div>
    </article>
  `;
}

function rolloutRecord(status: RolloutStatus, data: DashboardData): string {
  const rollout = status.rollout;
  const evalSet = data.eval_sets.find((item) => item.id === rollout.required_eval_set_id);
  return `
    <article class="rollout-record ${selectedClass(String(rollout.id))}">
      <div class="rollout-header">
        <div><h3>${escapeHtml(rollout.version)}</h3><p class="muted small mono">${escapeHtml(rollout.id)}</p></div>
        <div class="chip-row">${chip(rollout.status, rolloutTone(String(rollout.status)))}<button class="link-button" type="button" data-select-id="${escapeHtml(rollout.id)}">Inspect</button></div>
      </div>
      <div class="row-grid">
        ${field("Strategy", rollout.strategy)}
        ${field("Target", `${rollout.target_percent}%`)}
        ${field("Channel", rollout.channel)}
        ${field("Runtime", status.runtime?.name || "none")}
        ${field("Artifact", rollout.artifact_hash || "unverified")}
        ${field("Eval gate", evalSet?.name || "none")}
        ${field("Latest eval", status.latest_eval_run ? `${status.latest_eval_run.score} ${status.latest_eval_run.passed ? "pass" : "fail"}` : "none")}
        ${field("Health policy", jsonSummary(rollout.health_policy))}
      </div>
      <div class="timeline">${status.events.slice(-4).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("")}</div>
    </article>
  `;
}

function secretRecord(secret: ApiRecord, agents: AgentItem[]): string {
  return `
    <article class="record ${selectedClass(String(secret.id))}">
      <div class="record-header">
        <div><h3>${escapeHtml(secret.name)}</h3><p class="muted small mono">${escapeHtml(secret.id)}</p></div>
        <div class="chip-row">${chip(secret.enabled ? "enabled" : "disabled", secret.enabled ? "good" : "bad")}<button class="link-button" type="button" data-select-id="${escapeHtml(secret.id)}">Inspect</button></div>
      </div>
      <div class="row-grid">
        ${field("Value", "***REDACTED***")}
        ${field("Scopes", jsonSummary(secret.scopes))}
        ${field("Created by", secret.created_by)}
        ${field("Rotated", secret.rotated_at || "never")}
      </div>
    </article>
  `;
}

function runtimeRunCard(run: ApiRecord, data: DashboardData): string {
  const task = data.tasks.find((detail) => detail.task.id === run.task_id)?.task;
  const agent = data.agents.find((item) => item.agent.id === run.agent_id)?.agent;
  const runtime = data.runtimes.find((item) => item.id === run.environment_id);
  return `
    <article class="mobile-object-card compact ${selectedClass(String(run.id))}">
      <div class="record-header">
        <div>
          <h3>${escapeHtml(task?.title || run.task_id || run.id)}</h3>
          <p class="muted small mono">${escapeHtml(run.id)}</p>
        </div>
        ${chip(run.status || "unknown", String(run.status) === "running" ? "info" : String(run.status) === "completed" ? "good" : "warn")}
      </div>
      <div class="row-grid compact-grid">
        ${field("Agent", agent?.name || run.agent_id)}
        ${field("Runtime", runtime?.name || run.environment_id)}
        ${field("Evidence", run.evidence_id || "none")}
        ${field("Updated", formatAge(String(run.updated_at || "")))}
      </div>
    </article>
  `;
}

function runtimeInspector(data: DashboardData): string {
  const selectedDelta = data.runtime_deltas.find((delta) => delta.id === state.selectedId) || null;
  const selectedRuntime = selectedDelta
    ? data.runtimes.find((runtime) =>
        runtime.id === selectedDelta.base_runtime_id
        || runtime.digest === selectedDelta.base_runtime_digest
        || runtime.id === selectedDelta.promoted_runtime_environment_id
      ) || null
    : data.runtimes.find((runtime) => runtime.id === state.selectedId) || data.runtimes[0];
  const selectedRollout = selectedDelta ? null : data.rollouts.find((item) => item.rollout.id === state.selectedId) || data.rollouts[0];
  if (!selectedRuntime && !selectedRollout && !selectedDelta) return "";
  const runtimeRollouts = selectedRuntime
    ? data.rollouts.filter((item) => item.rollout.runtime_environment_id === selectedRuntime.id)
    : [];
  const runtimeRuns = selectedRuntime
    ? data.runtime_runs.filter((run) => run.environment_id === selectedRuntime.id)
    : [];
  const runtimeDeltas = selectedRuntime
    ? data.runtime_deltas.filter((delta) =>
        delta.base_runtime_id === selectedRuntime.id
        || delta.base_runtime_digest === selectedRuntime.digest
        || delta.promoted_runtime_environment_id === selectedRuntime.id
      )
    : [];
  return `
    <section class="object-inspector">
      <div class="object-inspector-header">
        <div>
          <p class="eyebrow">${selectedDelta ? "Runtime Delta Inspector" : "Runtime Inspector"}</p>
          <h2>${escapeHtml(selectedDelta?.reason || selectedRuntime?.name || selectedRollout?.rollout.version || "Runtime")}</h2>
          <p class="muted small mono">${escapeHtml(selectedDelta?.id || selectedRuntime?.id || selectedRollout?.rollout.id || "")}</p>
        </div>
        <div class="chip-row">
          ${selectedDelta ? chip(selectedDelta.status, runtimeDeltaTone(String(selectedDelta.status))) : ""}
          ${selectedRuntime ? chip(`${runtimeRollouts.length} rollouts`, "info") : ""}
          ${selectedRuntime ? chip(`${runtimeRuns.length} runs`, runtimeRuns.length ? "good" : "warn") : ""}
          ${selectedRuntime ? chip(`${runtimeDeltas.length} deltas`, runtimeDeltas.length ? "warn" : "good") : ""}
        </div>
      </div>
      ${selectedDelta ? runtimeDeltaInspector(selectedDelta, data) : ""}
      ${selectedRuntime ? `
        <div class="row-grid">
          ${field("Created by", selectedRuntime.created_by)}
          ${field("Created", formatAge(String(selectedRuntime.created_at || "")))}
          ${field("Digest", shortHash(selectedRuntime.digest))}
          ${field("Runs", runtimeRuns.length)}
          ${field("Deltas", runtimeDeltas.length)}
        </div>
        <pre class="json-block">${escapeHtml(JSON.stringify(selectedRuntime.manifest || {}, null, 2))}</pre>
      ` : ""}
      ${selectedRollout ? rolloutInspector(selectedRollout, data) : ""}
    </section>
  `;
}

function runtimeDeltaInspector(delta: ApiRecord, data: DashboardData): string {
  const task = data.tasks.find((detail) => detail.task.id === delta.task_id)?.task;
  const agent = data.agents.find((item) => item.agent.id === delta.agent_id)?.agent;
  const base = data.runtimes.find((runtime) =>
    runtime.id === delta.base_runtime_id || runtime.digest === delta.base_runtime_digest
  );
  const promoted = data.runtimes.find((runtime) => runtime.id === delta.promoted_runtime_environment_id);
  return `
    <div class="row-grid">
      ${field("Task", task?.title || delta.task_id)}
      ${field("Agent", agent?.name || delta.agent_id)}
      ${field("Project", delta.project || task?.project || "default")}
      ${field("Base runtime", base?.name || shortHash(delta.base_runtime_digest))}
      ${field("Promoted runtime", promoted?.name || delta.promoted_runtime_environment_id || "none")}
      ${field("Package manager", delta.package_manager)}
      ${field("Dependencies", dependencySummary(delta.added_dependencies))}
      ${field("Lockfile", delta.lockfile_path || "none")}
      ${field("Lockfile digest", shortHash(delta.lockfile_digest))}
      ${field("Evidence", delta.evidence_id || "manual")}
    </div>
    <pre class="json-block">${escapeHtml(JSON.stringify({
      commands: delta.commands || [],
      validation: delta.validation || {},
    }, null, 2))}</pre>
  `;
}

function rolloutInspector(status: RolloutStatus, data: DashboardData): string {
  const rollout = status.rollout;
  const evalSet = data.eval_sets.find((item) => item.id === rollout.required_eval_set_id);
  return `
    <div class="record-section">
      <div class="object-inspector-header">
        <div>
          <p class="eyebrow">Rollout Inspector</p>
          <h2>${escapeHtml(rollout.version)}</h2>
          <p class="muted small mono">${escapeHtml(rollout.id)}</p>
        </div>
        <div class="chip-row">${chip(rollout.status, rolloutTone(String(rollout.status)))}${evalSet ? chip(`eval ${evalSet.name}`, "info") : chip("no eval gate", "warn")}</div>
      </div>
      <div class="row-grid">
        ${field("Strategy", rollout.strategy)}
        ${field("Target", `${rollout.target_percent}%`)}
        ${field("Tenant", rollout.tenant_id || "global")}
        ${field("Channel", rollout.channel)}
        ${field("Runtime", status.runtime?.name || rollout.runtime_environment_id || "none")}
        ${field("Artifact URI", rollout.artifact_uri || "none")}
        ${field("Artifact hash", rollout.artifact_hash || "unverified")}
        ${field("Latest eval", status.latest_eval_run ? `${status.latest_eval_run.score} ${status.latest_eval_run.passed ? "pass" : "fail"}` : "none")}
      </div>
      <div class="runtime-control-grid">
        <form class="action-form" data-action="rolloutAdvance" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Action ${select("action", ["start_canary", "promote", "pause", "resume", "rollback"], "start_canary")}</label>
          <label>Actor <input name="actor" value="human"></label>
          <label>Detail JSON <textarea class="json-editor" name="detail" placeholder="{}"></textarea></label>
          <button type="submit">Advance</button>
        </form>
        <form class="action-form" data-action="rolloutVerifyArtifact" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Artifact URI <input name="artifact_uri" value="${escapeHtml(rollout.artifact_uri || "")}" required></label>
          <label>Artifact hash <input name="artifact_hash" value="${escapeHtml(rollout.artifact_hash || "")}" required></label>
          <label>Actor <input name="actor" value="human"></label>
          <button type="submit">Verify Artifact</button>
        </form>
      </div>
      <div class="runtime-control-grid">
        <form class="action-form compact" data-action="rolloutHealth" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Actor <input name="actor" value="monitor"></label>
          <label>Checks JSON <textarea class="json-editor" name="checks" placeholder='{"runtime":"healthy"}'></textarea></label>
          <button type="submit">Record Health</button>
        </form>
        <form class="action-form compact danger-action" data-action="rolloutRescue" data-rollout-id="${escapeHtml(rollout.id)}">
          <label>Actor <input name="actor" value="human"></label>
          <label>Reason <input name="reason" placeholder="why rescue is needed"></label>
          <button type="submit">Rescue</button>
        </form>
      </div>
      <div class="timeline">${status.events.length ? status.events.slice(-8).map((event) => timelineItem(String(event.event_type), String(event.actor || ""), String(event.created_at || ""))).join("") : `<div class="empty-state">No rollout events</div>`}</div>
    </div>
  `;
}

function secretInspector(data: DashboardData): string {
  const secret = data.secrets.find((item) => item.id === state.selectedId) || data.secrets[0];
  if (!secret) return "";
  const audits = data.secret_audits.filter((audit) => audit.secret_id === secret.id);
  return `
    <section class="object-inspector">
      <div class="object-inspector-header">
        <div>
          <p class="eyebrow">Secret Inspector</p>
          <h2>${escapeHtml(secret.name)}</h2>
          <p class="muted small mono">${escapeHtml(secret.id)}</p>
        </div>
        <div class="chip-row">${chip(secret.enabled ? "enabled" : "disabled", secret.enabled ? "good" : "bad")}${chip(`${audits.length} audits`, audits.length ? "info" : "warn")}</div>
      </div>
      <div class="row-grid">
        ${field("Value", "***REDACTED***")}
        ${field("Scopes", jsonSummary(secret.scopes))}
        ${field("Created by", secret.created_by)}
        ${field("Created", formatAge(String(secret.created_at || "")))}
        ${field("Updated", formatAge(String(secret.updated_at || "")))}
        ${field("Rotated", secret.rotated_at || "never")}
      </div>
      <form class="action-form inspector-form" data-action="secretAccess" data-secret-id="${escapeHtml(secret.id)}">
        <label>Accessor ${agentSelect("accessor_agent_id", data.agents, "")}</label>
        <label>Purpose <input name="purpose" placeholder="deploy, test, audit"></label>
        <label>TTL seconds <input name="ttl_seconds" type="number" value="300" min="1"></label>
        <button type="submit">Request Handle</button>
      </form>
      <div class="record-section">
        <h3>Recent Access</h3>
        <div class="record-list">
          ${audits.length ? audits.slice(-8).map(secretAuditRecord).join("") : `<div class="empty-state">No audit records for this secret</div>`}
        </div>
      </div>
      <div class="record-section danger-zone">
        <div class="record-header">
          <div>
            <h3>Delete Secret</h3>
            <p class="muted small">Hard-deletes the secret row and scrubs the stored value.</p>
          </div>
          <button class="danger-button" type="button" data-secret-delete="${escapeHtml(secret.id)}">Delete</button>
        </div>
      </div>
    </section>
  `;
}

function secretAuditRecord(audit: ApiRecord): string {
  return `
    <article class="record compact">
      <div class="record-header"><div><h3>${escapeHtml(audit.result)}</h3><p class="muted small mono">${escapeHtml(audit.id)}</p></div>${chip(audit.result, audit.result === "granted" ? "good" : audit.result === "denied" ? "bad" : "warn")}</div>
      <div class="row-grid">
        ${field("Secret", audit.secret_id)}
        ${field("Accessor", audit.accessor_agent_id)}
        ${field("Purpose", audit.purpose)}
        ${field("Expires", audit.expires_at || "none")}
        ${field("Revealed", audit.revealed_at || "not revealed")}
        ${field("Created", formatAge(String(audit.created_at || "")))}
      </div>
    </article>
  `;
}

function bindViewControls(): void {
  const search = document.querySelector<HTMLInputElement>("#agentSearch");
  if (search) search.addEventListener("input", (event) => {
    state.agentQuery = (event.target as HTMLInputElement).value;
    state.agentPage = 1;
    updateUrlState(true);
    renderPreservingFocusedControl();
  });
  const projectFilter = document.querySelector<HTMLSelectElement>("#projectFilter");
  if (projectFilter) projectFilter.addEventListener("change", (event) => {
    state.projectFilter = (event.target as HTMLSelectElement).value;
    state.agentPage = 1;
    updateUrlState();
    render();
  });
  const showDerivedProjects = document.querySelector<HTMLInputElement>("#showDerivedProjects");
  if (showDerivedProjects) showDerivedProjects.addEventListener("change", (event) => {
    state.showDerivedProjects = (event.target as HTMLInputElement).checked;
    updateUrlState();
    render();
  });
  const agentFilter = document.querySelector<HTMLSelectElement>("#agentFilter");
  if (agentFilter) agentFilter.addEventListener("change", (event) => {
    state.agentFilter = (event.target as HTMLSelectElement).value;
    state.agentPage = 1;
    updateUrlState();
    render();
  });
  const agentSort = document.querySelector<HTMLSelectElement>("#agentSort");
  if (agentSort) agentSort.addEventListener("change", (event) => {
    state.agentSort = (event.target as HTMLSelectElement).value;
    updateUrlState();
    render();
  });
  const clearAgents = document.querySelector<HTMLButtonElement>("#clearAgentFilters");
  if (clearAgents) clearAgents.addEventListener("click", () => {
    state.agentQuery = "";
    state.agentFilter = "all";
    state.agentSort = "name";
    state.agentPage = 1;
    state.projectFilter = "all";
    updateUrlState();
    render();
  });
  const clearWorkScope = document.querySelector<HTMLButtonElement>("#clearWorkScope");
  if (clearWorkScope) clearWorkScope.addEventListener("click", () => {
    state.projectFilter = "all";
    state.selectedId = "";
    updateUrlState();
    render();
  });
  const prevAgentPage = document.querySelector<HTMLButtonElement>("#agentPrevPage");
  if (prevAgentPage) prevAgentPage.addEventListener("click", () => {
    state.agentPage = Math.max(1, state.agentPage - 1);
    updateUrlState();
    render();
  });
  const nextAgentPage = document.querySelector<HTMLButtonElement>("#agentNextPage");
  if (nextAgentPage) nextAgentPage.addEventListener("click", () => {
    state.agentPage += 1;
    updateUrlState();
    render();
  });
  const taskFilter = document.querySelector<HTMLSelectElement>("#taskFilter");
  if (taskFilter) taskFilter.addEventListener("change", (event) => {
    state.taskFilter = (event.target as HTMLSelectElement).value;
    updateUrlState();
    render();
  });
  const clearTasks = document.querySelector<HTMLButtonElement>("#clearTaskFilter");
  if (clearTasks) clearTasks.addEventListener("click", () => {
    state.taskFilter = "all";
    updateUrlState();
    render();
  });
  bindAuditControl("#auditSubjectType", "auditSubjectType");
  bindAuditControl("#auditSubjectId", "auditSubjectId", true);
  bindAuditControl("#auditEventPrefix", "auditEventPrefix", true);
  bindAuditControl("#auditActor", "auditActor", true);
  bindAuditControl("#auditLayer", "auditLayer", true);
  bindAuditControl("#auditLevel", "auditLevel");
  bindAuditControl("#auditAgentId", "auditAgentId");
  bindAuditControl("#auditTaskId", "auditTaskId");
  bindAuditControl("#auditProject", "auditProject");
  bindAuditControl("#auditFleet", "auditFleet");
  bindAuditControl("#auditSince", "auditSince", true);
  bindAuditControl("#auditUntil", "auditUntil", true);
  const clearAudit = document.querySelector<HTMLButtonElement>("#clearAuditFilters");
  if (clearAudit) clearAudit.addEventListener("click", () => {
    state.auditSubjectType = "";
    state.auditSubjectId = "";
    state.auditEventPrefix = "";
    state.auditActor = "";
    state.auditLayer = "";
    state.auditLevel = "";
    state.auditAgentId = "";
    state.auditTaskId = "";
    state.auditProject = "";
    state.auditFleet = "";
    state.auditSince = "";
    state.auditUntil = "";
    updateUrlState();
    render();
  });
}

function bindAuditControl(selector: string, key: keyof Pick<DashboardState, "auditSubjectType" | "auditSubjectId" | "auditEventPrefix" | "auditActor" | "auditLayer" | "auditLevel" | "auditAgentId" | "auditTaskId" | "auditProject" | "auditFleet" | "auditSince" | "auditUntil">, replace = false): void {
  const control = document.querySelector<HTMLInputElement | HTMLSelectElement>(selector);
  if (!control) return;
  control.addEventListener("input", (event) => {
    state[key] = (event.target as HTMLInputElement | HTMLSelectElement).value;
    updateUrlState(replace);
    renderPreservingFocusedControl();
  });
  control.addEventListener("change", (event) => {
    state[key] = (event.target as HTMLInputElement | HTMLSelectElement).value;
    updateUrlState(replace);
    render();
  });
}

// wf-05: change-event router. Right now only the workflow selector
// listens here; other selectors live in their own forms and submit.
function handleContentChange(event: Event): void {
  const target = event.target as HTMLElement | null;
  if (!target) return;
  const planField = target.closest<HTMLElement>("[data-plan-field]");
  if (planField) {
    syncWorkflowPlanDraftFromDom();
    renderPreservingFocusedControl();
    return;
  }
  const selector = target.closest<HTMLSelectElement>(
    "select[data-action='workflowGraphSelect']",
  );
  if (selector) {
    state.selectedWorkflowId = selector.value;
    state.selectedNodeKey = "";
    render();
    return;
  }
  const workflowTaskSelector = target.closest<HTMLSelectElement>(
    "select[data-action='workflowTaskSelect']",
  );
  if (workflowTaskSelector) {
    state.selectedId = workflowTaskSelector.value;
    updateUrlState();
    render();
    return;
  }
  const hermesFleetSelector = target.closest<HTMLSelectElement>("#hermesFleetSelect");
  if (hermesFleetSelector) {
    state.selectedId = hermesFleetSelector.value;
    updateUrlState();
    render();
    return;
  }
  // wf-05 part 3: refresh the draft builder's derived UI (step-key
  // datalist, required-answer inputs) on any input/select change
  // inside a draft form.
  const draftForm = target.closest<HTMLFormElement>("form[data-draft-builder]");
  if (draftForm) {
    draftBuilderRefreshDerived(draftForm);
    return;
  }
}

function handleContentKeydown(event: KeyboardEvent): void {
  if (event.key !== "Enter" && event.key !== " ") return;
  const target = event.target as Element | null;
  const workflowNode = target?.closest<HTMLElement>("[data-action='workflowNodeOpen']");
  const launchpad = target?.closest<HTMLElement>("[data-dashboard-go]");
  if (!workflowNode && !launchpad) return;
  event.preventDefault();
  if (workflowNode) {
    toggleWorkflowNode(workflowNode);
  } else if (launchpad) {
    navigateDashboardView(launchpad.dataset.dashboardGo as ViewKey);
  }
}

async function handleContentClick(event: MouseEvent): Promise<void> {
  const launchpad = (event.target as Element | null)?.closest<HTMLElement>("[data-dashboard-go]");
  if (launchpad) {
    event.preventDefault();
    navigateDashboardView(launchpad.dataset.dashboardGo as ViewKey);
    return;
  }
  const workflowPlanAdd = (event.target as Element | null)?.closest<HTMLElement>("[data-action='workflowPlanNodeAdd']");
  if (workflowPlanAdd) {
    event.preventDefault();
    addWorkflowPlanNode();
    return;
  }
  const workflowPlanDelete = (event.target as Element | null)?.closest<HTMLElement>("[data-action='workflowPlanNodeDelete']");
  if (workflowPlanDelete) {
    event.preventDefault();
    deleteWorkflowPlanNode(workflowPlanDelete);
    return;
  }
  const workflowPlanMove = (event.target as Element | null)?.closest<HTMLElement>("[data-action='workflowPlanNodeMove']");
  if (workflowPlanMove) {
    event.preventDefault();
    moveWorkflowPlanNode(workflowPlanMove);
    return;
  }
  const workflowPlanCancel = (event.target as Element | null)?.closest<HTMLElement>("[data-action='workflowPlanCancel']");
  if (workflowPlanCancel) {
    event.preventDefault();
    cancelWorkflowPlan();
    return;
  }
  const workflowPlanAccept = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-action='workflowPlanAccept']");
  if (workflowPlanAccept) {
    event.preventDefault();
    await acceptWorkflowPlan(workflowPlanAccept);
    return;
  }
  const projectDelete = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-project-delete]");
  if (projectDelete) {
    event.preventDefault();
    const project = projectDelete.dataset.projectDelete || "";
    if (!project) return;
    await runDirectDelete(projectDelete, "Project", `/projects/${encodeURIComponent(project)}?actor=human`, () => {
      if (state.projectFilter === project) state.projectFilter = "all";
      if (state.selectedId === project) state.selectedId = "";
    });
    return;
  }
  const agentDelete = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-agent-delete]");
  if (agentDelete) {
    event.preventDefault();
    const agentId = agentDelete.dataset.agentDelete || "";
    if (!agentId) return;
    await runDirectDelete(agentDelete, "Agent", `/agents/${encodeURIComponent(agentId)}`, () => {
      if (state.selectedId === agentId) state.selectedId = "";
    });
    return;
  }
  const taskDelete = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-task-delete]");
  if (taskDelete) {
    event.preventDefault();
    const taskId = taskDelete.dataset.taskDelete || "";
    if (!taskId) return;
    await runDirectDelete(taskDelete, "Task", `/tasks/${encodeURIComponent(taskId)}?actor=human`, () => {
      if (state.selectedId === taskId) state.selectedId = "";
    });
    return;
  }
  const secretDelete = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-secret-delete]");
  if (secretDelete) {
    event.preventDefault();
    const secretId = secretDelete.dataset.secretDelete || "";
    if (!secretId) return;
    await runDirectDelete(secretDelete, "Secret", `/secrets/${encodeURIComponent(secretId)}`, () => {
      if (state.selectedId === secretId) state.selectedId = "";
    });
    return;
  }
  const projectTarget = (event.target as Element | null)?.closest<HTMLElement>("[data-project-focus]");
  if (projectTarget) {
    state.projectFilter = projectTarget.dataset.projectFocus || "all";
    state.selectedId = "";
    state.agentPage = 1;
    updateUrlState();
    render();
    return;
  }
  const taskOpen = (event.target as Element | null)?.closest<HTMLElement>("[data-task-open]");
  if (taskOpen) {
    const taskId = taskOpen.dataset.taskOpen || "";
    if (!taskId) return;
    state.selectedId = taskId;
    state.activeView = "tasks";
    updateUrlState();
    render();
    return;
  }
  const bucketTarget = (event.target as Element | null)?.closest<HTMLElement>("[data-agent-filter-value]");
  if (bucketTarget) {
    const value = bucketTarget.dataset.agentFilterValue || "";
    if (value && value !== "idle") {
      state.agentQuery = value;
      state.activeView = "agents";
      state.agentPage = 1;
      updateUrlState();
      render();
    }
    return;
  }
  // wf-05: open the inspector when a workflow-graph node is clicked.
  // wf-05 part 3: draft-builder row management.
  const draftAddBtn = (event.target as Element | null)?.closest<HTMLElement>(
    "[data-action='draftRowAdd']",
  );
  if (draftAddBtn) {
    event.preventDefault();
    draftBuilderAddRow(draftAddBtn);
    return;
  }
  const draftRemoveBtn = (event.target as Element | null)?.closest<HTMLElement>(
    "[data-action='draftRowRemove']",
  );
  if (draftRemoveBtn) {
    event.preventDefault();
    draftBuilderRemoveRow(draftRemoveBtn);
    return;
  }
  const nodeOpen = (event.target as Element | null)?.closest<HTMLElement>(
    "[data-action='workflowNodeOpen']",
  );
  if (nodeOpen) {
    toggleWorkflowNode(nodeOpen);
    return;
  }
  const nodeClose = (event.target as Element | null)?.closest<HTMLElement>(
    "[data-action='workflowNodeClose']",
  );
  if (nodeClose) {
    state.selectedNodeKey = "";
    render();
    return;
  }
  const target = (event.target as Element | null)?.closest<HTMLElement>("[data-select-id]");
  if (!target) return;
  const selectedId = target.dataset.selectId || "";
  if (!selectedId) return;
  state.selectedId = state.selectedId === selectedId ? "" : selectedId;
  updateUrlState();
  render();
}

function navigateDashboardView(view: ViewKey | undefined): void {
  if (!view || !VIEW_KEYS.has(view)) return;
  state.activeView = view;
  state.actionMessage = null;
  updateUrlState();
  render();
}

function toggleWorkflowNode(node: HTMLElement): void {
  const key = node.dataset.nodeKey || "";
  state.selectedNodeKey = state.selectedNodeKey === key ? "" : key;
  render();
}

async function runDirectDelete(
  button: HTMLButtonElement,
  label: string,
  path: string,
  onSuccess: () => void = () => {},
): Promise<void> {
  if (!confirmDestructive(label)) return;
  button.disabled = true;
  try {
    const result = await deleteJSON(path);
    state.actionMessage = `${label} delete ok: ${redactedJson(result)}`;
    onSuccess();
    updateUrlState();
    await loadDashboard();
  } catch (error) {
    state.actionMessage = `${label} delete failed: ${error instanceof Error ? error.message : String(error)}`;
    render();
  } finally {
    button.disabled = false;
  }
}

function confirmDestructive(label: string): boolean {
  const title = DESTRUCTIVE_ACTION_LABELS[label] || `${label} delete?`;
  return window.confirm(`${title}\n\nThis cannot be undone.`);
}

function syncDashboardSubscription(): void {
  if (state.data && isConnectionLive()) {
    startDashboardStream();
  } else {
    stopDashboardStream();
  }
}

function startDashboardStream(): void {
  if (state.dashboardStream) return;
  const controller = new AbortController();
  state.dashboardStream = controller;
  state.dashboardStreamStatus = "connecting";
  api.stream("/dashboard/stream?timeout_seconds=60&poll_interval_seconds=1", {
    headers: { Accept: "application/x-ndjson" },
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      state.dashboardStreamStatus = "connected";
      renderSyncState();
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        let changed = false;
        for (const line of lines) {
          const text = line.trim();
          if (!text) continue;
          const event = JSON.parse(text) as DashboardStreamEvent;
          applyServerTime(event.server_time || event.updated_at || "");
          state.connection = { ...state.connection, connected: true };
          if (event.event === "updated") {
            await refreshDashboardFromStream();
          } else {
            changed = true;
          }
        }
        if (changed) renderSyncState();
      }
    })
    .catch((error) => {
      if (!controller.signal.aborted) {
        state.dashboardStreamStatus = "error";
        state.actionMessage = `Dashboard stream failed: ${error instanceof Error ? error.message : String(error)}`;
        render();
      }
    })
    .finally(() => {
      if (state.dashboardStream === controller) state.dashboardStream = null;
      if (!controller.signal.aborted && isConnectionLive()) {
        state.dashboardStreamStatus = "reconnecting";
        window.setTimeout(startDashboardStream, 1000);
      }
    });
}

async function refreshDashboardFromStream(): Promise<void> {
  if (state.loading) return;
  try {
    applyDashboardData((await requestJSON("/dashboard/state")) as DashboardData);
    state.connection = { ...state.connection, connected: true };
    state.error = null;
    renderPreservingFocusedControl();
  } catch (error) {
    state.dashboardStreamStatus = "error";
    state.actionMessage = `Dashboard refresh failed: ${error instanceof Error ? error.message : String(error)}`;
    render();
  }
}

function stopDashboardStream(): void {
  if (!state.dashboardStream) return;
  state.dashboardStream.abort();
  state.dashboardStream = null;
  state.dashboardStreamStatus = "idle";
}

function syncObservabilitySubscription(): void {
  if (state.activeView === "observability" && state.data) {
    startObservabilityStream();
  } else {
    stopObservabilityStream();
  }
}

function startObservabilityStream(): void {
  if (state.observabilityStream) return;
  const controller = new AbortController();
  state.observabilityStream = controller;
  state.observabilityStreamStatus = "connecting";
  const latest = uniqueObservations([
    ...state.observabilityLive,
    ...((state.data?.observability.latest || []) as ObservabilityEvent[]),
  ]);
  const after = latest.length ? latest[0].sequence : 0;
  const headers: Record<string, string> = { Accept: "application/x-ndjson" };
  api.stream(`/observability/stream?after_sequence=${encodeURIComponent(after)}&timeout_seconds=60&poll_interval_seconds=0.5`, {
    headers,
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      state.observabilityStreamStatus = "connected";
      renderSyncState();
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          const text = line.trim();
          if (!text) continue;
          state.observabilityLive = uniqueObservations([
            JSON.parse(text) as ObservabilityEvent,
            ...state.observabilityLive,
          ]).slice(0, 120);
        }
        if (state.activeView === "observability") render();
      }
    })
    .catch((error) => {
      if (!controller.signal.aborted) {
        state.observabilityStreamStatus = "error";
        state.actionMessage = `Observability stream failed: ${error instanceof Error ? error.message : String(error)}`;
        if (state.activeView === "observability") render();
      }
    })
    .finally(() => {
      if (state.observabilityStream === controller) state.observabilityStream = null;
      if (!controller.signal.aborted && state.activeView === "observability") {
        state.observabilityStreamStatus = "reconnecting";
        window.setTimeout(startObservabilityStream, 1000);
      }
    });
}

function stopObservabilityStream(): void {
  if (!state.observabilityStream) return;
  state.observabilityStream.abort();
  state.observabilityStream = null;
  state.observabilityStreamStatus = "idle";
}

async function handleActionSubmit(event: SubmitEvent): Promise<void> {
  const form = (event.target as Element | null)?.closest<HTMLFormElement>("form[data-action]");
  if (!form) return;
  event.preventDefault();
  const action = form.dataset.action || "";
  const values = formValues(form);
  setFormBusy(form, true);
  try {
    const result = await runAction(action, form, values);
    if (action === "workflowPlanPreview") {
      state.workflowPlanDraft = normalizeWorkflowPlanDraft(result);
      state.actionMessage = `Plan generated: ${state.workflowPlanDraft.nodes.length} proposed tasks`;
      render();
      return;
    }
    state.actionMessage = actionSuccessMessage(action, result);
    await loadDashboard();
  } catch (error) {
    state.actionMessage = `${labelize(action)} failed: ${error instanceof Error ? error.message : String(error)}`;
    render();
  } finally {
    if (form.isConnected) setFormBusy(form, false);
  }
}

function setFormBusy(form: HTMLFormElement, busy: boolean): void {
  form.setAttribute("aria-busy", busy ? "true" : "false");
  form.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
    "button, input, select, textarea",
  ).forEach((control) => {
    if (busy) {
      if (!control.disabled) {
        control.dataset.busyDisabled = "1";
        control.disabled = true;
      }
    } else if (control.dataset.busyDisabled === "1") {
      control.disabled = false;
      delete control.dataset.busyDisabled;
    }
  });
}

async function runAction(action: string, form: HTMLFormElement, values: JsonObject): Promise<unknown> {
  if (action === "dispatchTick") {
    return postJSON("/dispatch/tick", {
      lease_seconds: numberValue(values.lease_seconds, 900),
      limit: numberValue(values.limit, 100),
      stale_after_seconds: optionalNumber(values.stale_after_seconds),
    });
  }
  if (action === "agentCreate") {
    return postJSON("/agents", {
      machine_id: requiredString(values.machine_id),
      name: requiredString(values.name),
      agent_id: emptyToNull(values.agent_id),
      hermes_instance_id: emptyToNull(values.hermes_instance_id),
      fleet_id: emptyToNull(values.fleet_id),
      capabilities: csvList(values.capabilities),
      resources: parseJsonObject(values.resources),
      actor: String(values.actor || "human"),
    });
  }
  if (action === "agentUpdate") {
    return putJSON(`/agents/${encodeURIComponent(requiredDataset(form, "agentId"))}`, {
      name: requiredString(values.name),
      status: requiredString(values.status),
      health_status: requiredString(values.health_status),
      hermes_instance_id: emptyToNull(values.hermes_instance_id),
      capabilities: csvList(values.capabilities),
      resources: parseJsonObject(values.resources),
    });
  }
  if (action === "projectCreate") {
    return postJSON("/projects", {
      name: requiredString(values.name),
      description: String(values.description || ""),
      status: requiredString(values.status),
      metadata: parseJsonObject(values.metadata),
      actor: "human",
    });
  }
  if (action === "projectUpdate") {
    return putJSON(`/projects/${encodeURIComponent(requiredDataset(form, "project"))}`, {
      name: requiredString(values.name),
      description: String(values.description || ""),
      status: requiredString(values.status),
      metadata: parseJsonObject(values.metadata),
      actor: "human",
    });
  }
  if (action === "taskCreate") {
    return postJSON("/tasks", {
      title: requiredString(values.title),
      description: String(values.description || ""),
      project: emptyToNull(values.project),
      priority: numberValue(values.priority, 0),
      required_capabilities: csvList(values.required_capabilities),
      dependencies: csvList(values.dependencies),
      metadata: parseJsonObject(values.metadata),
      actor: "human",
    });
  }
  if (action === "workflowPlanPreview") {
    return postJSON("/dashboard/workflow-plan/preview", {
      goal: requiredString(values.goal),
      project: emptyToNull(values.project),
      prompt: String(values.prompt || ""),
      required_capabilities: csvList(values.required_capabilities),
      max_tasks: numberValue(values.max_tasks, 6),
      model: String(values.model || "*"),
      context: {
        active_view: state.activeView,
        project_filter: state.projectFilter,
      },
    });
  }
  if (action === "workflowPlanningTaskCreate") {
    const goal = requiredString(values.goal);
    const notes = String(values.description || "").trim();
    const metadata = parseJsonObject(values.metadata);
    if (!metadata.origin) metadata.origin = { type: "dashboard_workflow_planning" };
    if (!metadata.workflow) {
      metadata.workflow = {
        type: "task_chain",
        role: "planning",
        status: "planning",
      };
    }
    const title = String(values.title || "").trim() || `Plan workflow: ${truncate(goal, 72)}`;
    const description = [
      `Workflow goal:\n${goal}`,
      notes ? `Planning instructions:\n${notes}` : "",
      "Create child tasks for each executable step and leave this task blocked on the resulting chain.",
    ].filter(Boolean).join("\n\n");
    return postJSON("/tasks", {
      title,
      description,
      project: emptyToNull(values.project),
      priority: numberValue(values.priority, 0),
      required_capabilities: csvList(values.required_capabilities),
      dependencies: [],
      metadata,
      actor: "human",
    });
  }
  if (action === "workflowChainTaskAdd") {
    const parentTaskId = requiredDataset(form, "taskId");
    const metadata = parseJsonObject(values.metadata);
    if (!metadata.origin) metadata.origin = { type: "dashboard_workflow_chain", parent_task_id: parentTaskId };
    if (!metadata.workflow) {
      metadata.workflow = {
        type: "task_chain",
        role: "step",
        parent_task_id: parentTaskId,
      };
    }
    return postJSON(`/tasks/${encodeURIComponent(parentTaskId)}/children`, {
      actor: "human",
      children: [{
        title: requiredString(values.title),
        description: String(values.description || ""),
        project: emptyToNull(values.project),
        priority: numberValue(values.priority, 0),
        required_capabilities: csvList(values.required_capabilities),
        dependencies: [],
        metadata,
      }],
    });
  }
  if (action === "taskUpdate") {
    return putJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}`, {
      title: requiredString(values.title),
      description: String(values.description || ""),
      project: emptyToNull(values.project),
      priority: numberValue(values.priority, 0),
      required_capabilities: csvList(values.required_capabilities),
      dependencies: csvList(values.dependencies),
      metadata: parseJsonObject(values.metadata),
      actor: "human",
    });
  }
  if (action === "taskClaim") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/claim?agent_id=${encodeURIComponent(requiredString(values.agent_id))}&lease_seconds=${numberValue(values.lease_seconds, 900)}`, {});
  }
  if (action === "taskAddChild") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/children`, {
      actor: requiredString(values.actor),
      children: [{
        title: requiredString(values.title),
        description: String(values.description || ""),
        project: emptyToNull(values.project),
        required_capabilities: csvList(values.required_capabilities),
        dependencies: csvList(values.dependencies),
        metadata: {},
      }],
    });
  }
  if (action === "taskStart") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/start?agent_id=${encodeURIComponent(requiredString(values.agent_id))}`, {});
  }
  if (action === "taskSubmitReview") {
    const taskId = requiredDataset(form, "taskId");
    return postJSON(`/tasks/${encodeURIComponent(taskId)}/submit-for-review?agent_id=${encodeURIComponent(requiredString(values.agent_id))}`, {});
  }
  if (action === "taskTransition") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/transition`, {
      target_state: requiredString(values.target_state),
      actor: requiredString(values.actor),
      detail: parseJsonObject(values.detail),
    });
  }
  if (action === "addEvidence") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/evidence`, {
      kind: requiredString(values.kind),
      uri: requiredString(values.uri),
      summary: requiredString(values.summary),
      created_by: requiredString(values.created_by),
      checksum: emptyToNull(values.checksum),
      metadata: {},
    });
  }
  if (action === "requestReview") {
    return postJSON(`/tasks/${encodeURIComponent(requiredDataset(form, "taskId"))}/reviews`, {
      reviewer_agent_id: requiredString(values.reviewer_agent_id),
      actor: requiredString(values.actor),
    });
  }
  if (action === "reviewDecision") {
    return postJSON(`/reviews/${encodeURIComponent(requiredDataset(form, "reviewId"))}/decision`, {
      status: requiredString(values.status),
      reviewer_agent_id: requiredString(values.reviewer_agent_id),
      reason: emptyToNull(values.reason),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "publishTask") {
    return postJSON("/publications", {
      task_id: requiredString(values.task_id),
      target: requiredString(values.target),
      created_by: requiredString(values.created_by),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "hermesRuntimeUpdate") {
    return putJSON(`/dashboard/hermes/fleets/${encodeURIComponent(requiredDataset(form, "fleetId"))}/config-surface`, {
      runtime: {
        gateway_model: String(values.gateway_model || ""),
        gateway_provider: String(values.gateway_provider || ""),
        gateway_base_url: String(values.gateway_base_url || ""),
        slack_home_channel_name: String(values.slack_home_channel_name || ""),
      },
      apply_local: boolValue(values.apply_local) === "true",
      actor: "human",
    });
  }
  if (action === "hermesConfigSet") {
    const key = requiredString(values.config_key);
    const body: JsonObject = { actor: "human" };
    if (boolValue(values.remove) === "true") {
      body.remove_config = [key];
    } else {
      body.config = { [key]: parseJsonValue(values.value_json) };
    }
    return putJSON(`/dashboard/hermes/fleets/${encodeURIComponent(requiredDataset(form, "fleetId"))}/config-surface`, body);
  }
  if (action === "hermesEnvSet") {
    const key = requiredString(values.env_key);
    const body: JsonObject = { actor: "human" };
    if (boolValue(values.remove) === "true") {
      body.remove_env = [key];
    } else {
      body.env = { [key]: requiredString(values.value) };
    }
    return putJSON(`/dashboard/hermes/fleets/${encodeURIComponent(requiredDataset(form, "fleetId"))}/config-surface`, body);
  }
  if (action === "hermesPluginsUpdate") {
    return putJSON(`/dashboard/hermes/fleets/${encodeURIComponent(requiredDataset(form, "fleetId"))}/config-surface`, {
      plugins: {
        enabled: csvList(values.enabled),
        disabled: csvList(values.disabled),
      },
      actor: "human",
    });
  }
  if (action === "hermesSkillsUpdate") {
    return putJSON(`/dashboard/hermes/fleets/${encodeURIComponent(requiredDataset(form, "fleetId"))}/config-surface`, {
      skills: {
        disabled: csvList(values.disabled),
      },
      actor: "human",
    });
  }
  if (action === "agentBulkUpdate") {
    const body: JsonObject = {
      agent_ids: String(values.agent_ids || "").split(",").map((item) => item.trim()).filter(Boolean),
    };
    if (String(values.status || "").trim()) body.status = String(values.status).trim();
    if (String(values.health_status || "").trim()) body.health_status = String(values.health_status).trim();
    if (String(values.capabilities || "").trim()) {
      body.capabilities = String(values.capabilities).split(",").map((item) => item.trim()).filter(Boolean);
    }
    return postJSON("/agents/bulk", body);
  }
  if (action === "roleSeed") {
    return postJSON("/roles/seed", {
      replace: boolValue(values.replace) === "true",
    });
  }
  if (action === "runtimeCreate") {
    return postJSON("/runtimes", {
      name: requiredString(values.name),
      manifest: parseJsonObject(values.manifest),
      created_by: requiredString(values.created_by),
    });
  }
  if (action === "runtimeRunCreate") {
    return postJSON("/runtime-runs", {
      task_id: requiredString(values.task_id),
      agent_id: requiredString(values.agent_id),
      environment_id: requiredString(values.environment_id),
    });
  }
  if (action === "runtimeRunComplete") {
    const runId = requiredString(values.run_id);
    return postJSON(`/runtime-runs/${encodeURIComponent(runId)}/complete`, {
      evidence_id: requiredString(values.evidence_id),
      status: requiredString(values.status),
    });
  }
  if (action === "runtimeDeltaPropose") {
    return postJSON("/runtime-deltas", {
      task_id: requiredString(values.task_id),
      agent_id: requiredString(values.agent_id),
      package_manager: requiredString(values.package_manager),
      commands: parseJsonStringArray(values.commands),
      added_dependencies: parseJsonAnyArray(values.added_dependencies),
      reason: requiredString(values.reason),
      project: emptyToNull(values.project),
      base_runtime_id: emptyToNull(values.base_runtime_id),
      lockfile_path: emptyToNull(values.lockfile_path),
      lockfile_digest: emptyToNull(values.lockfile_digest),
      evidence_id: emptyToNull(values.evidence_id),
    });
  }
  if (action === "runtimeDeltaValidate") {
    return postJSON(`/runtime-deltas/${encodeURIComponent(requiredDataset(form, "deltaId"))}/validate`, {
      actor: requiredString(values.actor || "operator"),
    });
  }
  if (action === "runtimeDeltaReject") {
    return postJSON(`/runtime-deltas/${encodeURIComponent(requiredDataset(form, "deltaId"))}/reject`, {
      actor: requiredString(values.actor || "operator"),
      reason: requiredString(values.reason),
    });
  }
  if (action === "runtimeDeltaPromote") {
    return postJSON(`/runtime-deltas/${encodeURIComponent(requiredDataset(form, "deltaId"))}/promote`, {
      actor: requiredString(values.actor || "operator"),
      runtime_name: emptyToNull(values.runtime_name),
    });
  }
  if (action === "rolloutCreate") {
    return postJSON("/rollouts", {
      version: requiredString(values.version),
      strategy: requiredString(values.strategy),
      target_percent: numberValue(values.target_percent, 0),
      created_by: requiredString(values.created_by),
      tenant_id: emptyToNull(values.tenant_id),
      channel: requiredString(values.channel),
      runtime_environment_id: emptyToNull(values.runtime_environment_id),
      artifact_uri: emptyToNull(values.artifact_uri),
      artifact_hash: emptyToNull(values.artifact_hash),
      health_policy: parseJsonObject(values.health_policy),
      required_eval_set_id: emptyToNull(values.required_eval_set_id),
    });
  }
  if (action === "rolloutAdvance") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/advance`, {
      action: requiredString(values.action),
      actor: requiredString(values.actor),
      detail: parseJsonObject(values.detail),
    });
  }
  if (action === "rolloutVerifyArtifact") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/artifact`, {
      artifact_uri: requiredString(values.artifact_uri),
      artifact_hash: requiredString(values.artifact_hash),
      actor: requiredString(values.actor),
    });
  }
  if (action === "rolloutHealth") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/health`, {
      actor: requiredString(values.actor),
      checks: parseJsonObject(values.checks),
    });
  }
  if (action === "rolloutRescue") {
    return postJSON(`/rollouts/${encodeURIComponent(requiredDataset(form, "rolloutId"))}/rescue`, {
      actor: requiredString(values.actor),
      reason: requiredString(values.reason),
      detail: {},
    });
  }
  if (action === "secretCreate") {
    return postJSON("/secrets", {
      name: requiredString(values.name),
      value: requiredString(values.value),
      scopes: parseJsonObject(values.scopes),
      created_by: requiredString(values.created_by),
    });
  }
  if (action === "secretAccess") {
    return postJSON(`/secrets/${encodeURIComponent(requiredDataset(form, "secretId"))}/access`, {
      accessor_agent_id: requiredString(values.accessor_agent_id),
      purpose: requiredString(values.purpose),
      ttl_seconds: numberValue(values.ttl_seconds, 300),
    });
  }
  if (action === "workflowDraftCreate") {
    // wf-05 part 3: if the row editors are populated, prefer them
    // over the hidden raw-JSON textareas. The textareas remain as an
    // escape hatch (and the values dict already carries them too).
    const stepsFromRows = draftBuilderCollectSteps(form);
    const questionsFromRows = draftBuilderCollectQuestions(form);
    const answersFromRows = draftBuilderCollectAnswers(form);
    const proposedSteps =
      stepsFromRows.length > 0
        ? stepsFromRows
        : parseJsonArray(values.proposed_steps);
    const questions =
      questionsFromRows.length > 0
        ? questionsFromRows
        : parseJsonArray(values.questions);
    const fromRawAnswers = parseJsonObject(values.answers);
    const answers = { ...fromRawAnswers, ...answersFromRows };
    return postJSON("/workflows/drafts", {
      goal: requiredString(values.goal),
      proposed_steps: proposedSteps,
      questions,
      answers,
    });
  }
  if (action === "workflowDraftPreview") {
    return postJSON(`/workflows/drafts/${encodeURIComponent(requiredDataset(form, "draftId"))}/preview`, {
      input: parseJsonObject(values.input),
    });
  }
  if (action === "workflowDraftApprove") {
    return postJSON(`/workflows/drafts/${encodeURIComponent(requiredDataset(form, "draftId"))}/approve`, {
      slug: requiredString(values.slug),
      name: requiredString(values.name),
    });
  }
  if (action === "workflowPreview") {
    return postJSON(`/workflows/${encodeURIComponent(requiredDataset(form, "workflowId"))}/preview`, {
      input: parseJsonObject(values.input),
    });
  }
  if (action === "workflowStart") {
    return postJSON(`/workflows/${encodeURIComponent(requiredDataset(form, "workflowId"))}/start`, {
      started_by: requiredString(values.started_by),
      input: parseJsonObject(values.input),
    });
  }
  if (action === "notifierConfigure") {
    return postJSON("/notifier/channels", {
      name: requiredString(values.name),
      channel_type: requiredString(values.channel_type),
      event_types: String(values.event_types || "").split(",").map((item) => item.trim()).filter(Boolean),
      target: parseJsonObject(values.target),
      enabled: true,
    });
  }
  if (action === "notifierDeliver") {
    return postJSON("/notifier/deliver", {
      limit: numberValue(values.limit, 50),
    });
  }
  throw new Error(`unsupported action: ${action}`);
}

function postJSON(path: string, body: JsonObject): Promise<unknown> {
  return requestJSON(path, { method: "POST", body: JSON.stringify(body) });
}

function putJSON(path: string, body: JsonObject): Promise<unknown> {
  return requestJSON(path, { method: "PUT", body: JSON.stringify(body) });
}

function deleteJSON(path: string): Promise<unknown> {
  return requestJSON(path, { method: "DELETE" });
}

function formValues(form: HTMLFormElement): JsonObject {
  const values: JsonObject = {};
  new FormData(form).forEach((value, key) => {
    values[key] = String(value);
  });
  return values;
}

function relationshipGraph(data: DashboardData): string {
  const fleets = data.fleets.slice(0, 6);
  const machines = data.machines.slice(0, 8);
  const agents = data.agents.slice(0, 12).map((item) => item.agent);
  const activeTasks = data.tasks
    .filter((detail) => !TERMINAL_TASK_STATES.has(detail.task.state))
    .slice(0, 14)
    .map((detail) => detail.task);
  const nodes = [
    ...fleets.map((fleet, index) => graphNode(fleet.id, fleet.name, "fleet", 70, 58 + index * 58)),
    ...machines.map((machine, index) => graphNode(machine.id, machine.hostname, "machine", 235, 58 + index * 58)),
    ...agents.map((agent, index) => graphNode(agent.id, agent.name, "agent", 430, 46 + index * 48)),
    ...activeTasks.map((task, index) => graphNode(task.id, task.title, "task", 650, 44 + index * 46)),
  ];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges: Array<{ from: string; to: string; tone: string }> = [];
  for (const fleet of fleets) {
    for (const agentId of fleet.agent_ids || []) {
      edges.push({ from: fleet.id, to: agentId, tone: "fleet-agent" });
    }
  }
  for (const agent of agents) {
    edges.push({ from: agent.machine_id, to: agent.id, tone: "machine-agent" });
  }
  for (const task of activeTasks) {
    if (task.owner_agent_id) edges.push({ from: task.owner_agent_id, to: task.id, tone: "agent-task" });
    for (const dependency of task.dependencies || []) {
      edges.push({ from: dependency, to: task.id, tone: "dependency" });
    }
  }
  const height = Math.max(360, 90 + Math.max(fleets.length * 58, machines.length * 58, agents.length * 48, activeTasks.length * 46));
  const edgeSvg = edges.map((edge) => {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) return "";
    return `<path class="graph-edge graph-edge-${edge.tone}" d="M${from.x + 90},${from.y} C${from.x + 170},${from.y} ${to.x - 170},${to.y} ${to.x - 90},${to.y}"></path>`;
  }).join("");
  const nodeSvg = nodes.map((node) => `
    <g class="graph-node graph-node-${node.kind} ${selectedClass(node.id)}" data-select-id="${escapeHtml(node.id)}" transform="translate(${node.x},${node.y})">
      <rect x="-86" y="-18" width="172" height="36" rx="8"></rect>
      <text text-anchor="middle" y="4">${escapeHtml(truncate(node.label, 22))}</text>
    </g>
  `).join("");
  return `
    <div class="graph-wrap">
      <svg class="relationship-graph" viewBox="0 0 760 ${height}" role="img" aria-label="Fleet topology graph">
        <text class="graph-column-label" x="70" y="24">Fleets</text>
        <text class="graph-column-label" x="235" y="24">Machines</text>
        <text class="graph-column-label" x="430" y="24">Agents</text>
        <text class="graph-column-label" x="650" y="24">Tasks</text>
        ${edgeSvg}
        ${nodeSvg}
      </svg>
    </div>
    <div class="mobile-card-list graph-mobile-list">
      ${nodes.map((node) => `
        <button class="mobile-object-card compact ${selectedClass(node.id)}" type="button" data-select-id="${escapeHtml(node.id)}">
          <span><strong>${escapeHtml(node.label)}</strong></span>
          <span class="muted small">${escapeHtml(labelize(node.kind))} / ${escapeHtml(node.id)}</span>
        </button>
      `).join("")}
    </div>
  `;
}

function graphNode(id: string, label: string, kind: string, x: number, y: number): { id: string; label: string; kind: string; x: number; y: number } {
  return { id, label, kind, x, y };
}

function topologySelectionDetail(data: DashboardData): string {
  const id = state.selectedId;
  if (!id) return `<div class="empty-state">Select a topology node</div>`;
  const fleet = data.fleets.find((item) => item.id === id || item.name === id);
  if (fleet) return fleetDetail(fleet, data);
  const machine = data.machines.find((item) => item.id === id || item.hostname === id);
  if (machine) return machineSelectionDetail(machine, data);
  const agent = data.agents.find((item) => item.agent.id === id || item.agent.name === id);
  if (agent) return agentSelectionDetail(agent, data);
  const task = data.tasks.find((item) => item.task.id === id);
  if (task) return taskSelectionDetail(task, data);
  return `<div class="empty-state">No dashboard record found for ${escapeHtml(id)}</div>`;
}

function machineSelectionDetail(machine: MachineRecord, data: DashboardData): string {
  const agents = data.agents.filter((item) => item.agent.machine_id === machine.id);
  return `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(machine.hostname)}</h3><p class="muted small mono">${escapeHtml(machine.id)}</p></div>
        ${chip(machine.trusted ? "trusted" : "untrusted", machine.trusted ? "good" : "bad")}
      </div>
      <div class="row-grid">
        ${field("Agents", String(agents.length))}
        ${field("Labels", jsonSummary(machine.labels))}
        ${field("Resources", jsonSummary(machine.resources))}
      </div>
      <div class="agent-list">
        ${agents.length ? agents.map((item) => agentPill(item, data)).join("") : `<div class="empty-state">No agents on this machine</div>`}
      </div>
    </article>
  `;
}

function agentSelectionDetail(item: AgentItem, data: DashboardData): string {
  const agent = item.agent;
  return `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(agent.name)}</h3><p class="muted small mono">${escapeHtml(agent.id)}</p></div>
        <div class="chip-row">${chip(agent.status, statusTone(agent.status))}${chip(agent.health_status, healthTone(agent.health_status))}</div>
      </div>
      <div class="row-grid">
        ${field("Type", "agent record")}
        ${field("Fleet", agentFleetLabel(data, agent.id))}
        ${field("Role", roleLabel(data, agent.role_id))}
        ${field("Machine", item.machine?.hostname || "missing")}
        ${field("Capacity", `${item.active_lease_count} / ${item.capacity}`)}
        ${field("Last seen", formatAge(agent.last_seen_at))}
        ${field("Hermes", agent.hermes_instance_id || "unbound")}
        ${field("Projects", (item.active_projects || []).join(", ") || "idle")}
        ${field("Capabilities", (agent.capabilities || []).join(", ") || "none")}
        ${field("Resources", jsonSummary(agent.resources))}
      </div>
      <div class="story-stack">
        ${item.active_tasks.length ? item.active_tasks.map((task) => storyButton(task)).join("") : `<div class="empty-state">No active task</div>`}
      </div>
    </article>
  `;
}

function taskSelectionDetail(detail: TaskDetail, data: DashboardData): string {
  const task = detail.task;
  const owner = task.owner_agent_id ? data.agents.find((item) => item.agent.id === task.owner_agent_id) : null;
  return `
    <article class="record compact">
      <div class="record-header">
        <div><h3>${escapeHtml(task.title)}</h3><p class="muted small mono">${escapeHtml(task.id)}</p></div>
        ${chip(task.state, statusTone(task.state))}
      </div>
      <div class="row-grid">
        ${field("Project", taskProject(task))}
        ${field("Priority", `P${task.priority || 0}`)}
        ${field("Owner", owner?.agent.name || task.owner_agent_id || "unassigned")}
        ${field("Dependencies", String((task.dependencies || []).length))}
        ${field("Required", (task.required_capabilities || []).join(", ") || "none")}
      </div>
    </article>
  `;
}

function metric(label: string, value: unknown, note: string): string {
  return `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><p class="metric-note">${escapeHtml(note)}</p></div>`;
}

function stateBars(states: string[], counts: Record<string, number>, total: number, emptyLabel = "No tasks"): string {
  if (!total) return `<div class="empty-state">${escapeHtml(emptyLabel)}</div>`;
  return `<div class="state-bar">${states.map((name) => {
    const count = counts[name] || 0;
    const width = Math.max(2, Math.round((count / total) * 100));
    return `<div class="state-row"><span>${escapeHtml(labelize(name))}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="mono small">${count}</span></div>`;
  }).join("")}</div>`;
}

function observationMetric(item: ObservabilityEvent): string {
  return `
    <article class="metric-observation">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <p class="muted small">${escapeHtml(item.layer)} / ${escapeHtml(item.source)} · ${escapeHtml(formatAge(item.created_at))}</p>
      </div>
      <div class="metric-observation-value">${escapeHtml(formatMetricValue(item))}</div>
    </article>
  `;
}

function observationRecord(item: ObservabilityEvent): string {
  const subject = item.subject_type && item.subject_id ? `${item.subject_type}:${item.subject_id}` : "";
  return `
    <article class="observation-row tone-left-${observationTone(item.level)}">
      <div class="observation-main">
        <span class="mono small">#${escapeHtml(item.sequence)}</span>
        ${chip(item.kind, item.kind === "metric" ? "info" : observationTone(item.level))}
        ${chip(item.level, observationTone(item.level))}
        <strong>${escapeHtml(item.name)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.layer)} / ${escapeHtml(item.source)} ${subject ? `· ${escapeHtml(subject)}` : ""} · ${escapeHtml(formatAge(item.created_at))}</div>
      <div class="observation-detail">${escapeHtml(item.kind === "metric" ? formatMetricValue(item) : jsonSummary(item.detail))}</div>
    </article>
  `;
}

function llmRouteRecord(item: ObservabilityEvent): string {
  const detail = item.detail || {};
  const provider = String(detail.provider || "unknown provider");
  const resolvedModel = String(detail.response_model || detail.resolved_model || detail.requested_model || "unknown model");
  const requestedModel = String(detail.requested_model || "");
  const status = Number(detail.status_code || 0);
  const duration = Number(detail.duration_ms || 0);
  const agent = String(detail.agent_id || item.subject_id || item.source || "unknown agent");
  const task = String(detail.task_id || "");
  const usage = detail.usage as JsonObject | undefined;
  const totalTokens = usage && typeof usage.total_tokens === "number" ? `${usage.total_tokens} tokens` : "";
  const attempts = Array.isArray(detail.attempts) ? `${detail.attempts.length} attempt(s)` : "";
  const tone = status >= 500 ? "bad" : status >= 400 ? "warn" : "good";
  const modelNote = requestedModel && requestedModel !== resolvedModel ? `requested ${requestedModel}` : String(detail.outcome || "");
  return `
    <article class="observation-row tone-left-${tone}">
      <div class="observation-main">
        ${chip(provider, tone)}
        <strong>${escapeHtml(resolvedModel)}</strong>
        ${status ? chip(String(status), tone) : ""}
      </div>
      <div class="muted small">${escapeHtml(agent)}${task ? ` · task:${escapeHtml(task)}` : ""} · ${escapeHtml(formatAge(item.created_at))}${duration ? ` · ${escapeHtml(Math.round(duration))}ms` : ""}</div>
      <div class="observation-detail">${escapeHtml([modelNote, totalTokens, attempts].filter(Boolean).join(" · ") || jsonSummary(detail))}</div>
    </article>
  `;
}

function auditEventRecord(item: AuditEvent): string {
  const tone = item.event_type.includes("deleted") || item.event_type.includes("failed")
    ? "warn"
    : item.event_type.includes("error")
      ? "bad"
      : "info";
  return `
    <article class="observation-row tone-left-${tone}">
      <div class="observation-main">
        ${chip(item.subject_type, "info")}
        <strong>${escapeHtml(item.event_type)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.subject_id)} · ${escapeHtml(item.actor)} · ${escapeHtml(formatAge(item.created_at))}</div>
      <div class="observation-detail">${escapeHtml(jsonSummary(item.detail))}</div>
    </article>
  `;
}

function commandAuditRecord(item: CommandAuditRecord): string {
  const tone = item.phase === "completed" || item.phase === "started" ? "info" : "bad";
  const subject = item.task_id ? `task:${item.task_id}` : `agent:${item.agent_id}`;
  const argv = (item.argv || []).join(" ");
  const result = item.returncode === null || item.returncode === undefined ? "" : ` rc=${item.returncode}`;
  const duration = item.duration_ms === null || item.duration_ms === undefined ? "" : ` ${Math.round(item.duration_ms)}ms`;
  return `
    <article class="observation-row tone-left-${tone}">
      <div class="observation-main">
        ${chip(item.phase, tone)}
        <strong>${escapeHtml(item.command_id)}</strong>
      </div>
      <div class="muted small">${escapeHtml(item.agent_id)} · ${escapeHtml(subject)} · ${escapeHtml(formatAge(item.created_at))}${escapeHtml(result)}${escapeHtml(duration)}</div>
      <div class="observation-detail mono">${escapeHtml(argv)}</div>
      <div class="muted small">${escapeHtml(item.cwd)}</div>
    </article>
  `;
}

function canWrite(data: DashboardData): boolean {
  return !!data.session?.can_write || !!data.session?.is_admin;
}

function sessionAccessBadge(data: DashboardData): string {
  const session = data.session;
  if (!session) return `<div class="chip-row">${chip("access unknown", "warn")}</div>`;
  const scopes = session.scopes.length ? session.scopes.join(",") : "open-dev";
  return `<div class="chip-row">${chip(session.mode || "unknown", session.can_write ? "good" : "warn")}${chip(`scopes ${scopes}`, "info")}</div>`;
}

function disabledAttr(disabled: boolean): string {
  return disabled ? "disabled" : "";
}

function uniqueObservations(items: ObservabilityEvent[]): ObservabilityEvent[] {
  const seen = new Set<number>();
  const unique: ObservabilityEvent[] = [];
  for (const item of items.sort((a, b) => Number(b.sequence || 0) - Number(a.sequence || 0))) {
    if (seen.has(item.sequence)) continue;
    seen.add(item.sequence);
    unique.push(item);
  }
  return unique;
}

function attentionList(data: DashboardData): string {
  const items = [
    ...data.agents.filter((item) => !item.availability.eligible).map((item) => `${item.agent.name}: ${item.availability.reasons.join(", ")}`),
    ...data.dead_letters.map((task) => `Dead letter: ${task.title}`),
    ...data.rollouts.filter((item) => ["rescuing", "failed"].includes(String(item.rollout.status))).map((item) => `Rollout ${item.rollout.version}: ${item.rollout.status}`),
    ...data.dispatch.tasks.filter((item) => item.eligible_agent_count === 0).map((item) => `No eligible agent: ${item.task.title}`),
  ];
  return items.length
    ? `<div class="record-list">${items.slice(0, 8).map((item) => `<div class="record compact">${escapeHtml(item)}</div>`).join("")}</div>`
    : `<div class="empty-state">No attention items</div>`;
}

function field(label: string, value: unknown): string {
  return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(value == null || value === "" ? "none" : value)}</span></div>`;
}

function chip(value: unknown, tone: Tone = "info"): string {
  return `<span class="chip tone-${tone}">${escapeHtml(labelize(value))}</span>`;
}

function timelineItem(eventType: string, actor: string, createdAt: string): string {
  return `<div class="timeline-item"><span class="mono small">${escapeHtml(labelize(eventType))}</span><br><span class="muted small">${escapeHtml(actor)} ${escapeHtml(formatAge(createdAt))}</span></div>`;
}

function agentSelect(name: string, agents: AgentItem[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">Select agent</option>${agents.map((item) => option(item.agent.id, item.agent.name, selected)).join("")}</select>`;
}

function taskSelect(name: string, tasks: TaskDetail[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">Select task</option>${tasks.map((detail) => option(detail.task.id, detail.task.title, selected)).join("")}</select>`;
}

function runtimeSelect(name: string, runtimes: ApiRecord[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">Select runtime</option>${runtimes.map((runtime) => option(String(runtime.id), String(runtime.name || runtime.id), selected)).join("")}</select>`;
}

function runtimeRunSelect(name: string, runs: ApiRecord[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">Select run</option>${runs.map((run) => option(String(run.id), `${String(run.status || "run")} / ${String(run.task_id || run.id)}`, selected)).join("")}</select>`;
}

function evalSetSelect(name: string, evalSets: ApiRecord[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">No eval gate</option>${evalSets.map((evalSet) => option(String(evalSet.id), String(evalSet.name || evalSet.id), selected)).join("")}</select>`;
}

function machineSelect(name: string, machines: MachineRecord[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">Select machine</option>${machines.map((machine) => option(machine.id, machine.hostname, selected)).join("")}</select>`;
}

function fleetSelect(name: string, fleets: FleetRecord[], selected: string, disabled = false): string {
  return `<select name="${escapeHtml(name)}"${disabled ? " disabled" : ""}><option value="">No fleet</option>${fleets.map((fleet) => option(fleet.id, fleet.name, selected)).join("")}</select>`;
}

function defaultFleetId(data: DashboardData): string {
  return data.fleets.length === 1 ? data.fleets[0].id : "";
}

function select(name: string, values: string[], selected: string, disabled = false, formId = ""): string {
  const attrs = `${formId ? ` form="${escapeHtml(formId)}"` : ""}${disabled ? " disabled" : ""}`;
  return `<select name="${escapeHtml(name)}"${attrs}>${values.map((value) => option(value, labelize(value), selected)).join("")}</select>`;
}

function option(value: string, label: string, selected: string): string {
  return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
}

function taskOrigin(task: TaskRecord): JsonObject {
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const origin = metadata.origin;
  return origin && typeof origin === "object" ? origin as JsonObject : {};
}

function mustData(): DashboardData {
  if (!state.data) throw new Error("dashboard data is not loaded");
  return state.data;
}

function parseJsonObject(value: unknown): JsonObject {
  const text = String(value || "").trim();
  if (!text) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`invalid JSON: ${detail}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("metadata must be a JSON object, e.g. {\"key\": \"value\"}");
  }
  return parsed as JsonObject;
}

function parseJsonValue(value: unknown): unknown {
  const text = String(value || "").trim();
  if (!text) return "";
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function parseJsonArray(value: unknown): JsonObject[] {
  const text = String(value || "").trim();
  if (!text) return [];
  const parsed = JSON.parse(text);
  if (!Array.isArray(parsed)) {
    throw new Error("expected a JSON array");
  }
  return parsed as JsonObject[];
}

function parseJsonAnyArray(value: unknown): unknown[] {
  const text = String(value || "").trim();
  if (!text) return [];
  const parsed = JSON.parse(text);
  if (!Array.isArray(parsed)) {
    throw new Error("expected a JSON array");
  }
  return parsed;
}

function parseJsonStringArray(value: unknown): string[] {
  return parseJsonAnyArray(value).map((item) => String(item)).filter((item) => item.trim());
}

function requiredString(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) throw new Error("required field is blank");
  return text;
}

function requiredDataset(form: HTMLFormElement, key: string): string {
  const value = form.dataset[key];
  if (!value) throw new Error(`missing action context: ${key}`);
  return value;
}

function numberValue(value: unknown, fallback: number): number {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) throw new Error(`expected number: ${text}`);
  return parsed;
}

function optionalNumber(value: unknown): number | null {
  const text = String(value || "").trim();
  return text ? numberValue(text, 0) : null;
}

function boolValue(value: unknown): string {
  return value === "on" || value === true || value === "true" ? "true" : "false";
}

function csvList(value: unknown): string[] {
  return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function listFromUnknown(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter(Boolean);
}

function jsonValue(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value || "");
  }
}

function emptyToNull(value: unknown): string | null {
  const text = String(value || "").trim();
  return text || null;
}

function actionSuccessMessage(action: string, result: unknown): string {
  const record = result && typeof result === "object" ? result as JsonObject : {};
  if (action === "taskCreate" || action === "workflowPlanningTaskCreate") {
    return `Task created: ${compactObjectTitle(record)}`;
  }
  if (action === "workflowChainTaskAdd") {
    const children = Array.isArray(record.children) ? record.children : [];
    const firstChild = children.find((item) => item && typeof item === "object") as JsonObject | undefined;
    return firstChild
      ? `Task chain updated: added ${compactObjectTitle(firstChild)}`
      : "Task chain updated";
  }
  if (action === "workflowPlanAccept") {
    const created = Array.isArray(record.created) ? record.created : [];
    return `Workflow accepted: ${created.length} tasks created`;
  }
  if (action === "projectCreate") return `Project created: ${compactObjectTitle(record)}`;
  if (action === "projectUpdate") return `Project saved: ${compactObjectTitle(record)}`;
  const summary = jsonSummary(record);
  return summary === "none" ? `${labelize(action)} ok` : `${labelize(action)} ok: ${summary}`;
}

function compactObjectTitle(record: JsonObject): string {
  const name = String(record.title || record.name || record.project || record.id || "record");
  const id = String(record.id || "");
  return id && id !== name ? `${name} (${id})` : name;
}

function redactedJson(value: unknown): string {
  return JSON.stringify(value, (key, item) => key === "value" ? "***REDACTED***" : item);
}

function jsonSummary(value: unknown): string {
  if (value == null || typeof value !== "object") return value == null ? "none" : String(value);
  const keys = Object.keys(value as JsonObject);
  if (!keys.length) return "none";
  return keys.slice(0, 4).map((key) => `${key}:${compactValue((value as JsonObject)[key])}`).join(", ");
}

function compactValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.slice(0, 3).join("|")}${value.length > 3 ? "|..." : ""}]`;
  if (value && typeof value === "object") return "{...}";
  return String(value);
}

function shortHash(value: unknown): string {
  const text = String(value || "");
  return text.length > 16 ? `${text.slice(0, 12)}...` : text || "no digest";
}

function selectedClass(id: string): string {
  return state.selectedId && state.selectedId === id ? "is-selected" : "";
}

function truncate(value: unknown, limit: number): string {
  const text = String(value || "");
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
}

function statusTone(status: string): Tone {
  if (status === "idle") return "good";
  if (status === "busy") return "info";
  if (status === "draining") return "warn";
  return "bad";
}

function projectTone(status: string): Tone {
  if (status === "active") return "good";
  if (status === "inactive" || status === "archived") return "warn";
  return "info";
}

function healthTone(status: string): Tone {
  if (["healthy", "ready", "configured"].includes(status)) return "good";
  if (["degraded", "degraded_allowed", "unknown"].includes(status)) return "warn";
  return "bad";
}

function observationTone(level: string): Tone {
  if (level === "critical" || level === "error") return "bad";
  if (level === "warning") return "warn";
  if (level === "debug") return "info";
  return "good";
}

function formatMetricValue(item: ObservabilityEvent): string {
  if (item.value == null) return "none";
  const value = Math.abs(item.value) >= 100 ? Math.round(item.value) : Math.round(item.value * 100) / 100;
  return `${value}${item.unit ? ` ${item.unit}` : ""}`;
}

function rolloutTone(status: string): Tone {
  if (status === "promoted") return "good";
  if (["planned", "canarying", "paused"].includes(status)) return "info";
  if (["rescuing", "rolled_back"].includes(status)) return "warn";
  return "bad";
}

function runtimeDeltaTone(status: string): Tone {
  if (status === "promoted" || status === "validated") return "good";
  if (status === "proposed") return "warn";
  if (status === "rejected") return "bad";
  return "info";
}

function formatAge(value: string | null | undefined): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "unknown";
  const diffMs = Date.now() - date.getTime();
  const suffix = diffMs >= 0 ? "ago" : "from now";
  const minutes = Math.max(1, Math.round(Math.abs(diffMs) / 60000));
  if (minutes < 60) return `${minutes}m ${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ${suffix}`;
  return `${Math.round(hours / 24)}d ${suffix}`;
}

function formatTime(value: Date): string {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(value);
}

function labelize(value: unknown): string {
  return String(value == null || value === "" ? "none" : value).replaceAll("_", " ");
}

function escapeHtml(value: unknown): string {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => {
    const replacements: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return replacements[char];
  });
}

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`Missing dashboard element: ${selector}`);
  return element as T;
}
