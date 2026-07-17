import { expect, test } from "@playwright/test";

const SHA = "c".repeat(40);

function dashboardState() {
  return {
    schema: "mac.dashboard_ide.v1",
    overview: {
      counts: { healthy_agents: 0, active_tasks: 0 },
      task_states: {},
      agent_statuses: {},
    },
    project_summaries: [{ name: "mac", task_count: 0 }],
    agents: [], tasks: [], fleets: [], workflows: [], workflow_drafts: [],
    workflow_runs: {}, events: [], messages: [], notifications: [],
    observability: {}, action_events: [], command_audit: [], runtimes: [],
    runtime_deltas: [], runtime_runs: [], rollouts: [], secrets: [],
    secret_audits: [], service_links: [], integration_findings: [], artifacts: [],
    terminal_sessions: [], updated_at: new Date().toISOString(),
    session: { can_write: true, is_admin: true },
  };
}

function plan() {
  return {
    schema: "mac.work_package.plan.v1",
    package_id: "wp_ide",
    goal: "Ship the exact change",
    project: "mac",
    repository_id: "projectrepo_mac",
    planning_base_ref: "refs/heads/main",
    planning_base_sha: SHA,
    plan_generation: 1,
    nodes: [
      {
        node_key: "change",
        title: "Implement change",
        node_type: "mutation",
        depends_on: [],
        effects: { writes: ["src/mac/change.py"] },
        expected_outputs: ["candidate"],
        verification: { profile: "repository-default" },
        estimates: { confidence: "high" },
      },
      {
        node_key: "assemble",
        title: "Assemble",
        node_type: "integration",
        depends_on: ["change"],
        effects: { reads: ["src/mac/change.py"] },
        expected_outputs: ["tree"],
        verification: { profile: "integration-default" },
      },
      {
        node_key: "certify",
        title: "Certify",
        node_type: "certification",
        depends_on: ["assemble"],
        effects: { reads: ["src/mac/change.py"] },
        expected_outputs: ["certificate"],
        verification: { profile: "certification-default" },
      },
    ],
  };
}

test("Fleet IDE admits a lossless held plan and activates only after a fresh downstream gate", async ({ page }) => {
  let readinessChecks = 0;
  let activationPosts = 0;

  await page.route("**/api/dashboard/state?view=ide", (route) => route.fulfill({ json: dashboardState() }));
  await page.route("**/api/dashboard/stream*", (route) => route.fulfill({
    body: '{"event":"connected"}\n',
    contentType: "application/x-ndjson",
    status: 200,
  }));
  await page.route("**/api/.well-known/agent-card.json", (route) => route.fulfill({ json: { name: "mac" } }));
  await page.route("**/api/dashboard/workflow-plan/preview", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.mode).toBe("managed");
    expect(body.project).toBe("mac");
    const exactPlan = plan();
    await route.fulfill({
      json: {
        schema: "mac.dashboard.managed_work_plan.v1",
        mode: "managed",
        source: "model",
        package_id: "wp_ide",
        goal: exactPlan.goal,
        project: "mac",
        repository_id: "projectrepo_mac",
        planning_base_ref: exactPlan.planning_base_ref,
        planning_base_sha: SHA,
        plan_generation: 1,
        plan_digest: `sha256:${"d".repeat(64)}`,
        topological_order: ["change", "assemble", "certify"],
        nodes: exactPlan.nodes,
        plan: exactPlan,
        activation: {
          required: true,
          automatic: false,
          expected_plan_version: 1,
          expected_epoch: 1,
          endpoint: "/work-packages/wp_ide/activate",
        },
      },
    });
  });
  await page.route("**/api/dashboard/workflow-plan/accept", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.mode).toBe("managed");
    expect(body.plan.nodes[0].title).toBe("Implement operator-edited change");
    await route.fulfill({
      json: {
        schema: "mac.dashboard.managed_work_plan_accept.v1",
        mode: "managed",
        held: true,
        package: { id: "wp_ide", state: "admitted" },
        task_ids: ["task_change", "task_assemble", "task_certify"],
        activation: {
          required: true,
          automatic: false,
          expected_plan_version: 1,
          expected_epoch: 1,
          endpoint: "/work-packages/wp_ide/activate",
        },
      },
    });
  });
  await page.route("**/api/work-packages/wp_ide/activation-readiness", async (route) => {
    readinessChecks += 1;
    await route.fulfill({
      json: readinessChecks === 1
        ? { ready: false, code: "work_package_pipeline_disabled", reason: "Pipeline remains default-off" }
        : { ready: true, code: "ready", reason: "" },
    });
  });
  await page.route("**/api/work-packages/wp_ide/activate", async (route) => {
    activationPosts += 1;
    expect(route.request().postDataJSON()).toMatchObject({
      expected_plan_version: 1,
      expected_epoch: 1,
    });
    await route.fulfill({ json: { package: { id: "wp_ide", state: "active" } } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Workflow studio" })).toBeVisible();
  await page.getByPlaceholder(/Describe the desired outcome/).fill("Ship the exact change");
  await page.getByRole("button", { name: "Generate graph" }).click();
  await expect(page.getByText(`refs/heads/main @ ${SHA.slice(0, 12)}`)).toBeVisible();

  const editor = page.getByLabel("Exact editable plan JSON");
  const edited = JSON.parse(await editor.inputValue());
  edited.nodes[0].title = "Implement operator-edited change";
  await editor.fill(JSON.stringify(edited, null, 2));
  await page.getByRole("button", { name: "Admit held plan" }).click();

  await expect(page.getByText("Held work package wp_ide")).toBeVisible();
  await expect(page.getByText("Pipeline remains default-off")).toBeVisible();
  const activate = page.getByRole("button", { name: "Activate exact version" });
  await expect(activate).toBeDisabled();
  expect(activationPosts).toBe(0);

  await page.getByRole("button", { name: "Refresh readiness" }).click();
  await expect(page.getByText("Downstream pull gate is ready.")).toBeVisible();
  await expect(activate).toBeEnabled();
  await activate.click();
  await expect(page.getByText("Managed work package activated: wp_ide")).toBeVisible();
  await expect(page.getByText("Active work package wp_ide")).toBeVisible();
  await expect(page.getByRole("button", { name: "Activate exact version" })).toHaveCount(0);
  expect(readinessChecks).toBe(3);
  expect(activationPosts).toBe(1);
});
