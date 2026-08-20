import { expect, test } from "@playwright/test";

/**
 * Bulk actions over a group of tasks.
 *
 * The per-card answer form is correct and stays correct; it just cannot keep
 * up. The number of tasks parked on the same question is set by the fleet, so
 * a workflow costing one interaction per task loses by construction.
 *
 * What these pin is the safety shape, which is the same one the CLI has:
 * nothing happens until a preview has been taken, and the apply carries the
 * token of *what was previewed* -- so a group that changed membership without
 * changing size is refused rather than silently acted on.
 */

function parkedTask(id: string, question: string) {
  return {
    task: {
      id,
      title: `Parked ${id}`,
      project: "alpha",
      priority: 1,
      state: "needs_input",
      owner_agent_id: null,
      dependencies: [],
      required_capabilities: [],
      metadata: {
        needs_input: {
          schema: "mac.task_needs_input.v1",
          questions: [{ question, why: "", options: [] }],
          asked_by: "worker-1",
          asked_at: "2026-08-01T00:00:00+00:00",
          from_state: "running",
        },
      },
    },
    detail_loaded: false,
  };
}

function dashboardState() {
  const tasks = [
    parkedTask("task-a", "Which database?"),
    parkedTask("task-b", "Which database?"),
    parkedTask("task-c", "Which database?"),
  ];
  return {
    schema: "mac.dashboard_ide.v1",
    overview: {
      counts: { healthy_agents: 1, active_tasks: tasks.length },
      task_states: { needs_input: tasks.length },
      agent_statuses: { idle: 1 },
    },
    project_summaries: [{ name: "alpha", task_count: tasks.length }],
    agents: [],
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
    bus_streams: [],
    updated_at: new Date().toISOString(),
    session: { can_write: true },
  };
}

async function setupPage(
  page: import("@playwright/test").Page,
  options: { groups?: { name: string; expression: string }[] } = {},
) {
  await page.route("**/api/dashboard/state?view=ide", async (route) => {
    await route.fulfill({ json: dashboardState() });
  });
  await page.route("**/api/.well-known/agent-card.json", async (route) => {
    await route.fulfill({ json: { name: "mac", protocolVersion: "0.3.0" } });
  });
  await page.route("**/api/task-groups", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ json: options.groups || [] });
  });
  for (const path of [
    "**/api/communication/identities",
    "**/api/communication/accounts",
    "**/api/communication/representations",
    "**/api/communication/gateway-leases?active_only=true",
  ]) {
    await page.route(path, async (route) => {
      await route.fulfill({ json: [] });
    });
  }
  await page.route("**/api/dashboard/stream*", async (route) => {
    await route.fulfill({
      body: '{"event":"connected"}\n',
      contentType: "application/x-ndjson",
      status: 200,
    });
  });
}

async function gotoWorkView(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();
  await page.getByText("Work", { exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
}

test("previewing a group reports how many tasks it names", async ({ page }) => {
  await setupPage(page);
  await page.route("**/api/tasks/select", async (route) => {
    await route.fulfill({
      json: {
        matched: 3,
        token: "tok-abc",
        returned: 3,
        truncated: false,
        tasks: [
          { id: "task-a", title: "Parked task-a", project: "alpha", state: "needs_input", priority: 1, questions: ["Which database?"] },
        ],
      },
    });
  });
  await gotoWorkView(page);

  await page.locator("[data-batch-selector]").fill("state=needs_input project=alpha");
  await page.locator("[data-batch-preview]").click();

  const result = page.locator("[data-batch-preview-result]");
  await expect(result).toContainText("3");
  await expect(result).toContainText("Which database?");
});

test("nothing can be applied before a preview", async ({ page }) => {
  await setupPage(page);
  let applied = 0;
  await page.route("**/api/tasks/batch", async (route) => {
    applied += 1;
    await route.fulfill({ json: {} });
  });
  await gotoWorkView(page);

  // The apply control does not exist until a group has been previewed.
  await expect(page.locator("[data-batch-apply]")).toHaveCount(0);
  expect(applied).toBe(0);
});

test("applying answers the whole group against the previewed token", async ({ page }) => {
  await setupPage(page);
  const bodies: Array<Record<string, unknown>> = [];
  await page.route("**/api/tasks/select", async (route) => {
    await route.fulfill({
      json: { matched: 3, token: "tok-abc", returned: 3, truncated: false, tasks: [] },
    });
  });
  await page.route("**/api/tasks/batch", async (route) => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({
      json: {
        batch_id: "batch-1", selection_token: "tok-abc", operation: "answer",
        selector: "state=needs_input", applied: true, matched: 3,
        changed: ["task-a", "task-b", "task-c"], changed_count: 3,
        failed: [], failed_count: 0, truncated: false,
      },
    });
  });
  await gotoWorkView(page);

  await page.locator("[data-batch-preview]").click();
  await page.locator("[data-batch-answer]").fill("postgres, us-west");
  await page.locator("[data-batch-apply]").click();

  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({
    operation: "answer",
    apply: true,
    // The token is what makes this safe: the server refuses if the group is
    // no longer the one that was previewed.
    expect_token: "tok-abc",
    options: { answer: "postgres, us-west" },
  });
  await expect(page.locator("[data-batch-status]")).toContainText("answered 3");
});

test("a refused batch shows the reason instead of failing silently", async ({ page }) => {
  await setupPage(page);
  await page.route("**/api/tasks/select", async (route) => {
    await route.fulfill({
      json: { matched: 2, token: "stale", returned: 2, truncated: false, tasks: [] },
    });
  });
  await page.route("**/api/tasks/batch", async (route) => {
    await route.fulfill({
      status: 409,
      json: { detail: "the selected group is no longer the one previewed" },
    });
  });
  await gotoWorkView(page);

  await page.locator("[data-batch-preview]").click();
  await page.locator("[data-batch-answer]").fill("postgres");
  await page.locator("[data-batch-apply]").click();

  await expect(page.locator("[data-batch-error]")).toContainText("no longer the one previewed");
});

test("a saved group can be picked and becomes the selector", async ({ page }) => {
  await setupPage(page, {
    groups: [{ name: "parked-alpha", expression: "state=needs_input project=alpha" }],
  });
  await page.route("**/api/tasks/select", async (route) => {
    await route.fulfill({
      json: { matched: 3, token: "tok", returned: 3, truncated: false, tasks: [] },
    });
  });
  await gotoWorkView(page);

  await page.locator("[data-batch-group-pick]").selectOption("parked-alpha");

  await expect(page.locator("[data-batch-selector]")).toHaveValue("group=parked-alpha");
});
