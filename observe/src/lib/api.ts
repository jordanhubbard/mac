// Types for `GET /dashboard/observe` (schema `mac.dashboard.observe.v1`) and
// the thin client around it.
//
// Every section is OPTIONAL in the type, and that is load-bearing: the hub
// omits any section it could not read and names it in `degraded`. Making these
// non-optional would let the UI render `?? 0` and quietly invent health.

import { createReadOnlyFetch, type ReadOnlyFetch } from "./http";

export interface Degradation {
  section: string;
  reason: string;
}

export interface Dwell {
  count: number;
  p50: number | null;
  p90: number | null;
  max: number | null;
}

export interface TasksSection {
  by_state: Record<string, number>;
  total: number;
  live_total: number;
  dwell_seconds: Record<string, Dwell>;
  undated_rows: number;
}

export interface StuckTask {
  id: string;
  title: string;
  state: string;
  project: string | null;
  owner_agent_id: string | null;
  updated_at: string | null;
  created_at: string | null;
  attempt_count: number | null;
  max_attempts: number | null;
  dwell_seconds: number | null;
  age_seconds: number | null;
}

export interface ProjectRow {
  project: string;
  by_state: Record<string, number>;
  total: number;
  live: number;
}

export interface ProjectsSection {
  registered_by_status: Record<string, number>;
  with_tasks: number;
  rows: ProjectRow[];
  truncated: number;
}

export interface FlowSection {
  bucket_seconds: number;
  bucket_starts: string[];
  series: Record<string, number[]>;
  dropped_rows: number;
  total: number;
}

export interface TransitionRow {
  task_id: string;
  from_state: string | null;
  to_state: string | null;
  actor: string | null;
  created_at: string | null;
  title: string | null;
  project: string | null;
  age_seconds: number | null;
}

export interface AgentRow {
  id: string;
  name: string;
  status: string;
  health_status: string;
  instance_kind: string | null;
  current_task_id: string | null;
  last_seen_at: string | null;
  seconds_since_seen: number | null;
  open_tasks: number;
  active_leases: number;
  belief_contradicted: boolean;
  dispatch_hold: number | null;
}

export interface AgentsSection {
  by_status: Record<string, number>;
  by_health: Record<string, number>;
  rows: AgentRow[];
  total: number;
  truncated: number;
}

export interface PipelinesSection {
  reviews: Record<string, number>;
  publications: Record<string, number>;
  leases: Record<string, number>;
}

export interface NapRow {
  id: string;
  agent_id: string | null;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  age_seconds: number | null;
}

export interface CyclesSection {
  naps_by_status: Record<string, number>;
  recent_naps: NapRow[];
  schedules_total: number;
  schedules_enabled: number;
}

export interface DreamRow {
  id: string;
  agent_id: string | null;
  project: string | null;
  status: string;
  state: string;
  created_at: string | null;
  promoted_at: string | null;
  age_seconds: number | null;
}

export interface DreamsSection {
  by_status: Record<string, number>;
  by_state: Record<string, number>;
  recent: DreamRow[];
}

export interface AgentBusSection {
  streams_by_status: Record<string, number>;
  messages_by_status: Record<string, number>;
  chunks_in_window: number;
  chunk_bytes_in_window: number;
}

export interface TelemetrySection {
  cursor: number;
  events_total: number;
  events_in_window: number;
  by_level_in_window: Record<string, number>;
  top_names_in_window: Array<{ name: string; count: number }>;
  oldest_event_at: string | null;
  newest_event_at: string | null;
  retention_span_seconds: number | null;
}

export interface TranscriptCoverage {
  rows_total: number;
  tasks_with_transcript: number;
  tasks_total: number;
  /** null when there are no tasks — "no tasks" is not "0% coverage". */
  coverage_fraction: number | null;
  attributed_rows: number;
  unattributed_rows: number;
  commands_audited: number;
}

export interface MergeQueueRow {
  repository: string;
  branch: string;
  depth: number;
  by_state: Record<string, number>;
  /** null when the queue has never sized its window -- NOT the floor. */
  window_size: number | null;
  landed_count: number;
  failure_count: number;
  speculation_discarded: number;
  last_event: string;
  updated_at: string | null;
}

export interface MergeQueueEviction {
  repository: string;
  branch: string;
  task_id: string;
  pull_request_number: number;
  eviction_reason: string;
  updated_at: string | null;
}

export interface MergeQueueSection {
  queues: MergeQueueRow[];
  queue_count: number;
  total_depth: number;
  total_landed: number;
  total_failed: number;
  recent_evictions: MergeQueueEviction[];
  live_states: string[];
}

