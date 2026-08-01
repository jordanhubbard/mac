import { expect, test } from "@playwright/test";

/**
 * A task parked on a human question is the one card the fleet will never move
 * on its own: it is excluded from every sweeper, reaper, and dispatch pass.
 * The card therefore has to carry both the question and the way to answer it,
 * and Submit has to put the task back in the pending queue -- otherwise the
 * work waits forever, which is the failure the state was built to avoid.
 */

function parkedTask(id: string, questions: Array<{ question: string; why?: string }>) {
  return {
    task: {
      id,
      title: `Parked ${id}`,
      project: "alpha",
      priority: 1,
      state: "needs_input",
      description: "original description",
      owner_agent_id: null,
      dependencies: [],
      required_capabilities: [],
      metadata: {
        needs_input: {
          schema: "mac.task_needs_input.v1",
          questions: questions.map((entry) => ({
            question: entry.question,
            why: entry.why || "",
            options: [],
          })),
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
    parkedTask("task-parked-1", [
      { question: "Which database should this use?", why: "the spec names neither" },
      { question: "Which region?" },
    ]),
    {
      task: {
        id: "task-open-1",
        title: "Ordinary open task",
        project: "alpha",
        priority: 1,
        state: "open",
        owner_agent_id: null,
        dependencies: [],
        required_capabilities: [],
      },
      detail_loaded: false,
    },
  ];
  return {
    schema: "mac.dashboard_ide.v1",
    overview: {
      counts: { healthy_agents: 1, active_tasks: tasks.length },
      task_states: { needs_input: 1, open: 1 },
      agent_statuses: { idle: 1 },
    },
    project_summaries: [{ name: "alpha", task_count: 2 }],
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
    terminal_sessions: [],
    updated_at: new Date().toISOString(),
    session: { can_write: true },
  };
}

async function setupPage(page: import("@playwright/test").Page) {
  await page.route("**/api/dashboard/state?view=ide", async (route) => {
    await route.fulfill({ json: dashboardState() });
  });
  await page.route("**/api/.well-known/agent-card.json", async (route) => {
    await route.fulfill({ json: { name: "mac", protocolVersion: "0.3.0" } });
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

test("a parked task gets its own lane and shows the outstanding questions", async ({ page }) => {
  await setupPage(page);
  await gotoWorkView(page);

  await expect(page.locator(".kanban-lane.state-needs_input")).toBeVisible();
  const card = page.locator('.kanban-card[data-task-id="task-parked-1"]');
  await expect(card).toHaveClass(/state-needs_input/);
  await expect(card.getByText("Which database should this use?")).toBeVisible();
  await expect(card.getByText("the spec names neither")).toBeVisible();
  await expect(card.getByText("Which region?")).toBeVisible();

  // An ordinary task carries no answer form.
  await expect(
    page.locator('.kanban-card[data-task-id="task-open-1"] [data-needs-input-submit]'),
  ).toHaveCount(0);
});

test("submitting an answer returns the task to the pending queue", async ({ page }) => {
  await setupPage(page);
  const answered: Array<Record<string, unknown>> = [];
  await page.route("**/api/tasks/task-parked-1/answer", async (route) => {
    answered.push(route.request().postDataJSON());
    await route.fulfill({ json: { id: "task-parked-1", state: "open" } });
  });
  await gotoWorkView(page);

  const card = page.locator('.kanban-card[data-task-id="task-parked-1"]');
  await card.locator("[data-needs-input-answer]").fill("postgres, us-west");
  await card.locator("[data-needs-input-submit]").click();

  await expect.poll(() => answered.length).toBe(1);
  expect(answered[0]).toMatchObject({ answer: "postgres, us-west", actor: "human" });
});

test("submitting with no answer asks for one instead of requeuing", async ({ page }) => {
  await setupPage(page);
  let calls = 0;
  await page.route("**/api/tasks/task-parked-1/answer", async (route) => {
    calls += 1;
    await route.fulfill({ json: { id: "task-parked-1", state: "open" } });
  });
  await gotoWorkView(page);

  const card = page.locator('.kanban-card[data-task-id="task-parked-1"]');
  await card.locator("[data-needs-input-submit]").click();

  await expect(card.getByRole("alert")).toContainText("answer is required");
  expect(calls).toBe(0);
});

test("a revised description is saved before the task is requeued", async ({ page }) => {
  await setupPage(page);
  const order: string[] = [];
  await page.route("**/api/tasks/task-parked-1", async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    order.push("update");
    await route.fulfill({ json: { id: "task-parked-1", state: "needs_input" } });
  });
  await page.route("**/api/tasks/task-parked-1/answer", async (route) => {
    order.push("answer");
    await route.fulfill({ json: { id: "task-parked-1", state: "open" } });
  });
  await gotoWorkView(page);

  const card = page.locator('.kanban-card[data-task-id="task-parked-1"]');
  await card.getByText("Edit description").click();
  await card.locator("[data-needs-input-description]").fill("clarified description");
  await card.locator("[data-needs-input-answer]").fill("postgres");
  await card.locator("[data-needs-input-submit]").click();

  // Description first: a failure there must not leave the task requeued
  // against stale text.
  await expect.poll(() => order).toEqual(["update", "answer"]);
});
