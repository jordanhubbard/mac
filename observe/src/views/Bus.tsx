/**
 * The bus: what the fleet is saying to itself.
 *
 * This replaces the PTY terminal. The terminal tab was a shell into a host —
 * the last place the hub UI could COMMAND rather than observe, and #417
 * deleted it for that reason. What earns the screen instead is the
 * conversation the fleet is actually having: AgentBus is fleet-wide, every
 * agent hears all of it, and by convention nobody acts until addressed by
 * name. That is worth watching. A remote shell is not.
 *
 * PHASE 1 IS READ-ONLY, and deliberately so. There is no compose box here and
 * no mutating verb anywhere in this file — `tests/readonly.test.ts` asserts
 * that over the whole source tree, and it stays true. Speaking to the bus is
 * phase 2, gated on ADR 0025's stated exception; adding it here quietly is
 * exactly what that test exists to prevent.
 *
 * Honesty rules, same as every other view (ADR 0024 §4): a quiet bus and a
 * broken read are opposite facts and must never look alike, and the window is
 * bounded with what it dropped stated rather than silently truncated.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  BusIdentity,
  BusTrafficEntry,
  ConsoleClient,
  RollCall,
} from "../lib/api";
import { HubError } from "../lib/http";
import { Empty, Panel } from "../components/primitives";
import { UNKNOWN, clockTime, count, shortId } from "../lib/format";

/**
 * How many messages the view keeps. A busy fleet outruns anything a browser
 * should hold, so the window is bounded and the count of what fell out of it
 * is rendered — a silently truncated feed reads as "that is all there was".
 */
export const MAX_MESSAGES = 300;

const POLL_MS = 4000;

/** One thing said, flattened into the row the UI renders. */
export interface BusRow {
  key: string;
  at: string;
  from: string;
  topic: string;
  addressedTo: string[];
  addressedToMe: boolean;
  contentType: string;
  sizeBytes: number;
  body: string;
}

/**
 * Payload text for a row, bounded.
 *
 * Bus payloads are arbitrary JSON and some carry a whole evidence blob. The
 * row shows a bounded summary and the full text is one interaction away —
 * ADR 0018's progressive disclosure, for ADR 0024's reason: the common
 * question is answered by the summary.
 */
export function payloadText(payload: unknown): string {
  if (payload === null || payload === undefined) return "";
  if (typeof payload === "string") return payload;
  try {
    return JSON.stringify(payload);
  } catch {
    // A payload that will not serialize is still a message that happened;
    // saying so beats dropping the row.
    return "<unserializable payload>";
  }
}

export function toRow(entry: BusTrafficEntry): BusRow {
  const chunk = entry.chunk ?? ({} as BusTrafficEntry["chunk"]);
  return {
    key: chunk.id || entry.cursor,
    at: chunk.created_at || "",
    from: entry.from_agent_id || chunk.sender_agent_id || "",
    topic: entry.topic || "",
    addressedTo: entry.addressed_to ?? [],
    addressedToMe: Boolean(entry.addressed_to_me),
    contentType: chunk.content_type || "",
    sizeBytes: Number(chunk.size_bytes ?? 0),
    body: payloadText(chunk.payload),
  };
}

/**
 * Merge a new batch into the window, newest first, bounded.
 *
 * Returns the number of rows pushed out of the window so the view can say so.
 * De-duplicates on key because a resumed cursor can legitimately re-deliver
 * the row it resumed from.
 */
export function mergeRows(
  existing: BusRow[],
  incoming: BusRow[],
): { rows: BusRow[]; dropped: number } {
  if (!incoming.length) return { rows: existing, dropped: 0 };
  const seen = new Set(existing.map((row) => row.key));
  const fresh = incoming.filter((row) => !seen.has(row.key));
  const merged = [...fresh.slice().reverse(), ...existing];
  return {
    rows: merged.slice(0, MAX_MESSAGES),
    dropped: Math.max(0, merged.length - MAX_MESSAGES),
  };
}

type Phase = "connecting" | "reading" | "not-a-participant" | "error";

