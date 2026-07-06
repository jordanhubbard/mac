/**
 * Shared project-selection state helpers.
 *
 * Selected project is kept in the URL (?project=<name>) so reloads,
 * copy/paste, and browser back/forward all preserve the selection.
 * The sentinel value null / missing param means "All projects".
 */

export const ALL_PROJECTS = null;

/** Read the current project from the URL search params. */
export function projectFromUrl(): string | null {
  const value = new URL(window.location.href).searchParams.get("project");
  return value || null;
}

/** Persist selected project to URL without creating a history entry. */
export function pushProjectToUrl(projectId: string | null): void {
  const url = new URL(window.location.href);
  if (projectId) {
    url.searchParams.set("project", projectId);
  } else {
    url.searchParams.delete("project");
  }
  window.history.pushState({}, "", url.pathname + url.search + url.hash);
}

/** Replace (not push) selected project in URL — used during initial load. */
export function replaceProjectInUrl(projectId: string | null): void {
  const url = new URL(window.location.href);
  if (projectId) {
    url.searchParams.set("project", projectId);
  } else {
    url.searchParams.delete("project");
  }
  window.history.replaceState({}, "", url.pathname + url.search + url.hash);
}

/**
 * Derive canonical project name from a ProjectSummary-like record.
 * Mirrors the projectName helper in WorkbenchExplorer.
 */
export function canonicalProjectName(project: Record<string, unknown>): string {
  return String(project.name || project.project || project.id || "unassigned");
}

/**
 * Build a memoization-friendly project→count map from task details and
 * project summaries in one pass.
 *
 * Prefer the authoritative task_count from project_summaries when present;
 * fall back to counting tasks directly so the two values stay consistent.
 */
export function buildProjectCounts(
  tasks: Array<{ task: { project?: string } }>,
  projectSummaries: Array<Record<string, unknown>>,
): Map<string, number> {
  // First pass: count from the task list (always correct for filtered views)
  const fromTasks = new Map<string, number>();
  for (const { task } of tasks) {
    const key = task.project || "unassigned";
    fromTasks.set(key, (fromTasks.get(key) ?? 0) + 1);
  }

  // If authoritative summaries exist, use them for the total count but only
  // when the summary total >= the task-list count (the task list is a subset
  // of all tasks; the summary is the authoritative total).
  const counts = new Map<string, number>(fromTasks);
  for (const summary of projectSummaries) {
    const name = canonicalProjectName(summary);
    const authoritativeTotal =
      typeof summary.task_count === "number" ? summary.task_count : undefined;
    if (authoritativeTotal !== undefined) {
      const taskListCount = fromTasks.get(name) ?? 0;
      // Use the authoritative total (which covers states filtered out of the
      // current task view) but cap it at the task-list count when the summary
      // is stale/smaller.
      counts.set(name, Math.max(authoritativeTotal, taskListCount));
    }
  }
  return counts;
}
