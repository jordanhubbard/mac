import { type TaskDetail, type ActivityEntry } from "../api/mac";

// A compact, dependency-free "task graph" ribbon: the per-task pipeline
// dispatch -> work -> review -> publish, lighting the active stage and naming the
// agent that played (or is playing) each role. One row, ~26px — designed to sit
// above the task detail without taking real estate. Real-time: it's a pure
// function of the polled TaskDetail, so it updates with the 5s refresh.

type StageState = "done" | "active" | "todo" | "error";

const STAGES: { key: string; label: string; phases: string[] }[] = [
  { key: "dispatch", label: "Dispatch", phases: ["dispatch", "claim"] },
  { key: "work", label: "Work", phases: ["worker", "execute", "build", "run"] },
  { key: "review", label: "Review", phases: ["review"] },
  { key: "publish", label: "Publish", phases: ["publish", "publication", "diagnosis"] },
];

function activeIndex(state?: string): number {
  switch ((state || "").toLowerCase()) {
    case "open":
    case "ready":
    case "blocked":
      return 0;
    case "claimed":
    case "running":
    case "in_progress":
      return 1;
    case "needs_review":
    case "reviewing":
    case "in_review":
      return 2;
    case "completed":
      return 4; // every stage done
    case "failed":
    case "cancelled":
      return -1; // error overlay on the current stage
    default:
      return 1;
  }
}

function shortAgent(actor?: string | null): string {
  if (!actor) return "";
  return String(actor).replace(/^agent_/, "");
}

// Best-effort: which agent acted in each stage, from the activity log + reviews +
// publications. Falls back to empty (role not yet entered).
function actorsByStage(detail: TaskDetail): Record<string, string> {
  const out: Record<string, string> = {};
  const acts = ((detail.task.metadata?.activity as ActivityEntry[]) || []);
  for (const a of acts) {
    const phase = String(a.phase || "").toLowerCase();
    for (const s of STAGES) {
      if (s.phases.some((p) => phase.includes(p))) out[s.key] = shortAgent(a.actor);
    }
  }
  const reviews = (detail.reviews as any[]) || [];
  if (reviews.length) {
    const r = reviews[reviews.length - 1];
    out.review = shortAgent(r.reviewer_agent_id || r.reviewer || r.actor || out.review);
  }
  const pubs = (detail as any).publications as any[] | undefined;
  if (pubs && pubs.length) out.publish = shortAgent(pubs[pubs.length - 1].created_by || out.publish);
  return out;
}

export function TaskPipeline({ detail }: { detail: TaskDetail | null }) {
  if (!detail) return null;
  const state = detail.task.state;
  const ai = activeIndex(state);
  const errored = ai === -1;
  const actors = actorsByStage(detail);

  return (
    <div className="pipeline" title={`task ${detail.task.id} · ${state}`}>
      {STAGES.map((s, i) => {
        let st: StageState = "todo";
        if (errored) st = i <= 1 ? "done" : i === 2 ? "error" : "todo";
        else if (i < ai) st = "done";
        else if (i === ai) st = "active";
        const who = actors[s.key];
        return (
          <div className={`pstage ${st}`} key={s.key}>
            {i > 0 && <span className="pedge" />}
            <span className="pdot" />
            <span className="plabel">{s.label}</span>
            {who && <span className="pwho">{who}</span>}
          </div>
        );
      })}
      <span className="pstate muted">{errored ? `⚠ ${state}` : state}</span>
    </div>
  );
}

// The live role an agent is playing right now, derived from the task it's on.
export function agentRole(
  agent: { current_task_id?: string | null; capabilities?: string[]; resources?: any },
  tasks: { id: string; state?: string }[]
): { role: string; cls: string } {
  if (!agent.current_task_id) return { role: "idle", cls: "idle" };
  const t = tasks.find((x) => x.id === agent.current_task_id);
  const s = (t?.state || "").toLowerCase();
  if (s === "needs_review" || s === "reviewing" || s === "in_review") return { role: "reviewer", cls: "review" };
  if (s === "running" || s === "claimed" || s === "in_progress") return { role: "worker", cls: "work" };
  if (s === "completed") return { role: "publisher", cls: "publish" };
  return { role: "active", cls: "work" };
}
