import { type FormEvent, useEffect, useState } from "react";
import { api, type ActivityEntry, type TaskDetail, type TaskDispatchExplanation } from "../api/mac";

type BlockedContext = {
  actor: string;
  at: string;
  blockingTasks: string[];
  detail: Record<string, unknown>;
  error: string;
  problems: string[];
  question: string;
  reason: string;
};

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : value === undefined || value === null ? "" : String(value);
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(stringValue).filter(Boolean) : [];
}

export function latestBlockedContext(detail: TaskDetail): BlockedContext | null {
  const history = detail.history || [];
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const entry = history[index];
    if (String(entry.to_state || "") !== "blocked") continue;
    const context = entry.detail && typeof entry.detail === "object" && !Array.isArray(entry.detail)
      ? entry.detail as Record<string, unknown>
      : {};
    return {
      actor: stringValue(entry.actor),
      at: stringValue(entry.created_at),
      blockingTasks: stringList(context.blocked_by_task_ids || context.dependencies),
      detail: context,
      error: stringValue(context.error),
      problems: stringList(context.problems),
      question: stringValue(context.question || context.prompt),
      reason: stringValue(context.reason || context.summary || context.message),
    };
  }
  return null;
}

function blockedContext(detail: TaskDetail): BlockedContext | null {
  return latestBlockedContext(detail);
}

function appendOperatorDirection(detail: TaskDetail, direction: string, at: string) {
  const task = detail.task;
  const currentDescription = String(task.description || "").trimEnd();
  const note = `Operator direction (${at}):\n${direction}`;
  const metadata = { ...(task.metadata || {}) };
  const currentActivity = Array.isArray(metadata.activity) ? metadata.activity : [];
  const activityEntry: ActivityEntry = {
    phase: "direction",
    actor: "human",
    summary: direction,
    at,
  };
  const currentGuidance = Array.isArray(metadata.operator_guidance) ? metadata.operator_guidance : [];
  return {
    description: currentDescription ? `${currentDescription}\n\n${note}` : note,
    metadata: {
      ...metadata,
      activity: [...currentActivity, activityEntry].slice(-24),
      operator_guidance: [...currentGuidance, { actor: "human", at, direction }].slice(-24),
    },
  };
}

