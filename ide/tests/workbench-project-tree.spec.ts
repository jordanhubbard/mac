import { expect, test } from "@playwright/test";

// ─── shared test fixtures ────────────────────────────────────────────────────

function makeTask(
  id: string,
  state: string,
  project = "alpha",
  priority = 1,
  title?: string,
) {
  return {
    task: {
      id,
      title: title ?? `${state} task ${id}`,
      project,
      priority,
      state,
      owner_agent_id: null,
      dependencies: [],
      required_capabilities: [],
    },
    detail_loaded: false,
  };
}

function dashboardState(overrides?: {
  extraTasks?: ReturnType<typeof makeTask>[];
  projectSummaries?: Array<Record<string, unknown>>;
}) {
  const baseTasks = [
    makeTask("alpha-open-1", "open", "alpha", 2, "Open alpha one"),
    makeTask("alpha-open-2", "open", "alpha", 1, "Open alpha two"),
    makeTask("alpha-blocked-1", "blocked", "alpha", 1, "Blocked alpha"),
    makeTask("beta-open-1", "open", "beta", 1, "Open beta one"),
    makeTask("beta-completed-1", "completed", "beta", 0, "Completed beta"),
    makeTask("gamma-open-1", "open", "gamma", 1, "Open gamma one"),
  ];
  const tasks = [...baseTasks, ...(overrides?.extraTasks ?? [])];
  const defaultSummaries = [
    { name: "alpha", task_count: 3 },
    { name: "beta", task_count: 2 },
    { name: "gamma", task_count: 1 },
  ];
  const projectSummaries = overrides?.projectSummaries ?? defaultSummaries;
  return {
    schema: "mac.dashboard_ide.v1",
    overview: {
      counts: { healthy_agents: 1, active_tasks: tasks.length },
      task_states: { open: 4, blocked: 1, completed: 1 },
      agent_statuses: { idle: 1 },
    },
    project_summaries: projectSummaries,
    agents: [
      {
        agent: {
          id: "agent-test",
          name: "test-agent",
          status: "idle",
          health_status: "healthy",
          capabilities: ["testing"],
          resources: { hardware: { cpu_count: 4, memory_mb: 8192, arch: "x64" } },
        },
        machine: null,
        availability: { eligible: true, reasons: [] },
      },
    ],
    tasks,
    fleets: [],
    workflows: [],
    workflow_drafts: [],
    workflow_runs: {},
    events: [],
    messages: [],
    notifications: [],
    observability: {},
    action_events: [],
    command_audit: [],
    runtimes: [],
    runtime_deltas: [],
    runtime_runs: [],
    rollouts: [],
    secrets: [],
    secret_audits: [],
    service_links: [],
    integration_findings: [],
    artifacts: [],
    terminal_sessions: [],
    updated_at: new Date().toISOString(),
    session: { can_write: true },
  };
}

async function setupPage(
  page: import("@playwright/test").Page,
  stateOverrides?: Parameters<typeof dashboardState>[0],
) {
  const state = dashboardState(stateOverrides);
  await page.route("**/api/dashboard/state?view=ide", async (route) => {
    await route.fulfill({ json: state });
  });
  await page.route("**/api/.well-known/agent-card.json", async (route) => {
    await route.fulfill({ json: { name: "mac", protocolVersion: "0.3.0" } });
  });
  await page.route("**/api/tasks/*?view=compact", async (route) => {
    await route.fulfill({
      json: {
        task: { id: "unknown", title: "detail", state: "open", project: "alpha" },
        detail_loaded: true,
        evidence: [],
        history: [],
        reviews: [],
        publications: [],
      },
    });
  });
  await page.route("**/api/dashboard/stream*", async (route) => {
    await route.fulfill({
      body: '{"event":"connected"}\n',
      contentType: "application/x-ndjson",
      status: 200,
    });
  });
  return state;
}

// ─── mouse selection ─────────────────────────────────────────────────────────

test("clicking a project label selects that project", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Click the "alpha" project label button
  await page.getByRole("button", { name: "alpha", exact: true }).click();

  // The project row should show selected state
  const alphaRow = page.locator(".project-row", { hasText: "alpha" }).first();
  await expect(alphaRow).toHaveClass(/selected/);
  await expect(page.getByTestId("rf__node-alpha-open-1")).toHaveCount(1);
  await expect(page.getByTestId("rf__node-beta-open-1")).toHaveCount(0);
});

