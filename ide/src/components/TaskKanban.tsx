import { useMemo, useState } from "react";
import type { TaskDetail } from "../api/mac";

const TASK_LANES = [
  ["open", "Open"],
  ["waiting", "Waiting"],
  ["blocked", "Blocked"],
  ["claimed", "Claimed"],
  ["running", "Running"],
  ["needs_review", "Needs review"],
  ["reviewing", "Reviewing"],
  ["completed", "Completed"],
  ["failed", "Failed"],
  ["cancelled", "Cancelled"],
] as const;
const INITIAL_CARDS_PER_LANE = 30;

function display(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

export function TaskKanban({
  tasks,
  selectedTaskId,
  onSelectTask,
  onInspectTask,
}: {
  tasks: TaskDetail[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  onInspectTask: (taskId: string) => void;
}) {
  const [laneLimits, setLaneLimits] = useState<Record<string, number>>({});
  const lanes = useMemo(() => {
    const grouped = new Map<string, TaskDetail[]>();
    for (const detail of tasks) {
      const state = String(detail.task.state || "open");
      const lane = grouped.get(state) || [];
      lane.push(detail);
      grouped.set(state, lane);
    }
    return TASK_LANES.map(([state, label]) => ({
      state,
      label,
      tasks: grouped.get(state) || [],
    }));
  }, [tasks]);

  return (
    <div aria-label="Task Kanban" className="task-kanban" role="region">
      {lanes.map((lane) => {
        const limit = laneLimits[lane.state] || INITIAL_CARDS_PER_LANE;
        const visibleTasks = lane.tasks.slice(0, limit);
        const remaining = lane.tasks.length - visibleTasks.length;
        return (
        <section
          aria-labelledby={`kanban-lane-${lane.state}`}
          className={`kanban-lane state-${lane.state}`}
          data-state={lane.state}
          key={lane.state}
        >
          <header className="kanban-lane-header">
            <h2 id={`kanban-lane-${lane.state}`}>{lane.label}</h2>
            <strong aria-label={`${lane.tasks.length} tasks`}>{lane.tasks.length}</strong>
          </header>
          <div className="kanban-lane-body">
            {lane.tasks.length ? visibleTasks.map(({ task }) => {
              const selected = task.id === selectedTaskId;
              return (
                <article
                  className={`kanban-card state-${task.state || "open"} ${selected ? "selected" : ""}`}
                  data-task-id={task.id}
                  key={task.id}
                  onDoubleClick={() => onInspectTask(task.id)}
                >
                  <button
                    aria-pressed={selected}
                    className="kanban-card-select"
                    onClick={() => onSelectTask(task.id)}
                    type="button"
                  >
                    <span className="kanban-card-title">{display(task.title, task.id)}</span>
                    <span className="kanban-card-id">{task.id}</span>
                    {task.description ? <span className="kanban-card-description">{task.description}</span> : null}
                    <span className="kanban-card-meta">
                      <span>P{task.priority ?? 0}</span>
                      <span>{display(task.project, "unassigned")}</span>
                      <span>{display(task.owner_agent_id, "unowned").replace(/^agent_/, "")}</span>
                    </span>
                  </button>
                  <button className="kanban-inspect" onClick={() => onInspectTask(task.id)} type="button">
                    Inspect
                  </button>
                </article>
              );
            }) : <span className="kanban-empty">No tasks</span>}
            {remaining > 0 ? (
              <button
                className="kanban-show-more"
                onClick={() => setLaneLimits((current) => ({
                  ...current,
                  [lane.state]: limit + INITIAL_CARDS_PER_LANE,
                }))}
                type="button"
              >
                Show {Math.min(INITIAL_CARDS_PER_LANE, remaining)} more
                <small>{remaining} remaining</small>
              </button>
            ) : null}
          </div>
        </section>
        );
      })}
    </div>
  );
}