export interface Snapshot {
  schema: string;
  server_time: string;
  window: { hours: number; since: string; until: string };
  observability_sequence: number;
  build_ms: number;
  degraded: Degradation[];
  tasks?: TasksSection;
  stuck?: StuckTask[];
  projects?: ProjectsSection;
  flow?: FlowSection;
  transitions?: TransitionRow[];
  agents?: AgentsSection;
  pipelines?: PipelinesSection;
  cycles?: CyclesSection;
  dreams?: DreamsSection;
  merge_queue?: MergeQueueSection;
  agentbus?: AgentBusSection;
  telemetry?: TelemetrySection;
  transcripts?: TranscriptCoverage;
}

// --- task drill-down -------------------------------------------------------

export interface TaskDetail {
  id: string;
  title: string;
  description: string;
  state: string;
  project: string | null;
  priority: number;
  owner_agent_id: string | null;
  lease_id: string | null;
  leased_until: string | null;
  attempt_count: number;
  max_attempts: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  created_by_human: string | null;
  dwell_seconds: number | null;
  age_seconds: number | null;
}

export interface HistoryEntry {
  id: string;
  event_type: string;
  actor: string | null;
  from_state: string | null;
  to_state: string | null;
  created_at: string | null;
  age_seconds: number | null;
}

export interface TranscriptTurn {
  id: string;
  sequence: number;
  agent_id: string | null;
  command_id: string | null;
  /** null means nobody recorded which CLI ran — NOT "no CLI ran". */
  coding_agent: string | null;
  model: string | null;
  returncode: number | null;
  duration_ms: number | null;
  truncated: boolean;
  started_at: string | null;
  completed_at: string | null;
  compression: string | null;
  payload_bytes: number | null;
  has_payload: boolean;
  metadata: string | null;
  created_at: string | null;
}

export interface TranscriptsSection {
  rows: TranscriptTurn[];
  count: number;
  attributed: number;
  unattributed: number;
  truncated_list: boolean;
}

export interface CommandRow {
  id: string;
  command_id: string;
  agent_id: string | null;
  phase: string;
  argv: string;
  cwd: string | null;
  lease_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  returncode: number | null;
  stdout_bytes: number | null;
  stderr_bytes: number | null;
  created_at: string | null;
  age_seconds: number | null;
}

export interface EvidenceRow {
  id: string;
  kind: string;
  uri: string;
  summary: string;
  created_by: string;
  created_at: string | null;
}

export interface ReviewRow {
  id: string;
  reviewer_agent_id: string;
  status: string;
  created_at: string | null;
  completed_at: string | null;
}

export interface PublicationRow {
  id: string;
  status: string;
  created_at: string | null;
}

export interface TaskDrilldown {
  schema: string;
  server_time: string;
  task_id: string;
  found: boolean;
  build_ms: number;
  degraded: Degradation[];
  task?: TaskDetail;
  history?: HistoryEntry[];
  transcripts?: TranscriptsSection;
  commands?: CommandRow[];
  evidence?: EvidenceRow[];
  reviews?: ReviewRow[];
  publications?: PublicationRow[];
}

export interface ClippedText {
  text: string;
  clipped: boolean;
  full_length: number;
}

export interface TranscriptEntry {
  schema: string;
  transcript_id: string;
  found: boolean;
  task_id?: string;
  sequence?: number;
  agent_id?: string | null;
  command_id?: string | null;
  coding_agent?: string | null;
  model?: string | null;
  returncode?: number | null;
  duration_ms?: number | null;
  truncated_at_capture?: boolean;
  started_at?: string | null;
  completed_at?: string | null;
  prompt?: ClippedText;
  response?: ClippedText;
  stderr?: ClippedText;
  metadata_raw?: string | null;
}

export const EXPECTED_TASK_SCHEMA = "mac.dashboard.observe.task.v1";

export const EXPECTED_SCHEMA = "mac.dashboard.observe.v1";

export interface StreamEvent {
  event: "connected" | "updated" | "heartbeat" | string;
  server_time: string;
  observability_sequence: number;
}

// ---------------------------------------------------------------------------
// AgentBus
//
// The bus is a fleet-wide conversation between agents, and the read endpoints
// say so: they are self-only, so this console reads them AS an agent rather
// than as an anonymous viewer. `busIdentity()` reports which agent the current
// token IS; everything else needs that id.
// ---------------------------------------------------------------------------

/** Which agent this token joins the bus as — or why it cannot join at all. */
export interface BusIdentity {
  schema: string;
  agent_id: string | null;
  joined: boolean;
  reason: string;
}

/**
 * One thing said on the bus.
 *
 * `addressed_to` is ADDRESSING, NOT ACCESS. Point-to-point messages are not
 * private; the field names who is expected to answer, by the convention that
 * an agent does not answer until addressed by name. The view renders it as
 * "→ names", never as a lock.
 */