test("clicking All projects clears project selection", async ({ page }) => {
  await setupPage(page);
  await page.goto("/?project=alpha");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // All projects button should not be selected initially
  const allProjectsButton = page.getByRole("button", { name: "All projects" });
  await expect(allProjectsButton).toHaveAttribute("aria-pressed", "false");

  // Click All projects
  await allProjectsButton.click();
  await expect(allProjectsButton).toHaveAttribute("aria-pressed", "true");
});

// ─── keyboard selection ──────────────────────────────────────────────────────

test("Enter key on project treeitem selects and deselects it", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  const alphaItem = page.getByRole("treeitem", { name: "alpha project" });
  await alphaItem.focus();
  await alphaItem.press("Enter");
  await expect(alphaItem).toHaveAttribute("aria-selected", "true");

  // Press Enter again to deselect
  await alphaItem.press("Enter");
  await expect(alphaItem).toHaveAttribute("aria-selected", "false");
});

test("Space key on project treeitem toggles selection", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  const betaItem = page.getByRole("treeitem", { name: "beta project" });
  await betaItem.focus();
  await betaItem.press(" ");
  await expect(betaItem).toHaveAttribute("aria-selected", "true");
});

// ─── expand / collapse ───────────────────────────────────────────────────────

test("chevron button expands project children", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Expand alpha via its chevron
  await page.getByRole("button", { name: "Expand alpha" }).click();

  // Task children should be visible
  const children = page.locator(".project-children");
  await expect(children.getByRole("button", { name: "Open alpha one", exact: true })).toBeVisible();
  await expect(children.getByRole("button", { name: "Blocked alpha", exact: true })).toBeVisible();
});

test("chevron button collapses project children", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Expand then collapse
  await page.getByRole("button", { name: "Expand alpha" }).click();
  const child = page
    .locator(".project-children")
    .getByRole("button", { name: "Open alpha one", exact: true });
  await expect(child).toBeVisible();

  await page.getByRole("button", { name: "Collapse alpha" }).click();
  await expect(child).not.toBeVisible();
});

test("ArrowRight key expands project, ArrowLeft collapses it", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  const gammaItem = page.getByRole("treeitem", { name: "gamma project" });
  await gammaItem.focus();

  // Expand with ArrowRight
  await gammaItem.press("ArrowRight");
  await expect(gammaItem).toHaveAttribute("aria-expanded", "true");
  const child = page
    .locator(".project-children")
    .getByRole("button", { name: "Open gamma one", exact: true });
  await expect(child).toBeVisible();

  // Collapse with ArrowLeft
  await gammaItem.press("ArrowLeft");
  await expect(gammaItem).toHaveAttribute("aria-expanded", "false");
  await expect(child).not.toBeVisible();
});

// ─── aria-expanded truthfulness ──────────────────────────────────────────────

test("aria-expanded reflects actual expand state", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  const alphaItem = page.getByRole("treeitem", { name: "alpha project" });
  await expect(alphaItem).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Expand alpha" }).click();
  await expect(alphaItem).toHaveAttribute("aria-expanded", "true");
});

// ─── empty projects ──────────────────────────────────────────────────────────

