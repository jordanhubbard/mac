/**
 * The console's central promise: it never renders a plausible-looking zero.
 *
 * These are render tests rather than logic tests because the failure mode they
 * guard is visual — a panel that *looks* like a healthy reading when the hub
 * could not be read at all.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Snapshot } from "../src/lib/api";
import { LiveView } from "../src/views/Live";
import { StuckView } from "../src/views/Stuck";
import { AgentsView } from "../src/views/Fleet";
import { Tile, Unavailable } from "../src/components/primitives";

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    schema: "mac.dashboard.observe.v1",
    server_time: "2026-08-17T12:00:00+00:00",
    window: {
      hours: 6,
      since: "2026-08-17T06:00:00+00:00",
      until: "2026-08-17T12:00:00+00:00",
    },
    observability_sequence: 42,
    build_ms: 31.2,
    degraded: [],
    ...overrides,
  };
}

describe("a missing section is stated, never zeroed", () => {
  it("says unavailable and explains that the number is unknown", () => {
    render(<Unavailable what="Flow" reason="StoreError: relation does not exist" />);
    expect(screen.getByText(/Flow unavailable/)).toBeTruthy();
    expect(screen.getByText(/unknown — not zero/)).toBeTruthy();
    expect(screen.getByText(/relation does not exist/)).toBeTruthy();
  });

  it("renders a tile with no value as 'unknown', not 0", () => {
    const { container } = render(<Tile label="landed" value={null} />);
    expect(container.textContent).toContain("unknown");
    expect(container.textContent).not.toContain("0");
  });

  it("renders a real zero as 0", () => {
    const { container } = render(<Tile label="landed" value={0} />);
    expect(container.textContent).toContain("0");
    expect(container.textContent).not.toContain("unknown");
  });

  it("the live view degrades per-section when the hub omits flow", () => {
    const { container } = render(
      <LiveView
        onOpenTask={() => undefined}
        snap={snapshot({
          degraded: [{ section: "flow", reason: "StoreError: timeout" }],
          tasks: {
            by_state: { blocked: 360, running: 2 },
            total: 362,
            live_total: 362,
            dwell_seconds: {
              blocked: { count: 360, p50: 90000, p90: 400000, max: 900000 },
              running: { count: 2, p50: 30, p90: 40, max: 40 },
            },
            undated_rows: 0,
          },
        })}
      />,
    );
    // The section that failed says so...
    expect(screen.getByText(/Flow unavailable/)).toBeTruthy();
    expect(screen.getByText(/StoreError: timeout/)).toBeTruthy();
    // ...and the tiles that depended on it report unknown rather than 0.
    expect(container.textContent).toContain("unknown");
    // ...while the section that succeeded still renders its real numbers.
    expect(container.textContent).toContain("360");
  });

  it("distinguishes 'no transitions happened' from 'transitions unavailable'", () => {
    render(
      <LiveView
        onOpenTask={() => undefined}
        snap={snapshot({
          transitions: [],
          flow: {
            bucket_seconds: 360,
            bucket_starts: ["2026-08-17T11:00:00+00:00"],
            series: {},
            dropped_rows: 0,
            total: 0,
          },
        })}
      />,
    );
    expect(screen.getByText(/No transitions in this window/)).toBeTruthy();
    expect(screen.queryByText(/Transitions unavailable/)).toBeNull();
  });
});

describe("dwell is reported as duration, and absence as absence", () => {
  it("shows p50/p90/max per state", () => {
    const { container } = render(
      <StuckView
        snap={snapshot({
          tasks: {
            by_state: { blocked: 360 },
            total: 360,
            live_total: 360,
            dwell_seconds: {
              blocked: { count: 360, p50: 86400 * 4, p90: 86400 * 9, max: 86400 * 11 },
            },
            undated_rows: 3,
          },
          stuck: [],
        })}
      />,
    );
    expect(container.textContent).toContain("4d0h");
    expect(container.textContent).toContain("9d0h");
    // The rows excluded from dwell are declared rather than silently dropped.
    expect(container.textContent).toMatch(/3 tasks have no readable updated_at/);
  });

  it("renders an empty dwell sample as a dash", () => {
    const { container } = render(
      <StuckView
        snap={snapshot({
          tasks: {
            by_state: { blocked: 1 },
            total: 1,
            live_total: 1,
            dwell_seconds: { blocked: { count: 0, p50: null, p90: null, max: null } },
            undated_rows: 0,
          },
        })}
      />,
    );
    expect(container.textContent).toContain("—");
  });
});

describe("the hub's belief about an agent is shown next to the evidence", () => {
  const agentSnapshot = snapshot({
    agents: {
      by_status: { busy: 1 },
      by_health: { healthy: 1 },
      total: 1,
      truncated: 0,
      rows: [
        {
          id: "agent_1",
          name: "ghost",
          status: "busy",
          health_status: "healthy",
          instance_kind: "static",
          current_task_id: null,
          last_seen_at: "2026-08-17T08:00:00+00:00",
          seconds_since_seen: 14400,
          open_tasks: 1,
          active_leases: 1,
          belief_contradicted: true,
          dispatch_hold: 0,
        },
      ],
    },
  });

  it("marks a status the evidence does not support", () => {
    const { container } = render(<AgentsView snap={agentSnapshot} />);
    expect(container.textContent).toContain("busy");
    expect(container.textContent).toContain("unverified");
    expect(screen.getByText(/status the evidence does not support/)).toBeTruthy();
    // The last-heard age is shown so the operator can judge for themselves.
    expect(container.textContent).toContain("4h00m");
  });

  it("says 'never' when an agent has no readable last_seen_at", () => {
    const snap = snapshot({
      agents: {
        ...agentSnapshot.agents!,
        rows: [
          {
            ...agentSnapshot.agents!.rows[0],
            seconds_since_seen: null,
            belief_contradicted: false,
          },
        ],
      },
    });
    const { container } = render(<AgentsView snap={snap} />);
    expect(container.textContent).toContain("never");
    expect(container.textContent).toContain("no readable last_seen_at");
  });
});
