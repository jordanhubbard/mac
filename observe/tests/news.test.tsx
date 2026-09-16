import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { ConsoleClient, NewsFeed } from "../src/lib/api";
import { NewsView } from "../src/views/News";

const FEED: NewsFeed = {
  schema: "mac.news.v1",
  server_time: "2026-09-01T12:00:00+00:00",
  cursor: 12,
  items: [
    {
      sequence: 12,
      created_at: "2026-09-01T12:00:00+00:00",
      kind: "task",
      event_type: "task.transitioned",
      actor: "agent_worker",
      summary: "agent_worker moved Ship board from claimed to running",
      task_id: "task_1",
      task_title: "Ship board",
      project: "mac",
      from_state: "claimed",
      to_state: "running",
      failure_class: "hub_verification_error",
      attempt_refunded: true,
      agent_id: null,
      agent_name: null,
      previous_status: null,
      status: null,
      previous_health_status: null,
      health_status: null,
      changed_fields: [],
    },
    {
      sequence: 11,
      created_at: "2026-09-01T11:59:00+00:00",
      kind: "agent",
      event_type: "agent.heartbeat_updated",
      actor: "agent_worker",
      summary: "agent_worker changed worker from idle to busy",
      task_id: null,
      task_title: null,
      project: null,
      from_state: null,
      to_state: null,
      failure_class: null,
      attempt_refunded: false,
      agent_id: "agent_worker",
      agent_name: "worker",
      previous_status: "idle",
      status: "busy",
      previous_health_status: "healthy",
      health_status: "healthy",
      changed_fields: ["status"],
    },
  ],
};

describe("news board", () => {
  it("shows task movement and agent status with attribution", async () => {
    const client = { news: async () => FEED } as unknown as ConsoleClient;
    render(<NewsView client={client} refreshKey={1} onOpenTask={() => undefined} />);

    await waitFor(() => expect(screen.getByText("Ship board")).toBeTruthy());
    expect(screen.getByText("worker")).toBeTruthy();
    expect(screen.getAllByText("agent_worker").length).toBeGreaterThan(0);
    expect(screen.getByText("running")).toBeTruthy();
    expect(screen.getByText("busy")).toBeTruthy();
    expect(screen.getByText(/hub_verification_error.*attempt refunded/)).toBeTruthy();
  });

  it("labels a failed feed instead of pretending it is empty", async () => {
    const client = { news: async () => { throw new Error("hub unavailable"); } } as unknown as ConsoleClient;
    render(<NewsView client={client} refreshKey={1} onOpenTask={() => undefined} />);

    await waitFor(() => expect(screen.getByText(/hub unavailable/)).toBeTruthy());
    expect(screen.queryByText(/No significant activity/)).toBeNull();
  });
});
