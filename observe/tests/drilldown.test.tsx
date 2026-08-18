/**
 * The drill-down's honesty contract.
 *
 * This data is PARTIAL and the UI has to look partial. On this fleet only ~2%
 * of tasks have any transcript, `coding_agent`/`model` were empty on every
 * historical row, and `command_audit` records only the harness's own spawns —
 * not what the coding CLI ran inside its sandbox. Each of those gaps has a
 * failure mode where a blank panel reads as "the agent did nothing", and each
 * one is asserted here.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConsoleClient, type Snapshot, type TaskDrilldown } from "../src/lib/api";
import { TaskView } from "../src/views/Task";

function snapshot(coverage?: Snapshot["transcripts"]): Snapshot {
  return {
    schema: "mac.dashboard.observe.v1",
    server_time: "2026-08-17T12:00:00+00:00",
    window: {
      hours: 6,
      since: "2026-08-17T06:00:00+00:00",
      until: "2026-08-17T12:00:00+00:00",
    },
    observability_sequence: 1,
    build_ms: 12,
    degraded: [],
    transcripts: coverage,
  };
}

const COVERAGE: Snapshot["transcripts"] = {
  rows_total: 275,
  tasks_with_transcript: 188,
  tasks_total: 8000,
  coverage_fraction: 188 / 8000,
  attributed_rows: 0,
  unattributed_rows: 275,
  commands_audited: 40,
};

function drilldown(overrides: Partial<TaskDrilldown> = {}): TaskDrilldown {
  return {
    schema: "mac.dashboard.observe.task.v1",
    server_time: "2026-08-17T12:00:00+00:00",
    task_id: "task_1",
    found: true,
    build_ms: 8,
    degraded: [],
    task: {
      id: "task_1",
      title: "wire the thing",
      description: "",
      state: "blocked",
      project: "mac",
      priority: 0,
      owner_agent_id: null,
      lease_id: null,
      leased_until: null,
      attempt_count: 1,
      max_attempts: 3,
      started_at: null,
      completed_at: null,
      created_at: "2026-08-10T12:00:00+00:00",
      updated_at: "2026-08-13T12:00:00+00:00",
      created_by_human: null,
      dwell_seconds: 4 * 86400,
      age_seconds: 7 * 86400,
    },
    history: [],
    transcripts: {
      rows: [],
      count: 0,
      attributed: 0,
      unattributed: 0,
      truncated_list: false,
    },
    commands: [],
    evidence: [],
    reviews: [],
    publications: [],
    ...overrides,
  };
}

/** A ConsoleClient whose network layer is a stub; nothing real is fetched. */
function stubClient(
  task: TaskDrilldown,
  transcript?: Record<string, unknown>,
): ConsoleClient {
  const fetchImpl = vi.fn(async (path: string) => {
    const body = path.includes("/transcripts/") ? (transcript ?? {}) : task;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  return new ConsoleClient(() => "", fetchImpl as unknown as typeof fetch);
}

const noop = () => undefined;

describe("a task with no transcript is not a task that did nothing", () => {
  it("says the transcript was never recorded, and how rare recording is", async () => {
    const client = stubClient(drilldown());
    render(
      <TaskView
        client={client}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/No transcript was recorded/)).toBeTruthy(),
    );
    expect(
      screen.getByText(/gap in recording, not evidence that the agent did nothing/),
    ).toBeTruthy();
    // The fleet-wide fraction is on screen so the operator can calibrate.
    expect(screen.getByText(/2\.4% of tasks/)).toBeTruthy();
    expect(screen.getByText(/188.*of.*8,000/)).toBeTruthy();
  });

  it("admits when even the coverage fraction is unknown", async () => {
    const client = stubClient(drilldown());
    render(
      <TaskView client={client} taskId="task_1" snap={snapshot()} onBack={noop} />,
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Fleet-wide transcript coverage is unknown/),
      ).toBeTruthy(),
    );
  });

  it("distinguishes a failed read from an empty one", async () => {
    const client = stubClient(
      drilldown({
        transcripts: undefined,
        degraded: [{ section: "transcripts", reason: "StoreError: no such table" }],
      }),
    );
    render(
      <TaskView
        client={client}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Transcript unavailable/)).toBeTruthy(),
    );
    expect(screen.getByText(/no such table/)).toBeTruthy();
    // ...and it must NOT claim nothing was recorded, which is a different fact.
    expect(screen.queryByText(/No transcript was recorded/)).toBeNull();
  });
});