export interface BusMessage {
  cursor: string;
  topic: string;
  from_agent_id: string;
  addressed_to: string[];
  addressed_to_me: boolean;
  reply_expected: boolean;
  chunk: {
    id: string;
    stream_id: string;
    sequence: number;
    sender_agent_id: string;
    content_type?: string;
    payload?: Record<string, unknown>;
    created_at?: string;
    [key: string]: unknown;
  };
}

/** One agent on the roll call, in the hub's existing inventory shape. */
export interface BusRosterAgent {
  id: string;
  name: string;
  capabilities: string[];
  status?: string | null;
  health_status?: string | null;
  current_task_id?: string | null;
  last_seen_at?: string | null;
  role_id?: string | null;
}

export interface BusRollCall {
  schema: string;
  counted_at: string;
  agent_count: number;
  agents: BusRosterAgent[];
}

export const EXPECTED_BUS_IDENTITY_SCHEMA = "mac.agentbus.identity.v1";

export class ConsoleClient {
  private readonly get: ReadOnlyFetch;

  constructor(tokenProvider: () => string, fetchImpl: typeof fetch = fetch) {
    this.get = createReadOnlyFetch(tokenProvider, (path, init) =>
      fetchImpl(path, init),
    );
  }

  async snapshot(windowHours: number, buckets: number): Promise<Snapshot> {
    const response = await this.get(
      `/dashboard/observe?window_hours=${encodeURIComponent(
        windowHours,
      )}&buckets=${encodeURIComponent(buckets)}`,
      { timeoutMs: 20_000 },
    );
    return (await response.json()) as Snapshot;
  }

  async task(taskId: string): Promise<TaskDrilldown> {
    const response = await this.get(
      `/dashboard/observe/tasks/${encodeURIComponent(taskId)}`,
      { timeoutMs: 20_000 },
    );
    return (await response.json()) as TaskDrilldown;
  }

  /** One transcript turn's text. Fetched only when a turn is expanded. */
  async transcript(transcriptId: string): Promise<TranscriptEntry> {
    const response = await this.get(
      `/dashboard/observe/transcripts/${encodeURIComponent(transcriptId)}`,
      { timeoutMs: 30_000 },
    );
    return (await response.json()) as TranscriptEntry;
  }

  /**
   * Which agent this token is on the bus.
   *
   * Called before any bus read, because the traffic and roll-call routes bind
   * the path agent to the bearer principal. Asking the hub is the only honest
   * way to know: the console cannot infer an agent id from a token, and
   * guessing produces a 403 that looks like the hub being down.
   */
  async busIdentity(): Promise<BusIdentity> {
    const response = await this.get("/agentbus/identity", { timeoutMs: 10_000 });
    return (await response.json()) as BusIdentity;
  }

  /**
   * Everything being said on the bus, as `agentId` hears it, after `cursor`.
   *
   * Cursored rather than windowed: the view appends, so a message is rendered
   * once and a quiet bus costs one empty read per poll instead of re-fetching
   * a backlog it already has.
   */
  async busTraffic(
    agentId: string,
    cursor: string,
    limit = 100,
  ): Promise<BusMessage[]> {
    const response = await this.get(
      `/agents/${encodeURIComponent(agentId)}/agentbus/traffic` +
        `?after_cursor=${encodeURIComponent(cursor)}&limit=${encodeURIComponent(limit)}`,
      { timeoutMs: 20_000 },
    );
    return (await response.json()) as BusMessage[];
  }

  /** Who is on the bus, and what each of them can do. */
  async busRollCall(agentId: string): Promise<BusRollCall> {
    const response = await this.get(
      `/agents/${encodeURIComponent(agentId)}/agentbus/roll-call`,
      { timeoutMs: 20_000 },
    );
    return (await response.json()) as BusRollCall;
  }

  /**
   * Subscribe to `/dashboard/stream`, calling `onEvent` per NDJSON line.
   *
   * The hub's stream carries a cursor, not the data — an "updated" event means
   * "something moved, come and look". The stream also ends on its own deadline
   * (default 60s), so this reconnects until aborted. Reconnection is the normal
   * path, not an error path.
   */
  async subscribe(
    onEvent: (event: StreamEvent) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const response = await this.get("/dashboard/stream?timeout_seconds=55", {
      accept: "application/x-ndjson",
      signal,
      timeoutMs: 0, // the stream is long-lived by design; only `signal` ends it
    });
    const body = response.body;
    if (!body) throw new Error("hub returned a stream with no body");
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) {
            try {
              onEvent(JSON.parse(line) as StreamEvent);
            } catch {
              /* a malformed frame is not a reason to drop the stream */
            }
          }
          newline = buffer.indexOf("\n");
        }
      }
    } finally {
      reader.cancel().catch(() => undefined);
    }
  }
}
