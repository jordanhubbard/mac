/**
 * The merge queue view.
 *
 * The queue exists because this repository has repeatedly produced gates that
 * reported healthy while enforcing nothing, and a queue nobody can watch is
 * the next one. So the properties tested here are the ones that would let this
 * view become that: an unreadable section must not render as an empty queue, a
 * never-sized window must not render as a floor, and a landed change must not
 * be counted as still waiting.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { MergeQueueRow, Snapshot } from "../src/lib/api";
import { MergeQueueView } from "../src/views/MergeQueue";

function row(over: Partial<MergeQueueRow> = {}): MergeQueueRow {
  return {
    repository: "jordanhubbard/mac",
    branch: "main",
    depth: 2,
    by_state: { queued: 2 },
    window_size: 4,
    landed_count: 10,
    failure_count: 1,
    speculation_discarded: 0,
    last_event: "landed",
    updated_at: "2026-08-18T06:00:00+00:00",
    ...over,
  };
}

function snapshot(mq?: Snapshot["merge_queue"], degraded: Snapshot["degraded"] = []): Snapshot {
  return {
    schema: "mac.dashboard.observe.v1",
    server_time: "2026-08-18T06:00:00+00:00",
    window: {
      hours: 6,
      since: "2026-08-18T00:00:00+00:00",
      until: "2026-08-18T06:00:00+00:00",
    },
    observability_sequence: 1,
    build_ms: 4.2,
    degraded,
    merge_queue: mq,
  };
}

function section(rows: MergeQueueRow[], over: Partial<NonNullable<Snapshot["merge_queue"]>> = {}) {
  return {
    queues: rows,
    queue_count: rows.length,
    total_depth: rows.reduce((n, r) => n + r.depth, 0),
    total_landed: rows.reduce((n, r) => n + r.landed_count, 0),
    total_failed: rows.reduce((n, r) => n + r.failure_count, 0),
    recent_evictions: [],
    live_states: ["queued", "testing", "tested"],
    ...over,
  };
}

describe("merge queue view", () => {
  it("says the section is unavailable rather than showing an empty queue", () => {
    render(
      <MergeQueueView
        snap={snapshot(undefined, [
          { section: "merge_queue", reason: "StoreError: relation does not exist" },
        ])}
      />,
    );

    expect(screen.getByText(/relation does not exist/)).toBeTruthy();
    // The word that would be a lie: a depth reading of any kind.
    expect(screen.queryByText("waiting to land")).toBeNull();
  });

  it("distinguishes an empty queue from an unreadable one", () => {
    render(<MergeQueueView snap={snapshot(section([]))} />);

    expect(screen.getByText(/No queue has been opened/)).toBeTruthy();
    expect(screen.getByText("queues")).toBeTruthy();
  });

  it("renders a never-sized window as unknown, not as the floor", () => {
    // Values chosen so nothing else in the row is "1": a loose text query would
    // otherwise pass on some unrelated cell and assert nothing.
    const { container } = render(
      <MergeQueueView
        snap={snapshot(
          section([
            row({ window_size: null, depth: 3, landed_count: 10, failure_count: 2 }),
          ]),
        )}
      />,
    );
    const cells = Array.from(container.querySelectorAll("tbody tr td"));
    const windowCell = cells[3]; // repository, branch, depth, WINDOW

    // "never speculated" and "backed all the way off to the floor of 1" are
    // different facts about the queue and must not render identically.
    expect(windowCell.textContent).toBe("—");
  });

  it("has no land rate before anything has been attempted", () => {
    render(
      <MergeQueueView
        snap={snapshot(section([row({ landed_count: 0, failure_count: 0 })]))}
      />,
    );

    // 0% would read as total failure; the truth is that nothing has run.
    expect(screen.queryByText("0%")).toBeNull();
  });

  it("computes the land rate from attempts, not from queue depth", () => {
    render(
      <MergeQueueView
        snap={snapshot(section([row({ landed_count: 3, failure_count: 1, depth: 99 })]))}
      />,
    );

    expect(screen.getByText("75%")).toBeTruthy();
  });

  it("shows the eviction reason, which is why a change did not land", () => {
    render(
      <MergeQueueView
        snap={snapshot(
          section([row()], {
            recent_evictions: [
              {
                repository: "jordanhubbard/mac",
                branch: "main",
                task_id: "task_1",
                pull_request_number: 406,
                eviction_reason: "tests_failed_on_speculative_base",
                updated_at: "2026-08-18T05:00:00+00:00",
              },
            ],
          }),
        )}
      />,
    );

    expect(screen.getByText("tests_failed_on_speculative_base")).toBeTruthy();
    expect(screen.getByText("406")).toBeTruthy();
  });

  it("says an empty eviction list means nothing was turned away", () => {
    render(<MergeQueueView snap={snapshot(section([row()]))} />);

    expect(screen.getByText(/Nothing has been evicted/)).toBeTruthy();
  });

  it("lists every queue it is given", () => {
    render(
      <MergeQueueView
        snap={snapshot(
          section([
            row({ repository: "a/one" }),
            row({ repository: "b/two", branch: "release" }),
          ]),
        )}
      />,
    );

    expect(screen.getByText("a/one")).toBeTruthy();
    expect(screen.getByText("b/two")).toBeTruthy();
    expect(screen.getByText("release")).toBeTruthy();
  });
});
