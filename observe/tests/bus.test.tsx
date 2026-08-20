/**
 * The Bus view's contract.
 *
 * This view replaced a pseudo-terminal, and the reason it is allowed to exist
 * where the PTY is not is a claim about what it does: it OBSERVES a
 * conversation. Three properties carry that claim, and each is asserted here.
 *
 * 1. It joins the bus AS AN AGENT, because the read endpoints are self-only.
 *    It asks the hub which agent its token is rather than guessing, and when
 *    the token is not an agent's it says so plainly instead of rendering an
 *    empty conversation that looks like a quiet fleet.
 *
 * 2. It renders addressing as addressing. `addressed_to` says who is expected
 *    to answer, by convention; it is NOT privacy, and a view that implied
 *    otherwise would misdescribe the bus it is showing.
 *
 * 3. It never writes. There is no compose box in phase 1, and the read-only
 *    guarantee in `readonly.test.ts` still covers the whole source tree,
 *    including this view.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import {
  ConsoleClient,
  type BusIdentity,
  type BusMessage,
  type BusRollCall,
} from "../src/lib/api";
import { BusView } from "../src/views/Bus";

const AGENT = "agent_operator";

function identity(overrides: Partial<BusIdentity> = {}): BusIdentity {
  return {
    schema: "mac.agentbus.identity.v1",
    agent_id: AGENT,
    joined: true,
    reason: "",
    ...overrides,
  };
}

function message(overrides: Partial<BusMessage> = {}): BusMessage {
  return {
    cursor: "2026-08-20T12:00:00+00:00|chunk_1",
    topic: "content",
    from_agent_id: "agent_rocky",
    addressed_to: [],
    addressed_to_me: false,
    reply_expected: false,
    chunk: {
      id: "chunk_1",
      stream_id: "stream_1",
      sequence: 1,
      sender_agent_id: "agent_rocky",
      created_at: "2026-08-20T12:00:00+00:00",
      payload: { text: "rebasing onto main before I push" },
    },
    ...overrides,
  };
}

function rollCall(overrides: Partial<BusRollCall> = {}): BusRollCall {
  return {
    schema: "mac.agentbus.roll_call.v1",
    counted_at: "2026-08-20T12:00:05+00:00",
    agent_count: 1,
    agents: [
      {
        id: "agent_rocky",
        name: "rocky",
        capabilities: ["python", "review"],
        status: "busy",
        health_status: "healthy",
        current_task_id: "task_abc",
      },
    ],
    ...overrides,
  };
}

/**
 * A client whose three bus reads are stubbed. Traffic is served once and then
 * exhausted, mirroring the cursor: a message is delivered a single time.
 */
function client(options: {
  identity?: BusIdentity;
  traffic?: BusMessage[];
  roster?: BusRollCall;
  identityError?: Error;
  trafficError?: Error;
}): ConsoleClient {
  const stub = new ConsoleClient(() => "tok", async () => new Response("{}"));
  let served = false;
  vi.spyOn(stub, "busIdentity").mockImplementation(async () => {
    if (options.identityError) throw options.identityError;
    return options.identity ?? identity();
  });
  vi.spyOn(stub, "busTraffic").mockImplementation(async () => {
    if (options.trafficError) throw options.trafficError;
    if (served) return [];
    served = true;
    return options.traffic ?? [];
  });
  vi.spyOn(stub, "busRollCall").mockImplementation(async () => {
    return options.roster ?? rollCall();
  });
  return stub;
}

describe("joining the bus", () => {
  it("asks the hub which agent it is rather than guessing", async () => {
    const stub = client({ traffic: [message()] });
    render(<BusView client={stub} />);

    await waitFor(() => expect(stub.busIdentity).toHaveBeenCalled());
    await waitFor(() =>
      expect(stub.busTraffic).toHaveBeenCalledWith(AGENT, ""),
    );
    // The roll call is read as the same agent: both endpoints are self-only.
    expect(stub.busRollCall).toHaveBeenCalledWith(AGENT);
  });

  it("says the token is not an agent instead of showing an empty bus", async () => {
    const stub = client({
      identity: identity({
        agent_id: null,
        joined: false,
        reason: "this token is not bound to an agent",
      }),
    });
    render(<BusView client={stub} />);

    await waitFor(() =>
      expect(screen.getByText(/not on the bus/i)).toBeTruthy(),
    );
    expect(screen.getByText(/not bound to an agent/i)).toBeTruthy();
    // A view that cannot join must not pretend to poll one.
    expect(stub.busTraffic).not.toHaveBeenCalled();
  });

  it("treats an unreachable identity endpoint as not-joined, not as silence", async () => {
    const stub = client({ identityError: new Error("cannot reach hub") });
    render(<BusView client={stub} />);

    await waitFor(() =>
      expect(screen.getByText(/not on the bus/i)).toBeTruthy(),
    );
    expect(screen.getByText(/cannot reach hub/i)).toBeTruthy();
  });
});