describe("missing attribution is labelled, never left blank", () => {
  const withTurn = drilldown({
    transcripts: {
      count: 1,
      attributed: 0,
      unattributed: 1,
      truncated_list: false,
      rows: [
        {
          id: "tr_1",
          sequence: 0,
          agent_id: "agent_1",
          command_id: "cmd_1",
          coding_agent: null,
          model: null,
          returncode: 0,
          duration_ms: 4210,
          truncated: false,
          started_at: null,
          completed_at: null,
          compression: "zlib",
          payload_bytes: 14400,
          has_payload: true,
          metadata: "{}",
          created_at: "2026-08-13T12:00:00+00:00",
        },
      ],
    },
  });

  it("renders 'unattributed' rather than an empty cell", async () => {
    const client = stubClient(withTurn);
    render(
      <TaskView
        client={client}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("unattributed")).toBeTruthy());
    // The panel header counts them, so the gap is visible without expanding.
    expect(screen.getByText(/1 unattributed/)).toBeTruthy();
  });

  it("names the CLI when it IS recorded", async () => {
    const attributed = drilldown({
      transcripts: {
        ...withTurn.transcripts!,
        attributed: 1,
        unattributed: 0,
        rows: [
          {
            ...withTurn.transcripts!.rows[0],
            coding_agent: "claude-code",
            model: "claude-opus-5",
          },
        ],
      },
    });
    render(
      <TaskView
        client={stubClient(attributed)}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("claude-code")).toBeTruthy());
    expect(screen.queryByText("unattributed")).toBeNull();
  });

  it("expands a turn and marks a clipped payload as a prefix", async () => {
    const client = stubClient(withTurn, {
      schema: "mac.dashboard.observe.transcript.v1",
      transcript_id: "tr_1",
      found: true,
      prompt: { text: "do the work", clipped: false, full_length: 11 },
      response: { text: "x".repeat(50), clipped: true, full_length: 900000 },
      stderr: { text: "", clipped: false, full_length: 0 },
    });
    render(
      <TaskView
        client={client}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText(/^#0$/)).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    await waitFor(() => expect(screen.getByText("do the work")).toBeTruthy());
    expect(screen.getByText(/This is a prefix, not the whole/)).toBeTruthy();
    expect(screen.getByText(/900,000 characters/)).toBeTruthy();
  });

  it("distinguishes an empty payload from a missing transcript", async () => {
    const empty = drilldown({
      transcripts: {
        ...withTurn.transcripts!,
        rows: [
          { ...withTurn.transcripts!.rows[0], has_payload: false, payload_bytes: 0 },
        ],
      },
    });
    const client = stubClient(empty, {
      schema: "mac.dashboard.observe.transcript.v1",
      transcript_id: "tr_1",
      found: true,
      prompt: { text: "", clipped: false, full_length: 0 },
      response: { text: "", clipped: false, full_length: 0 },
      stderr: { text: "", clipped: false, full_length: 0 },
    });
    render(
      <TaskView
        client={client}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("empty")).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    await waitFor(() =>
      expect(
        screen.getByText(/recorded with an empty payload/),
      ).toBeTruthy(),
    );
    expect(
      screen.getByText(/different from the task having no transcript at all/),
    ).toBeTruthy();
  });
});

describe("command_audit is labelled as incomplete by construction", () => {
  it("warns that the sandbox's own commands are not captured", async () => {
    render(
      <TaskView
        client={stubClient(drilldown())}
        taskId="task_1"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/This is not everything that ran/)).toBeTruthy(),
    );
    expect(
      screen.getByText(/executed inside its\s+sandbox is not captured/),
    ).toBeTruthy();
    expect(
      screen.getByText(/not "nothing ran"/),
    ).toBeTruthy();
  });
});

describe("an unknown task id is answered, not errored", () => {
  it("says there is no such task and does not imply the hub is down", async () => {
    const client = stubClient({
      schema: "mac.dashboard.observe.task.v1",
      server_time: "2026-08-17T12:00:00+00:00",
      task_id: "task_ghost",
      found: false,
      build_ms: 1,
      degraded: [],
    });
    render(
      <TaskView
        client={client}
        taskId="task_ghost"
        snap={snapshot(COVERAGE)}
        onBack={noop}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/No task with id task_ghost/)).toBeTruthy(),
    );
    expect(screen.getByText(/not "the hub is down"/)).toBeTruthy();
  });
});
