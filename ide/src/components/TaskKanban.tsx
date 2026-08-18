import { useEffect, useMemo, useState } from "react";
import { api } from "../api/mac";
import type { Task, TaskDetail, TaskGroup, TaskSelection } from "../api/mac";

const TASK_LANES = [
  ["open", "Open"],
  ["waiting", "Waiting"],
  ["blocked", "Blocked"],
  ["claimed", "Claimed"],
  ["running", "Running"],
  ["needs_input", "Needs your input"],
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

interface NeedsInputQuestion {
  question: string;
  why: string;
}

export function needsInputQuestions(task: Task): NeedsInputQuestion[] {
  const payload = (task.metadata?.needs_input || {}) as Record<string, unknown>;
  const raw = Array.isArray(payload.questions) ? payload.questions : [];
  return raw
    .map((entry) => {
      const item = (entry || {}) as Record<string, unknown>;
      return { question: String(item.question || ""), why: String(item.why || "") };
    })
    .filter((item) => item.question);
}

/**
 * The answer form on a parked task's card.
 *
 * Deliberately the same operation as `mac task edit`: supply the missing
 * information and the task returns to the pending queue. A parked task is
 * excluded from every sweeper and dispatch pass, so this form is the only
 * thing that will ever move it -- which is why the card carries it directly
 * rather than hiding it behind the inspector.
 */
function NeedsInputCardForm({ task, onSubmitted }: { task: Task; onSubmitted: () => void }) {
  const questions = needsInputQuestions(task);
  const originalDescription = String(task.description || "");
  const [answer, setAnswer] = useState("");
  const [description, setDescription] = useState(originalDescription);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const trimmed = answer.trim();
    if (!trimmed) {
      setError("An answer is required: the question is what is blocking this task.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // Persist a revised description first, so a failure there cannot leave
      // the task requeued against stale text.
      if (description !== originalDescription) {
        await api.updateTask(task.id, { description, actor: "human" });
      }
      await api.answerTaskInput(task.id, trimmed);
      setAnswer("");
      onSubmitted();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Answer required" className="needs-input-form" data-needs-input={task.id}>
      {questions.length ? (
        <ol className="needs-input-questions">
          {questions.map((item, index) => (
            <li key={`${task.id}-q${index}`}>
              {item.question}
              {item.why ? <small className="needs-input-why">{item.why}</small> : null}
            </li>
          ))}
        </ol>
      ) : null}
      <label className="needs-input-label" htmlFor={`needs-input-answer-${task.id}`}>
        Your answer
      </label>
      <textarea
        className="needs-input-answer"
        data-needs-input-answer
        id={`needs-input-answer-${task.id}`}
        onChange={(event) => setAnswer(event.target.value)}
        placeholder="Answer the question(s) above"
        rows={3}
        value={answer}
      />
      <details className="needs-input-details">
        <summary>Edit description</summary>
        <textarea
          aria-label="Task description"
          className="needs-input-description"
          data-needs-input-description
          onChange={(event) => setDescription(event.target.value)}
          rows={4}
          value={description}
        />
      </details>
      {error ? <p className="needs-input-error" role="alert">{error}</p> : null}
      <div className="needs-input-actions">
        <button
          className="needs-input-submit"
          data-needs-input-submit
          disabled={busy}
          onClick={submit}
          type="button"
        >
          {busy ? "Submitting…" : "Submit"}
        </button>
        <small>Submitting returns this task to the pending queue.</small>
      </div>
    </section>
  );
}

/**
 * Bulk actions over a group of tasks.
 *
 * The parked-task inbox is the case that forces this: the number of tasks
 * needing the same answer is set by the fleet, not by the person answering,
 * so a per-card form loses that race by construction.
 *
 * The safety shape is the CLI's, deliberately -- preview first, then apply
 * against the *token* of what was previewed. A count cannot tell you that one
 * task was cancelled and another created since you looked; the token can, and
 * the apply is refused rather than silently touching a task nobody saw.
 */
function TaskBatchBar({ onChanged }: { onChanged?: () => void }) {
  const [selector, setSelector] = useState("state=needs_input");
  const [answer, setAnswer] = useState("");
  const [preview, setPreview] = useState<TaskSelection | null>(null);
  const [groups, setGroups] = useState<TaskGroup[]>([]);
  const [groupName, setGroupName] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listTaskGroups().then(setGroups).catch(() => setGroups([]));
  }, []);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (cause) {
      // Selector and batch refusals carry their whole diagnosis in the
      // message -- which key was unknown, which group moved -- so show it
      // rather than a generic failure.
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const onPreview = () =>
    run(async () => {
      const result = await api.selectTasks(selector);
      setPreview(result);
      setStatus(null);
    });

  const onApply = () =>
    run(async () => {
      if (!preview) throw new Error("preview the group before applying");
      if (!answer.trim()) throw new Error("an answer is required");
      const outcome = await api.applyTaskBatch(
        selector,
        "answer",
        { answer: answer.trim() },
        true,
        preview.token,
      );
      setStatus(
        `answered ${outcome.changed_count} of ${outcome.matched}` +
          (outcome.failed_count ? `, ${outcome.failed_count} refused` : ""),
      );
      setPreview(null);
      setAnswer("");
      onChanged?.();
    });

  const onSaveGroup = () =>
    run(async () => {
      const name = groupName.trim();
      if (!name) throw new Error("name the group before saving it");
      await api.saveTaskGroup(name, selector);
      setGroups(await api.listTaskGroups());
      setGroupName("");
      setStatus(`saved group ${name}`);
    });

  return (
    <section aria-label="Bulk task actions" className="task-batch-bar">
      <div className="task-batch-selector">
        <label htmlFor="batch-selector">Group</label>
        <input
          data-batch-selector
          id="batch-selector"
          onChange={(event) => {
            setSelector(event.target.value);
            setPreview(null);
          }}
          placeholder="state=needs_input project=mac"
          value={selector}
        />
        <button data-batch-preview disabled={busy} onClick={onPreview} type="button">
          Preview
        </button>
        {groups.length ? (
          <select
            data-batch-group-pick
            onChange={(event) => {
              if (!event.target.value) return;
              setSelector(`group=${event.target.value}`);
              setPreview(null);
            }}
            value=""
          >
            <option value="">Saved groups…</option>
            {groups.map((group) => (
              <option key={group.name} value={group.name}>
                {group.name}
              </option>
            ))}
          </select>
        ) : null}
      </div>

      {preview ? (
        <div className="task-batch-preview" data-batch-preview-result>
          <strong>{preview.matched}</strong> task(s) match
          {preview.truncated ? <em> (showing {preview.returned})</em> : null}
          <ul>
            {preview.tasks.slice(0, 5).map((task) => (
              <li key={task.id}>
                {task.title}
                {task.questions.length ? <em> — {task.questions[0]}</em> : null}
              </li>
            ))}
          </ul>
          <div className="task-batch-apply">
            <input
              data-batch-answer
              onChange={(event) => setAnswer(event.target.value)}
              placeholder="Answer every task in this group"
              value={answer}
            />
            <button data-batch-apply disabled={busy} onClick={onApply} type="button">
              Answer {preview.matched}
            </button>
          </div>
          <div className="task-batch-save">
            <input
              data-batch-group-name
              onChange={(event) => setGroupName(event.target.value)}
              placeholder="Save this group as…"
              value={groupName}
            />
            <button data-batch-group-save disabled={busy} onClick={onSaveGroup} type="button">
              Save group
            </button>
          </div>
        </div>
      ) : null}

      {status ? <p data-batch-status>{status}</p> : null}
      {error ? (
        <p data-batch-error role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}

export function TaskKanban({
  tasks,
  selectedTaskId,
  onSelectTask,
  onInspectTask,
  onTaskChanged,
}: {
  tasks: TaskDetail[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  onInspectTask: (taskId: string) => void;
  onTaskChanged?: () => void;
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
    <>
    <TaskBatchBar onChanged={() => onTaskChanged?.()} />
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
              const publicationLane = task.publication_lane || task.publication_route?.lane || "unknown";
              const publicationLabel = publicationLane === "legacy"
                ? "legacy route"
                : "route unreported";
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
                      <span className={`publication-lane publication-lane-${publicationLane}`}>
                        {publicationLabel}
                      </span>
                    </span>
                  </button>
                  {task.state === "needs_input" ? (
                    <NeedsInputCardForm onSubmitted={() => onTaskChanged?.()} task={task} />
                  ) : null}
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
    </>
  );
}