export function TaskInspector({
  detail,
  canWrite,
  onClose,
  onRefresh,
  embedded = false,
}: {
  detail: TaskDetail;
  canWrite: boolean;
  onClose: () => void;
  onRefresh: () => void | Promise<void>;
  embedded?: boolean;
}) {
  const [direction, setDirection] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [dispatch, setDispatch] = useState<TaskDispatchExplanation | null>(null);
  const task = detail.task;
  const blocked = String(task.state) === "blocked";
  const waiting = String(task.state) === "waiting";
  const context = blockedContext(detail);
  const waitingOn = stringList(task.dependencies);

  useEffect(() => {
    let active = true;
    setDispatch(null);
    if (String(task.state) !== "open") return () => { active = false; };
    void api.explainTaskDispatch(task.id)
      .then((value) => { if (active) setDispatch(value); })
      .catch(() => { if (active) setDispatch(null); });
    return () => { active = false; };
  }, [task.id, task.state]);

  async function provideDirection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = direction.trim();
    if (!value || busy || !canWrite) return;
    setBusy(true);
    setMessage("");
    try {
      const at = new Date().toISOString();
      await api.updateTask(task.id, {
        ...appendOperatorDirection(detail, value, at),
        actor: "human",
      });
      await api.reopenTask(task.id, value);
      await onRefresh();
      onClose();
    } catch (error) {
      setMessage(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy(false);
    }
  }

  const inspector = (
      <section aria-labelledby="task-inspector-title" aria-modal={embedded ? undefined : true} className={`task-inspector ${embedded ? "task-inspector-embedded" : ""}`} role={embedded ? "region" : "dialog"}>
        <header className="task-inspector-header">
          <div>
            <span className="eyebrow">Task</span>
            <h2 id="task-inspector-title">{task.title || task.id}</h2>
            <small>{task.id}</small>
          </div>
          <button aria-label={embedded ? "Return to Work view" : "Close task inspector"} autoFocus className="icon-button" onClick={onClose} type="button">
            <i className={`codicon codicon-${embedded ? "arrow-left" : "close"}`} />
          </button>
        </header>

        <div className="task-inspector-facts">
          <span><small>State</small><strong className={`state-label state-${task.state}`}>{task.state}</strong></span>
          <span><small>Project</small><strong>{task.project || "unassigned"}</strong></span>
          <span><small>Owner</small><strong>{task.owner_agent_id?.replace(/^agent_/, "") || "unowned"}</strong></span>
          <span><small>Priority</small><strong>P{task.priority ?? 0}</strong></span>
        </div>

        {task.description ? <div className="task-inspector-section"><h3>Description</h3><p>{task.description}</p></div> : null}

        {blocked ? (
          <div className="task-inspector-section blocked-context">
            <h3>Why this task is blocked</h3>
            {context ? (
              <>
                {context.reason ? <p><strong>Reason:</strong> {context.reason}</p> : null}
                {context.question ? <p><strong>Question:</strong> {context.question}</p> : null}
                {context.error ? <p><strong>Error:</strong> {context.error}</p> : null}
                {context.blockingTasks.length ? <p><strong>Blocking tasks:</strong> {context.blockingTasks.join(", ")}</p> : null}
                {context.problems.map((problem) => <p key={problem}><strong>Problem:</strong> {problem}</p>)}
                <small>Recorded by {context.actor || "unknown"}{context.at ? ` at ${context.at}` : ""}</small>
                {!context.reason && !context.question && !context.error && !context.blockingTasks.length && !context.problems.length ? (
                  <details open><summary>Recorded block context</summary><pre>{JSON.stringify(context.detail, null, 2)}</pre></details>
                ) : null}
              </>
            ) : <p>The task ledger contains an invalid blocked transition with no reason. Reopen it with direction or repair the ledger event before redispatch.</p>}
          </div>
        ) : null}

        {waiting ? (
          <div className="task-inspector-section waiting-context">
            <h3>Waiting for dependencies</h3>
            {waitingOn.length ? (
              <p><strong>Incomplete dependency tasks:</strong> {waitingOn.join(", ")}</p>
            ) : (
              <p>This task has no dependency ids recorded. The ledger reconciler will reopen it or report the invalid waiting state.</p>
            )}
            <small>This is a scheduling wait, not a request for operator direction.</small>
          </div>
        ) : null}

        {String(task.state) === "open" && dispatch ? (
          <div className={`task-inspector-section dispatch-context ${dispatch.dispatchable ? "dispatch-ready" : "dispatch-unavailable"}`}>
            <h3>{dispatch.dispatchable ? "Ready for dispatch" : "Why this task is unclaimed"}</h3>
            {dispatch.unclaimed_reasons.map((reason) => (
              <p key={reason.code}><strong>{reason.code.replaceAll("_", " ")}:</strong> {reason.message}</p>
            ))}
            <small>{dispatch.eligible_agent_count} of {dispatch.candidate_count} registered agents currently eligible.</small>
            {!dispatch.dispatchable && dispatch.candidates.length ? (
              <details>
                <summary>Agent-specific rejections</summary>
                {dispatch.candidates.slice(0, 8).map((candidate) => (
                  <p key={candidate.agent_id}>
                    <strong>{candidate.agent_name || candidate.agent_id}:</strong>{" "}
                    {candidate.reasons.map((reason) => reason.message).join("; ") || "eligible"}
                  </p>
                ))}
              </details>
            ) : null}
          </div>
        ) : null}

        {blocked ? (
          <form className="direction-form" onSubmit={provideDirection}>
            <label htmlFor="operator-direction">Provide the missing direction</label>
            <textarea
              disabled={!canWrite || busy}
              id="operator-direction"
              onChange={(event) => setDirection(event.target.value)}
              placeholder="Tell the next worker what decision or information unblocks this task."
              required
              value={direction}
            />
            <div className="direction-actions">
              <span className="form-message">{message || (!canWrite ? "This session does not have write access." : "Direction is saved to the task and its audit history.")}</span>
              <button className="button primary" disabled={!canWrite || busy || !direction.trim()} type="submit">
                {busy ? "Reopening…" : "Provide direction and reopen"}
              </button>
            </div>
          </form>
        ) : null}

        <div className="task-inspector-section task-history">
          <h3>Recent history</h3>
          {(detail.history || []).slice(-6).reverse().map((entry, index) => (
            <div className="task-history-entry" key={String(entry.id || `${entry.created_at || "history"}-${index}`)}>
              <strong>{String(entry.event_type || "task event").replaceAll("_", " ")}</strong>
              <span>{entry.from_state ? `${entry.from_state} → ${entry.to_state || "—"}` : String(entry.to_state || "")}</span>
              <small>{String(entry.actor || "unknown")} · {String(entry.created_at || "")}</small>
            </div>
          ))}
        </div>
      </section>
  );
  if (embedded) return inspector;
  return (
    <div className="task-inspector-backdrop" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      {inspector}
    </div>
  );
}
