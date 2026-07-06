import { useDeferredValue, useMemo, useState } from "react";
import type { Agent, DashboardState, Task, TaskDetail } from "../api/mac";
import type { WorkbenchView } from "./ActivityRail";
import { buildProjectCounts } from "./projectScope";

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
  selectedProjectId,
  onSelectTask,
  onSelectAgent,
  onSelectProject,
}: {
  data: DashboardState;
  activeView: WorkbenchView;
  selectedTaskId: string | null;
  selectedAgentId: string | null;
  /** null = "All projects" */
  selectedProjectId: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectAgent: (agentId: string) => void;
  onSelectProject: (projectId: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("active");
  /** Set of project names whose task children are expanded in the tree. */
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());

  const projectRecords = useMemo(
    () =>
      data.project_summaries.length
        ? data.project_summaries
        : Array.from(
            new Set(data.tasks.map((detail) => detail.task.project || "unassigned")),
          ).map((name) => ({ name })),
    [data.project_summaries, data.tasks],
  );

  // Build authoritative counts in one memoized pass
  const projectCounts = useMemo(
    () => buildProjectCounts(data.tasks, data.project_summaries),
    [data.tasks, data.project_summaries],
  );

  // Index once instead of filtering the complete task list for every project
  // on every render. Expanded projects then read their children in O(1).
  const tasksByProject = useMemo(() => {
    const grouped = new Map<string, TaskDetail[]>();
    for (const detail of data.tasks) {
      const name = detail.task.project || "unassigned";
      const entries = grouped.get(name);
      if (entries) entries.push(detail);
      else grouped.set(name, [detail]);
    }
    for (const entries of grouped.values()) {
      entries.sort(
        (left, right) =>
          (Number(right.task.priority || 0) - Number(left.task.priority || 0)) ||
          taskLabel(left.task).localeCompare(taskLabel(right.task)),
      );
    }
    return grouped;
  }, [data.tasks]);

  // Tasks visible in the explorer list (filtered by state, query, and project)
  const tasks = useMemo(() => {
    const all = data.tasks.map((detail) => detail.task);
    return all
      .filter((task) => {
        if (
          stateFilter === "active" &&
          ["completed", "cancelled", "failed"].includes(String(task.state))
        )
          return false;
        if (stateFilter !== "active" && stateFilter !== "all" && task.state !== stateFilter)
          return false;
        if (selectedProjectId && (task.project || "unassigned") !== selectedProjectId) return false;
        return (
          !deferredQuery ||
          [task.title, task.description, task.project, task.id].some((value) =>
            String(value || "")
              .toLowerCase()
              .includes(deferredQuery),
          )
        );
      })
      .sort(
        (left, right) =>
          (Number(right.priority || 0) - Number(left.priority || 0)) ||
          taskLabel(left).localeCompare(taskLabel(right)),
      );
  }, [data.tasks, deferredQuery, stateFilter, selectedProjectId]);

  const agents = data.agents.map((item) => item.agent);

  function toggleExpand(name: string) {
    setExpandedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  }

  function handleProjectKeyDown(event: React.KeyboardEvent, name: string) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelectProject(selectedProjectId === name ? null : name);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setExpandedProjects((prev) => new Set([...prev, name]));
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      setExpandedProjects((prev) => {
        const next = new Set(prev);
        next.delete(name);
        return next;
      });
    }
  }

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
        {/* "All projects" action */}
        <button
          aria-pressed={selectedProjectId === null}
          className={`explorer-row project-row all-projects ${selectedProjectId === null ? "selected" : ""}`}
          onClick={() => onSelectProject(null)}
          type="button"
        >
          <i className="codicon codicon-home dim" />
          <span className="truncate">All projects</span>
        </button>

        <div aria-label="Projects" className="project-tree" role="tree">
          {projectRecords.map((project) => {
            const name = projectName(project);
            const count = projectCounts.get(name) ?? 0;
            const isSelected = selectedProjectId === name;
            const isExpanded = expandedProjects.has(name);
            const projectTasks = tasksByProject.get(name) ?? [];

            return (
              <div key={name} role="none">
              <div
                aria-expanded={isExpanded}
                aria-label={`${name} project`}
                aria-selected={isSelected}
                className={`explorer-row project-row ${isSelected ? "selected" : ""}`}
                onKeyDown={(event) => handleProjectKeyDown(event, name)}
                role="treeitem"
                tabIndex={0}
              >
                {/* Chevron: toggles expand/collapse independently */}
                <button
                  aria-label={isExpanded ? `Collapse ${name}` : `Expand ${name}`}
                  className="chevron-button"
                  onClick={(event) => {
                    event.stopPropagation();
                    toggleExpand(name);
                  }}
                  tabIndex={-1}
                  type="button"
                >
                  <i
                    className={`codicon ${isExpanded ? "codicon-chevron-down" : "codicon-chevron-right"}`}
                  />
                </button>
                {/* Project label: selects the project */}
                <button
                  aria-label={name}
                  aria-pressed={isSelected}
                  className="project-label-button"
                  onClick={() => onSelectProject(isSelected ? null : name)}
                  tabIndex={-1}
                  type="button"
                >
                  <i className="codicon codicon-folder" />
                  <span className="truncate">{name}</span>
                </button>
                <span className="row-count">{count}</span>
              </div>

              {/* Expanded task children */}
              {isExpanded && (
                <div className="project-children" role="group">
                  {projectTasks.length ? (
                    projectTasks.map(({ task }) => (
                      <button
                        className={`explorer-row task-row child-row ${task.id === selectedTaskId ? "selected" : ""}`}
                        key={task.id}
                        onClick={() => {
                          onSelectTask(task.id);
                        }}
                        title={taskLabel(task)}
                        type="button"
                      >
                        <span className={`state-dot state-${task.state || "open"}`} />
                        <span className="truncate">{taskLabel(task)}</span>
                      </button>
                    ))
                  ) : (
                    <span className="explorer-row child-row empty-project">No tasks</span>
                  )}
                </div>
              )}
              </div>
            );
          })}
        </div>
      </ExplorerSection>

      <ExplorerSection
        count={tasks.length}
        label={activeView === "workflows" ? "Workflow work" : "Active work"}
      >
        <div className="section-toolbar">
          <select
            aria-label="Task state filter"
            onChange={(event) => setStateFilter(event.target.value)}
            value={stateFilter}
          >
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
            <span
              className={`presence ${agent.health_status === "healthy" ? "online" : "offline"}`}
            />
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