test("expanding an empty project shows 'No tasks'", async ({ page }) => {
  await setupPage(page, {
    extraTasks: [],
    projectSummaries: [
      { name: "alpha", task_count: 3 },
      { name: "beta", task_count: 2 },
      { name: "gamma", task_count: 1 },
      { name: "empty-project", task_count: 0 },
    ],
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  await page.getByRole("button", { name: "Expand empty-project" }).click();
  await expect(page.locator(".empty-project")).toBeVisible();
});

test("the project tree does not silently truncate projects or expanded tasks", async ({ page }) => {
  const projectSummaries = Array.from({ length: 13 }, (_, index) => ({
    name: `project-${index + 1}`,
    task_count: index === 12 ? 21 : 0,
  }));
  const extraTasks = Array.from({ length: 21 }, (_, index) =>
    makeTask(
      `project-13-task-${index + 1}`,
      "open",
      "project-13",
      1,
      `Project 13 task ${index + 1}`,
    ),
  );
  await setupPage(page, { extraTasks, projectSummaries });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  await expect(page.getByRole("button", { name: "project-13", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Expand project-13" }).click();
  await expect(
    page
      .locator(".project-children")
      .getByRole("button", { name: "Project 13 task 21", exact: true }),
  ).toBeVisible();
});

// ─── URL restoration / navigation ────────────────────────────────────────────

test("project selection is reflected in URL as ?project=", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  await page.getByRole("button", { name: "beta", exact: true }).click();

  const url = new URL(page.url());
  expect(url.searchParams.get("project")).toBe("beta");
});

test("project restored from URL on page load", async ({ page }) => {
  await setupPage(page);
  await page.goto("/?project=beta");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  const betaItem = page.getByRole("treeitem", { name: "beta project" });
  await expect(betaItem).toHaveAttribute("aria-selected", "true");
});

test("browser back/forward navigate project selection", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Select alpha
  await page.getByRole("button", { name: "alpha", exact: true }).click();
  expect(new URL(page.url()).searchParams.get("project")).toBe("alpha");

  // Select beta
  await page.getByRole("button", { name: "beta", exact: true }).click();
  expect(new URL(page.url()).searchParams.get("project")).toBe("beta");

  // Go back: should be alpha
  await page.goBack();
  expect(new URL(page.url()).searchParams.get("project")).toBe("alpha");

  // Go forward: should be beta
  await page.goForward();
  expect(new URL(page.url()).searchParams.get("project")).toBe("beta");
});

// ─── task child navigation ────────────────────────────────────────────────────

test("clicking a task child in the expanded tree selects that task", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Expand alpha and click a child task
  await page.getByRole("button", { name: "Expand alpha" }).click();
  await page
    .locator(".project-children")
    .getByRole("button", { name: "Open alpha one", exact: true })
    .click();

  // Task should be selected (visible as selected in the explorer task list or kanban)
  const taskRow = page.locator(".task-row.child-row.selected");
  await expect(taskRow).toBeVisible();
});

// ─── filter intersection with project selection ───────────────────────────────

test("text filter in explorer applies within selected project scope", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Select alpha project
  await page.getByRole("button", { name: "alpha", exact: true }).click();

  // Now type a filter in the explorer search
  await page.getByRole("textbox", { name: "Filter tasks" }).fill("blocked");

  // Only alpha+blocked tasks should show in "Active work"
  await expect(page.locator(".explorer-task-list").getByText("Blocked alpha")).toBeVisible();
  // Beta tasks should not appear in the explorer task list
  await expect(page.locator(".explorer-task-list").getByText("Open beta one")).not.toBeVisible();
});

// ─── Work view Kanban project filter ─────────────────────────────────────────

test("clicking a project then navigating to Work filters Kanban cards", async ({ page }) => {
  await setupPage(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // Select "beta" project
  await page.getByRole("button", { name: "beta", exact: true }).click();

  // Navigate to Work view
  await page.getByText("Work", { exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Work", exact: true })).toBeVisible();

  // Should see beta task count badge in the toolbar
  await expect(page.getByText(/tasks in beta/)).toBeVisible();

  // Count visible kanban cards: only beta tasks
  const cards = page.locator(".kanban-card");
  const count = await cards.count();
  // 2 beta tasks total
  expect(count).toBeLessThanOrEqual(2);
  expect(count).toBeGreaterThan(0);
});

// ─── authoritative project counts ────────────────────────────────────────────

test("project count badge uses authoritative task_count from project_summaries", async ({
  page,
}) => {
  // alpha has 3 tasks in summaries but we only load 2 in tasks array (simulate truncated list)
  await setupPage(page, {
    projectSummaries: [
      { name: "alpha", task_count: 99 },
      { name: "beta", task_count: 2 },
      { name: "gamma", task_count: 1 },
    ],
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();

  // The count shown next to "alpha" should reflect the authoritative total (99), not the truncated task list count
  const alphaRow = page.locator(".project-row", { hasText: "alpha" }).first();
  await expect(alphaRow.locator(".row-count")).toHaveText("99");
});