export function BusView({ client }: { client: ConsoleClient }) {
  const [identity, setIdentity] = useState<BusIdentity | null>(null);
  const [rollCall, setRollCall] = useState<RollCall | null>(null);
  const [rows, setRows] = useState<BusRow[]>([]);
  const [dropped, setDropped] = useState(0);
  const [error, setError] = useState("");
  const [refusal, setRefusal] = useState("");
  const [phase, setPhase] = useState<Phase>("connecting");
  const [readAt, setReadAt] = useState<string>("");
  const cursor = useRef("");
  const window = useRef<BusRow[]>([]);

  useEffect(() => {
    let live = true;
    client
      .busIdentity()
      .then((next) => {
        if (!live) return;
        setIdentity(next);
        setPhase(next.bus_participant ? "reading" : "not-a-participant");
      })
      .catch((err: unknown) => {
        if (!live) return;
        const detail = err instanceof Error ? err.message : String(err);
        // The hub refuses this question outright for a credential that is not
        // on the bus -- a token that cannot join has no business being told,
        // by name, who is. That refusal IS the answer, so it renders as "not a
        // participant" with the hub's own words, not as a broken console.
        if (err instanceof HubError && (err.status === 401 || err.status === 403)) {
          setIdentity(null);
          setRefusal(detail);
          setPhase("not-a-participant");
          return;
        }
        setError(detail);
        setPhase("error");
      });
    return () => {
      live = false;
    };
  }, [client]);

  const agentId = identity?.bus_participant ? identity.agent_id : null;

  const poll = useCallback(async () => {
    if (!agentId) return;
    try {
      const batch = await client.busTraffic(agentId, cursor.current);
      if (batch.length) cursor.current = batch[batch.length - 1].cursor;
      const merged = mergeRows(window.current, batch.map(toRow));
      window.current = merged.rows;
      setRows(merged.rows);
      if (merged.dropped) setDropped((total) => total + merged.dropped);
      setReadAt(new Date().toISOString());
      setError("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [agentId, client]);

  useEffect(() => {
    if (!agentId) return undefined;
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [agentId, poll]);

  useEffect(() => {
    if (!agentId) return undefined;
    let live = true;
    client
      .busRollCall(agentId)
      .then((next) => live && setRollCall(next))
      .catch((err: unknown) => {
        if (!live) return;
        // A failed roll call must not blank the traffic that did read; the
        // panel says it is unavailable and the messages stay.
        setRollCall(null);
        setError((current) => current || (err instanceof Error ? err.message : String(err)));
      });
    return () => {
      live = false;
    };
  }, [agentId, client]);

  if (phase === "connecting") {
    return <p className="empty">Asking the hub who this console is on the bus…</p>;
  }

  if (phase === "error") {
    return (
      <div className="banner critical">
        <span className="icon" aria-hidden="true">
          ▲
        </span>
        <span>
          <strong>Cannot read the bus.</strong> {error}. Nothing is shown below
          because there is nothing true to show — this is not a silent fleet.
        </span>
      </div>
    );
  }

  if (phase === "not-a-participant") {
    return <NotAParticipant reason={identity?.reason || refusal} />;
  }

  return (
    <>
      {error ? (
        <div className="banner">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>The latest read of the bus failed.</strong> {error}. Messages
            below are what had already arrived, not the current state.
          </span>
        </div>
      ) : null}

      <div className="bus-grid">
        <Panel
          title="Traffic"
          sub={
            <>
              as heard by <code>{identity?.agent_id}</code>
              {readAt ? ` · read ${clockTime(readAt)}` : ""}
            </>
          }
          wide
        >
          <Traffic rows={rows} dropped={dropped} self={identity?.agent_id ?? ""} />
        </Panel>

        <Panel
          title="Roll call"
          sub={rollCall ? `${count(rollCall.agent_count)} on the bus` : undefined}
        >
          <Roster rollCall={rollCall} />
        </Panel>
      </div>
    </>
  );
}

/**
 * The console holds a credential that is not on the bus.
 *
 * Stated, with the reason the hub gave, rather than rendered as an empty feed.
 * "You cannot read this" and "nobody is saying anything" are opposite facts.
 */
function NotAParticipant({ reason }: { reason: string }) {
  return (
    <div className="banner serious">
      <span className="icon" aria-hidden="true">
        !
      </span>
      <span>
        <strong>This session is not on the bus.</strong> The bus is read as a
        participant — every read binds the agent in the URL to the credential
        presented — so a token that is not bound to an agent cannot hear it.
        This is not an empty bus.
        {reason ? <div className="unknown-text">{reason}</div> : null}
      </span>
    </div>
  );
}

function Traffic({
  rows,
  dropped,
  self,
}: {
  rows: BusRow[];
  dropped: number;
  self: string;
}) {
  if (!rows.length) {
    return (
      <Empty>
        The bus is quiet — this read succeeded and found nothing said. A bus
        nothing writes to is a real finding, not a missing panel.
      </Empty>
    );
  }
  return (
    <>
      {dropped ? (
        <p className="micro unknown-text">
          {count(dropped)} older message{dropped === 1 ? "" : "s"} fell out of the{" "}
          {MAX_MESSAGES}-message window and are not shown.
        </p>
      ) : null}
      <div className="bus-lines">
        {rows.map((row) => (
          <Message key={row.key} row={row} self={self} />
        ))}
      </div>
    </>
  );
}

function Message({ row, self }: { row: BusRow; self: string }) {
  const [open, setOpen] = useState(false);
  const preview = row.body.length > 160 ? `${row.body.slice(0, 160)}…` : row.body;
  return (
    <div className={row.addressedToMe ? "bus-line addressed" : "bus-line"}>
      <time title={row.at || undefined}>{clockTime(row.at)}</time>
      <span className="bus-from" title={row.from}>
        {row.from || UNKNOWN}
      </span>
      <span className="bus-to">
        <Addressing to={row.addressedTo} self={self} />
      </span>
      <span className="bus-topic">{row.topic || UNKNOWN}</span>
      <span className="bus-body">
        {open ? row.body : preview}
        {row.body.length > 160 ? (
          <button className="ghost micro" type="button" onClick={() => setOpen(!open)}>
            {open ? "less" : "more"}
          </button>
        ) : null}
      </span>
    </div>
  );
}

/**
 * Who is expected to answer.
 *
 * This is the convention the whole bus rests on: point-to-point messages are
 * not private, and an agent does not act until it is addressed by name. So the
 * row shows the addressing rather than hiding it — an operator watching needs
 * to see that a question was asked of someone in particular and, therefore,
 * that everyone else is right not to have answered.
 */
function Addressing({ to, self }: { to: string[]; self: string }) {
  if (!to.length) return <span className="micro">to the fleet</span>;
  return (
    <span className="micro">
      to{" "}
      {to.map((name, index) => (
        <span key={name}>
          {index ? ", " : ""}
          <code className={name === self ? "bus-self" : undefined}>{name}</code>
        </span>
      ))}
    </span>
  );
}

function Roster({ rollCall }: { rollCall: RollCall | null }) {
  if (!rollCall) {
    return (
      <div className="banner serious">
        <span className="icon" aria-hidden="true">
          !
        </span>
        <span>
          <strong>Roll call unavailable.</strong> The hub did not answer who is
          on the bus, so the roster is unknown — not empty.
        </span>
      </div>
    );
  }
  if (!rollCall.agents.length) {
    return <Empty>The roll call succeeded and nobody answered: no agents are on the bus.</Empty>;
  }
  return (
    <div className="roster">
      {rollCall.agents.map((agent) => (
        <div className="roster-row" key={agent.id ?? agent.name ?? Math.random()}>
          <span className="roster-name">
            <strong>{agent.name || agent.id || UNKNOWN}</strong>
            <small>{shortId(agent.id)}</small>
          </span>
          <span className="roster-status">{agent.status || UNKNOWN}</span>
          <span className="roster-caps">
            {agent.capabilities?.length ? (
              agent.capabilities.map((capability) => (
                <span className="chip" key={capability}>
                  {capability}
                </span>
              ))
            ) : (
              <span className="micro unknown-text">no declared capabilities</span>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}
