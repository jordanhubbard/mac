import { useDeferredValue, useMemo, useState } from "react";
import type { Agent, DashboardState, Task, TaskDetail } from "../api/mac";
import type { WorkbenchView } from "./ActivityRail";

function taskLabel(task: Task): string {
  return task.title || task.description?.split("\n")[0] || task.id;
}

function projectName(project: Record<string, unknown>): string {
  return String(project.name || project.project || project.id || "unassigned");
}

function agentInitials(agent: Agent): string {
  const value = agent.name || agent.id.replace(/^agent_/, "");
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

export function WorkbenchExplorer({
  data,
  activeView,
  selectedTaskId,
  selectedAgentId,
  onSelectTask,
  onSelectAgent,
}: {
  data: DashboardState;
  activeView: WorkbenchView;
  selectedTaskId: string | null;
  selectedAgentId: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectAgent: (agentId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("active");
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const tasks = useMemo(() => {
    const all = data.tasks.map((detail) => detail.task);
    return all
      .filter((task) => {
        if (stateFilter === "active" && ["completed", "cancelled", "failed"].includes(String(task.state))) return false;
        if (stateFilter !== "active" && stateFilter !== "all" && task.state !== stateFilter) return false;
        return !deferredQuery || [task.title, task.description, task.project, task.id]
          .some((value) => String(value || "").toLowerCase().includes(deferredQuery));
      })
      .sort((left, right) => (Number(right.priority || 0) - Number(left.priority || 0)) || taskLabel(left).localeCompare(taskLabel(right)));
  }, [data.tasks, deferredQuery, stateFilter]);
  const agents = data.agents.map((item) => item.agent);
  const projectRecords = data.project_summaries.length
    ? data.project_summaries
    : Array.from(new Set(data.tasks.map((detail) => detail.task.project || "unassigned"))).map((name) => ({ name }));

  return (
    <aside className="explorer-panel">
      <header className="explorer-header">
        <span>MAC / FLEET</span>
        <button aria-label="Explorer options" className="icon-button" type="button">
          <i className="codicon codicon-settings" />
        </button>
      </header>
      <button className="target-switcher" type="button">
        <i className="codicon codicon-globe" />
        <span>Primary hub</span>
        <i className="codicon codicon-chevron-down" />
      </button>

      <div className="explorer-search">
        <i className="codicon codicon-search" />
        <input
          aria-label="Filter tasks"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter work"
          value={query}
        />
      </div>

      <ExplorerSection count={projectRecords.length} label="Projects">
        {projectRecords.slice(0, 12).map((project) => {
          const name = projectName(project);
          const count = data.tasks.filter((detail) => (detail.task.project || "unassigned") === name).length;
          return (
            <div className="explorer-row" key={name}>
              <i className="codicon codicon-chevron-right dim" />
              <i className="codicon codicon-folder" />
              <span className="truncate">{name}</span>
              <span className="row-count">{count}</span>
            </div>
          );
        })}
      </ExplorerSection>

      <ExplorerSection count={tasks.length} label={activeView === "workflows" ? "Workflow work" : "Active work"}>
        <div className="section-toolbar">
          <select aria-label="Task state filter" onChange={(event) => setStateFilter(event.target.value)} value={stateFilter}>
            <option value="active">Active</option>
            <option value="all">All states</option>
            <option value="open">Open</option>
            <option value="running">Running</option>
            <option value="needs_review">Needs review</option>
            <option value="blocked">Blocked</option>
            <option value="completed">Completed</option>
          </select>
        </div>
        <div className="explorer-task-list">
          {tasks.slice(0, 40).map((task) => (
            <button
              className={`explorer-row task-row ${task.id === selectedTaskId ? "selected" : ""}`}
              key={task.id}
              onClick={() => onSelectTask(task.id)}
              title={taskLabel(task)}
              type="button"
            >
              <span className={`state-dot state-${task.state || "open"}`} />
              <span className="truncate">{taskLabel(task)}</span>
              <span className="row-count">P{task.priority ?? 0}</span>
            </button>
          ))}
        </div>
      </ExplorerSection>

      <ExplorerSection count={agents.length} label="Agent mesh">
        {agents.slice(0, 16).map((agent) => (
          <button
            className={`explorer-row agent-row ${agent.id === selectedAgentId ? "selected" : ""}`}
            key={agent.id}
            onClick={() => onSelectAgent(agent.id)}
            type="button"
          >
            <span className="agent-avatar small">{agentInitials(agent)}</span>
            <span className="truncate">{agent.name || agent.id.replace(/^agent_/, "")}</span>
            <span className={`presence ${agent.health_status === "healthy" ? "online" : "offline"}`} />
            <span className="protocol-mark">A2A</span>
          </button>
        ))}
      </ExplorerSection>
    </aside>
  );
}

function ExplorerSection({
  children,
  count,
  label,
}: {
  children: React.ReactNode;
  count: number;
  label: string;
}) {
  return (
    <section className="explorer-section">
      <div className="explorer-section-title">
        <span>{label}</span>
        <span>{count}</span>
      </div>
      {children}
    </section>
  );
}

export function findTaskDetail(data: DashboardState, taskId: string | null): TaskDetail | null {
  return data.tasks.find((detail) => detail.task.id === taskId) || null;
}
