import { expect, test } from "@playwright/test";

function task(id: string, state: string) {
  return {
    task: {
      id,
      title: `${state} task ${id}`,
      project: "mac",
      priority: 1,
      state,
      owner_agent_id: null,
      dependencies: [],
      required_capabilities: [],
    },
    detail_loaded: false,
  };
}

function dashboardState() {
  const tasks = [
    ...Array.from({ length: 400 }, (_, index) => task(`completed-${index}`, "completed")),
    ...Array.from({ length: 40 }, (_, index) => task(`blocked-${index}`, "blocked")),
  ];
  return {
    schema: "mac.dashboard_ide.v1",
    overview: {
      // The hub verifier is healthy too, but it is a logical control-plane
      // identity rather than a physical fleet node.
      counts: { healthy_agents: 2, active_tasks: 40 },
      task_states: { completed: 400, blocked: 40 },
      agent_statuses: { idle: 1 },
    },
    project_summaries: [{ name: "mac", task_count: tasks.length }],
    agents: [{
      agent: {
        id: "agent-test",
        name: "test-agent",
        status: "idle",
        health_status: "healthy",
        capabilities: ["testing"],
        resources: { hardware: { cpu_count: 8, memory_mb: 16_384, arch: "arm64" } },
      },
      machine: null,
      availability: { eligible: true, reasons: [] },
    }, {
      agent: {
        id: "agent-hub-reviewer",
        name: "hub-reviewer",
        status: "idle",
        health_status: "healthy",
        capabilities: ["review"],
        resources: { virtual: true, hub_review_verifier: { enabled: true } },
      },
      machine: null,
      availability: { eligible: false, reasons: ["logical service"] },
    }],
    tasks,
    fleets: [], workflows: [], workflow_drafts: [], workflow_runs: {}, events: [],
    messages: [], notifications: [], observability: {}, action_events: [], command_audit: [],
    runtimes: [], runtime_deltas: [], runtime_runs: [], rollouts: [], secrets: [],
    secret_audits: [], service_links: [], integration_findings: [], artifacts: [],
    terminal_sessions: [], updated_at: new Date().toISOString(), session: { can_write: true },
  };
}

test("Fleet IDE coalesces refreshes and bounds Kanban rendering", async ({ page }) => {
  let dashboardRequests = 0;
  let agentCardRequests = 0;
  let detailRequests = 0;
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));

  await page.route("**/api/dashboard/state?view=ide", async (route) => {
    dashboardRequests += 1;
    await route.fulfill({ json: dashboardState() });
  });
  await page.route("**/api/.well-known/agent-card.json", async (route) => {
    agentCardRequests += 1;
    await route.fulfill({ json: { name: "mac", protocolVersion: "0.3.0" } });
  });
  await page.route("**/api/tasks/*?view=compact", async (route) => {
    detailRequests += 1;
    const id = new URL(route.request().url()).pathname.split("/").pop() || "unknown";
    await route.fulfill({
      json: {
        task: { ...task(id, id.split("-")[0]).task, description: "hydrated detail" },
        detail_loaded: true,
        evidence: [], history: [], reviews: [], publications: [],
      },
    });
  });
  await page.route("**/api/dashboard/stream*", async (route) => {
    await route.fulfill({
      body: '{"event":"connected"}\n{"event":"updated"}\n',
      contentType: "application/x-ndjson",
      status: 200,
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Fleet cockpit" })).toBeVisible();
  await expect(page.getByText("Hub online")).toBeVisible();
  await expect(page.getByText("1 agents", { exact: true })).toBeVisible();
  await expect(page.getByText("hub-reviewer", { exact: true })).toHaveCount(0);
  await page.waitForTimeout(6_200);

  expect(dashboardRequests).toBe(2);
  expect(agentCardRequests).toBe(1);
  expect(detailRequests).toBe(1);
  expect(consoleErrors).toEqual([]);

  await page.getByText("Work", { exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
  expect(await page.locator(".kanban-card").count()).toBe(60);

  await page.getByPlaceholder("Search task, project, or state").fill("blocked");
  await expect(page.getByText("40 tasks", { exact: true })).toBeVisible();
  expect(await page.locator(".kanban-card").count()).toBe(30);
  await page.getByRole("button", { name: /Show 10 more/ }).click();
  expect(await page.locator(".kanban-card").count()).toBe(40);
});
