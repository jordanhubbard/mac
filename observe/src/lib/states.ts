// The domain's state machines, and how each state is drawn.
//
// Colour assignment follows the data-viz form rules rather than taste:
//
//  * In-flight task states are an ORDERED pipeline (open -> ... -> reviewing),
//    so they get a single-hue ORDINAL blue ramp — depth in the pipeline is a
//    magnitude, not an identity. Validated with
//    `validate_palette.js "#b7d3f6,#86b6ef,#3987e5,#184f95" --mode dark --ordinal`.
//  * Terminal and exception states are STATUS, so they take the reserved
//    status palette (good / warning / serious / critical), which is never
//    reused for a series. Every status colour is rendered beside its label,
//    never as colour alone.
//  * Non-task categoricals (review, publication, lease, ... statuses) draw
//    from the validated categorical order.
//
// Adding a colour here means re-running the validator. Do not eyeball it.

export const TASK_STATES = [
  "open",
  "waiting",
  "blocked",
  "claimed",
  "running",
  "needs_review",
  "needs_input",
  "reviewing",
  "completed",
  "failed",
  "cancelled",
] as const;

export type TaskState = (typeof TASK_STATES)[number];

export const TERMINAL_TASK_STATES: readonly string[] = [
  "completed",
  "failed",
  "cancelled",
];

/** Pipeline order for the flow columns: earliest stage first, sinks last. */
export const FLOW_ORDER: readonly string[] = [
  "open",
  "waiting",
  "blocked",
  "needs_input",
  "claimed",
  "running",
  "needs_review",
  "reviewing",
  "completed",
  "failed",
  "cancelled",
];

const ORDINAL = ["#b7d3f6", "#86b6ef", "#3987e5", "#184f95"];
const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;
const CATEGORICAL = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"];
const MUTED = "#898781";

const TASK_STATE_COLOR: Record<string, string> = {
  // Ordinal ramp: how deep into the pipeline the work has travelled.
  open: ORDINAL[0],
  claimed: ORDINAL[1],
  running: ORDINAL[2],
  reviewing: ORDINAL[3],
  needs_review: ORDINAL[3],
  // Status: states that mean something is wrong or waiting on a human.
  waiting: STATUS.warning,
  needs_input: STATUS.warning,
  blocked: STATUS.serious,
  completed: STATUS.good,
  failed: STATUS.critical,
  cancelled: MUTED,
};

export function taskStateColor(state: string): string {
  return TASK_STATE_COLOR[state] ?? MUTED;
}

/**
 * Whether a task state is an exception the operator should look at.
 * Used for icon+label pairing so the status hue never carries meaning alone.
 */
export function taskStateTone(state: string): "good" | "warn" | "bad" | "flow" {
  if (state === "completed") return "good";
  if (state === "failed") return "bad";
  if (state === "blocked" || state === "needs_input" || state === "waiting") {
    return "warn";
  }
  return "flow";
}

/** Stable categorical colour for a non-task status, assigned by fixed order. */
export function categoricalColor(key: string, universe: readonly string[]): string {
  const index = universe.indexOf(key);
  if (index < 0 || index >= CATEGORICAL.length) return MUTED;
  return CATEGORICAL[index];
}

/** Health/status colouring shared by agents and pipeline statuses. */
export function healthColor(value: string | null | undefined): string {
  switch (value) {
    case "healthy":
    case "active":
    case "approved":
    case "published":
    case "completed":
    case "certified":
      return STATUS.good;
    case "degraded":
    case "pending":
    case "queued":
    case "paused":
    case "changes_requested":
      return STATUS.warning;
    case "draining":
    case "expired":
    case "stale":
      return STATUS.serious;
    case "unhealthy":
    case "failed":
    case "rejected":
    case "aborted":
      return STATUS.critical;
    default:
      return MUTED;
  }
}

export function agentStatusColor(status: string | null | undefined): string {
  switch (status) {
    case "idle":
      return CATEGORICAL[2];
    case "busy":
      return CATEGORICAL[0];
    case "draining":
      return STATUS.warning;
    case "offline":
      return MUTED;
    default:
      return MUTED;
  }
}

export { STATUS as STATUS_COLORS, MUTED as MUTED_COLOR };

/**
 * Order a set of observed state names for display: known pipeline order first,
 * then anything the hub invented that this build has not heard of. Unknown
 * states are shown, not dropped — a state we do not recognise is information.
 */
export function orderStates(observed: Iterable<string>): string[] {
  const seen = new Set(observed);
  const known = FLOW_ORDER.filter((s) => seen.has(s));
  const unknown = [...seen].filter((s) => !FLOW_ORDER.includes(s)).sort();
  return [...known, ...unknown];
}