describe("rendering what is being said", () => {
  it("shows the speaker and the message", async () => {
    const stub = client({ traffic: [message()] });
    render(<BusView client={stub} />);

    await waitFor(() =>
      expect(
        screen.getByText(/rebasing onto main before I push/),
      ).toBeTruthy(),
    );
    expect(screen.getByText(/agent_rocky/)).toBeTruthy();
  });

  it("renders addressing as addressing, not as privacy", async () => {
    const stub = client({
      traffic: [
        message({
          cursor: "c2",
          addressed_to: ["agent_natasha"],
          addressed_to_me: false,
        }),
      ],
    });
    render(<BusView client={stub} />);

    // "→ names" — who is expected to answer. Nothing claims the message is
    // hidden from anyone, because on this bus it is not.
    await waitFor(() =>
      expect(screen.getByText(/→ agent_natasha/)).toBeTruthy(),
    );
    expect(screen.queryByText(/private/i)).toBeNull();
  });

  it("marks a message addressed to this console as expecting a reply", async () => {
    const stub = client({
      traffic: [
        message({
          cursor: "c3",
          addressed_to: [AGENT],
          addressed_to_me: true,
          reply_expected: true,
        }),
      ],
    });
    render(<BusView client={stub} />);

    await waitFor(() =>
      expect(screen.getByText(/reply expected/i)).toBeTruthy(),
    );
  });

  it("an unaddressed message is shown as spoken to everyone", async () => {
    const stub = client({ traffic: [message({ cursor: "c4" })] });
    render(<BusView client={stub} />);

    await waitFor(() => expect(screen.getByText(/→ everyone/)).toBeTruthy());
  });

  it("says a silent bus is silent rather than showing a blank panel", async () => {
    const stub = client({ traffic: [] });
    render(<BusView client={stub} />);

    await waitFor(() =>
      expect(screen.getByText(/The bus is silent/)).toBeTruthy(),
    );
  });

  it("keeps showing what it heard when a later read fails, and says so", async () => {
    const stub = new ConsoleClient(() => "tok", async () => new Response("{}"));
    vi.spyOn(stub, "busIdentity").mockResolvedValue(identity());
    vi.spyOn(stub, "busRollCall").mockResolvedValue(rollCall());
    let call = 0;
    vi.spyOn(stub, "busTraffic").mockImplementation(async () => {
      call += 1;
      if (call === 1) return [message()];
      throw new Error("503 hub unavailable");
    });

    render(<BusView client={stub} />);
    await waitFor(() =>
      expect(screen.getByText(/rebasing onto main/)).toBeTruthy(),
    );
    await waitFor(
      () => expect(screen.getByText(/last read of the bus failed/i)).toBeTruthy(),
      { timeout: 8000 },
    );
    // The heard message survives the failed read; it is not replaced by zero.
    expect(screen.getByText(/rebasing onto main/)).toBeTruthy();
  }, 12_000);
});

describe("who is on the bus", () => {
  it("lists present agents with what each can do", async () => {
    const stub = client({ traffic: [] });
    render(<BusView client={stub} />);

    await waitFor(() => expect(screen.getByText("rocky")).toBeTruthy());
    // Capabilities are the point of a roll call: knowing who is there is only
    // useful alongside what they can take.
    expect(screen.getByText(/python · review/)).toBeTruthy();
  });

  it("says so when an agent declares no capabilities", async () => {
    const stub = client({
      traffic: [],
      roster: rollCall({
        agents: [
          {
            id: "agent_operator",
            name: "operator",
            capabilities: [],
            status: "idle",
            health_status: "healthy",
            current_task_id: null,
          },
        ],
      }),
    });
    render(<BusView client={stub} />);

    await waitFor(() =>
      expect(screen.getByText(/no capabilities declared/)).toBeTruthy(),
    );
  });
});
