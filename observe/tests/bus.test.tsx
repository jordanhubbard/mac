/**
 * The Bus view — phase 1, read only.
 *
 * Two things are being protected here. The first is the console's honesty
 * rule: "this credential cannot hear the bus" and "the bus is quiet" are
 * opposite facts and must never render as the same empty panel. The second is
 * the identity boundary: the view reads as whoever the HUB says it is, and
 * never as an agent id it made up — a console that could name its own actor
 * would be the widening of `assert_actor` that this design refuses.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type {
  BusIdentity,
  BusTrafficEntry,
  ConsoleClient,
  RollCall,
} from "../src/lib/api";
import { HubError } from "../src/lib/http";
import { BusView, MAX_MESSAGES, mergeRows, payloadText, toRow } from "../src/views/Bus";

function entry(over: Partial<BusTrafficEntry> = {}): BusTrafficEntry {
  return {
    chunk: {
      id: over.chunk?.id ?? "chunk-1",
      stream_id: "stream-1",
      sequence: 1,
      sender_agent_id: "agent_rocky",
      content_type: "application/json",
      payload: { note: "picking up task_1" },
      payload_encoding: "json",
      size_bytes: 42,
      created_at: "2026-08-20T10:15:00+00:00",
      ...(over.chunk ?? {}),
    },
    cursor: over.cursor ?? "2026-08-20T10:15:00+00:00::chunk-1",
    topic: "coordination",
    from_agent_id: "agent_rocky",
    addressed_to: [],
    addressed_to_me: false,
    reply_expected: false,
    ...over,
  };
}

function identity(over: Partial<BusIdentity> = {}): BusIdentity {
  return {
    schema: "mac.agentbus.identity.v1",
    agent_id: "agent_console",
    bus_participant: true,
    human_id: null,
    principal_kind: "agent",
    reason: "",
    ...over,
  };
}

function rollCall(over: Partial<RollCall> = {}): RollCall {
  return {
    schema: "mac.agentbus.roll_call.v1",
    counted_at: "2026-08-20T10:15:00+00:00",
    agent_count: 1,
    agents: [
      {
        id: "agent_rocky",
        name: "rocky",
        capabilities: ["python", "repo"],
        status: "busy",
        health_status: "healthy",
        current_task_id: "task_1",
        last_seen_at: "2026-08-20T10:14:00+00:00",
      },
    ],
    ...over,
  };
}

function client(over: Partial<ConsoleClient> = {}): ConsoleClient {
  return {
    busIdentity: vi.fn(async () => identity()),
    busTraffic: vi.fn(async () => [] as BusTrafficEntry[]),
    busRollCall: vi.fn(async () => rollCall()),
    ...over,
  } as unknown as ConsoleClient;
}

describe("the console reads the bus as whoever the hub says it is", () => {
  it("asks the hub for its own bus identity and reads traffic as that agent", async () => {
    const busTraffic = vi.fn(async (_agentId: string, _afterCursor?: string) => [
      entry(),
    ]);
    render(<BusView client={client({ busTraffic } as Partial<ConsoleClient>)} />);

    await waitFor(() => expect(busTraffic).toHaveBeenCalled());
    expect(busTraffic.mock.calls[0][0]).toBe("agent_console");
    expect(await screen.findByText(/picking up task_1/)).toBeTruthy();
  });

  it("says the session is not on the bus rather than showing an empty feed", async () => {
    render(
      <BusView
        client={client({
          busIdentity: vi.fn(async () =>
            identity({
              agent_id: null,
              bus_participant: false,
              principal_kind: "client",
              reason: "this token is not bound to an agent",
            }),
          ),
        } as Partial<ConsoleClient>)}
      />,
    );

    expect(await screen.findByText(/not on the bus/)).toBeTruthy();
    expect(screen.getByText(/This is not an empty bus/)).toBeTruthy();
    // The hub's own reason, verbatim, rather than a message the console
    // invented about a refusal it did not make.
    expect(document.querySelector(".unknown-text")?.textContent).toBe(
      "this token is not bound to an agent",
    );
  });

  it("does not read traffic at all when it is not a participant", async () => {
    const busTraffic = vi.fn(async () => [] as BusTrafficEntry[]);
    render(
      <BusView
        client={client({
          busIdentity: vi.fn(async () =>
            identity({ agent_id: null, bus_participant: false, reason: "no binding" }),
          ),
          busTraffic,
        } as Partial<ConsoleClient>)}
      />,
    );

    await screen.findByText(/not on the bus/);
    expect(busTraffic).not.toHaveBeenCalled();
  });

  it("a quiet bus reads as quiet, and says so is a finding", async () => {
    render(<BusView client={client()} />);
    expect(await screen.findByText(/The bus is quiet/)).toBeTruthy();
  });

  it("renders the hub's refusal as 'not on the bus', not as a broken console", async () => {
    render(
      <BusView
        client={client({
          busIdentity: vi.fn(async () => {
            throw new HubError(403, "403 token lacks required scope: agent");
          }),
        } as Partial<ConsoleClient>)}
      />,
    );

    expect(await screen.findByText(/not on the bus/)).toBeTruthy();
    expect(document.querySelector(".unknown-text")?.textContent).toContain(
      "lacks required scope",
    );
  });

  it("an unreadable bus is stated, never rendered as silence", async () => {
    render(
      <BusView
        client={client({
          busIdentity: vi.fn(async () => {
            throw new Error("403 token missing scope");
          }),
        } as Partial<ConsoleClient>)}
      />,
    );

    expect(await screen.findByText(/Cannot read the bus/)).toBeTruthy();
    expect(screen.getByText(/not a silent fleet/)).toBeTruthy();
  });

  it("keeps the traffic that read when the roll call fails", async () => {
    render(
      <BusView
        client={client({
          busTraffic: vi.fn(async () => [entry()]),
          busRollCall: vi.fn(async () => {
            throw new Error("StoreError: timeout");
          }),
        } as Partial<ConsoleClient>)}
      />,
    );

    expect(await screen.findByText(/Roll call unavailable/)).toBeTruthy();
    expect(screen.getByText(/unknown — not empty/)).toBeTruthy();
    expect(screen.getByText(/picking up task_1/)).toBeTruthy();
  });
});

describe("addressing is shown, because it is what makes silence correct", () => {
  it("names who is expected to answer", async () => {
    render(
      <BusView
        client={client({
          busTraffic: vi.fn(async () => [
            entry({ addressed_to: ["agent_bullwinkle"], addressed_to_me: false }),
          ]),
        } as Partial<ConsoleClient>)}
      />,
    );

    expect(await screen.findByText("agent_bullwinkle")).toBeTruthy();
    expect(screen.getByText(/^to$/)).toBeTruthy();
  });

  it("says 'to the fleet' when nobody in particular was addressed", async () => {
    render(
      <BusView client={client({ busTraffic: vi.fn(async () => [entry()]) } as Partial<ConsoleClient>)} />,
    );
    expect(await screen.findByText(/to the fleet/)).toBeTruthy();
  });
});

describe("the window is bounded, and says what it dropped", () => {
  it("keeps the newest MAX_MESSAGES and reports the overflow", () => {
    const rows = Array.from({ length: MAX_MESSAGES }, (_, i) =>
      toRow(entry({ chunk: { id: `old-${i}` } as BusTrafficEntry["chunk"] })),
    );
    const merged = mergeRows(rows, [
      toRow(entry({ chunk: { id: "new" } as BusTrafficEntry["chunk"] })),
    ]);

    expect(merged.rows).toHaveLength(MAX_MESSAGES);
    expect(merged.rows[0].key).toBe("new");
    expect(merged.dropped).toBe(1);
  });

  it("does not re-add a row the resumed cursor delivered twice", () => {
    const first = toRow(entry());
    const merged = mergeRows([first], [toRow(entry())]);
    expect(merged.rows).toHaveLength(1);
    expect(merged.dropped).toBe(0);
  });

  it("renders an unserializable payload as a message that happened", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(payloadText(cyclic)).toContain("unserializable");
    expect(payloadText(null)).toBe("");
  });
});

describe("phase 1 lands alone: the bus view does not write", () => {
  const source = readFileSync(
    resolve(__dirname, "..", "src", "views", "Bus.tsx"),
    "utf8",
  );

  it("has no compose box", () => {
    expect(/<form|<textarea|<input/.test(source)).toBe(false);
  });

  it("names no mutating verb and no broadcast endpoint", () => {
    expect(/["'`](POST|PUT|PATCH|DELETE)["'`]/.test(source)).toBe(false);
    expect(source).not.toContain("/agentbus/broadcast");
  });
});
