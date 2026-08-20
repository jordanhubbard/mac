import { useEffect, useMemo, useRef, useState } from "react";
import type {
  BusIdentity,
  BusMessage,
  BusRollCall,
  BusRosterAgent,
  ConsoleClient,
} from "../lib/api";
import { Empty, Panel, Tile } from "../components/primitives";
import { UNKNOWN, clockTime, count, shortId } from "../lib/format";
import { agentStatusColor, healthColor } from "../lib/states";

/**
 * The bus, as a conversation rather than as four counters.
 *
 * AgentBus is a fleet-wide conversation any agent can join. It hears
 * everything being said, it can ask for a roll call of who is present and what
 * each can do, and by convention nobody answers until addressed by name. This
 * view is that, read-only: the traffic on the left, the roster on the right.
 *
 * TWO THINGS THIS VIEW REFUSES TO IMPLY.
 *
 * 1. That an addressed message is a private one. `addressed_to` is addressing,
 *    not access — the hub deliberately does not enforce it, because
 *    enforcement would stop an agent volunteering the one fact that keeps
 *    another from destroying work. It renders as "→ names", never as a lock,
 *    and the roll call is what tells you which names exist to be used.
 *
 * 2. That a silent bus is a healthy one. A publish that nobody consumed looks
 *    identical to a working fleet from the counters; here it looks like
 *    silence, and the panel says how long the silence has lasted.
 *
 * It is a READ. There is no compose box: this console issues GET and HEAD and
 * nothing else (`observe/tests/readonly.test.ts`), and saying something to the
 * bus is a write. That decision is ADR 0025's, not this file's.
 */

/** How often to ask for new traffic. The bus is conversational, not real-time. */
const POLL_MS = 4000;

/**
 * Messages kept in the view. A conversation you cannot scroll back through is
 * not much of a conversation, but an unbounded list is a leak in a tab left
 * open for a week.
 */
const KEEP = 300;

export function BusView({ client }: { client: ConsoleClient }) {
  const [identity, setIdentity] = useState<BusIdentity | null>(null);
  const [messages, setMessages] = useState<BusMessage[]>([]);
  const [rollCall, setRollCall] = useState<BusRollCall | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [firstReadDone, setFirstReadDone] = useState(false);
  const cursor = useRef("");

  // Identity first, and separately: every other read needs the agent id, and
  // "this token is not an agent" is a different answer from "the bus is down".
  useEffect(() => {
    let live = true;
    client
      .busIdentity()
      .then((value) => live && setIdentity(value))
      .catch(
        (err: unknown) =>
          live &&
          setIdentity({
            schema: "",
            agent_id: null,
            joined: false,
            reason: err instanceof Error ? err.message : String(err),
          }),
      );
    return () => {
      live = false;
    };
  }, [client]);

  const agentId = identity?.joined ? identity.agent_id : null;

  useEffect(() => {
    if (!agentId) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const round = async () => {
      try {
        const [traffic, roster] = await Promise.all([
          client.busTraffic(agentId, cursor.current),
          client.busRollCall(agentId),
        ]);
        if (!live) return;
        if (traffic.length) {
          cursor.current = traffic[traffic.length - 1].cursor;
          // Newest first, bounded. The cursor means each message arrives once,
          // so this appends rather than replacing a re-fetched window.
          setMessages((prev) => [...traffic.reverse(), ...prev].slice(0, KEEP));
        }
        setRollCall(roster);
        setError(null);
      } catch (err: unknown) {
        if (live) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (live) {
          setFirstReadDone(true);
          timer = setTimeout(round, POLL_MS);
        }
      }
    };
    void round();

    return () => {
      live = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [client, agentId]);

  if (identity && !identity.joined) {
    return <NotOnTheBus identity={identity} />;
  }

  const addressedToMe = messages.filter((m) => m.addressed_to_me).length;
  const speakers = new Set(messages.map((m) => m.from_agent_id)).size;

  return (
    <>
      <div className="tiles">
        <Tile
          label="messages heard"
          value={messages.length}
          accent="var(--series-1)"
          note={messages.length >= KEEP ? `most recent ${KEEP}` : undefined}
        />
        <Tile label="agents speaking" value={speakers} accent="var(--series-2)" />
        <Tile
          label="addressed to this console"
          value={addressedToMe}
          accent="var(--series-4)"
          note="by convention, only the named answer"
        />
        <Tile
          label="on the bus"
          value={rollCall ? rollCall.agent_count : null}
          accent="var(--series-3)"
        />
      </div>

      {error ? (
        <div className="banner">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>The last read of the bus failed.</strong> {error}. What is
            below is what was heard before that — the bus may have moved on.
          </span>
        </div>
      ) : null}

      <div className="grid">
        <Panel
          title="Traffic"
          wide
          accent="var(--series-1)"
          sub={
            identity?.agent_id
              ? `heard as ${shortId(identity.agent_id)}`
              : undefined
          }
        >
          {messages.length === 0 ? (
            firstReadDone ? (
              <Empty>
                The bus is silent. That is a finding, not an empty panel — a
                fleet coordinating over the bus is a fleet that talks on it.
              </Empty>
            ) : (
              <Empty>Joining the bus…</Empty>
            )
          ) : (
            <ol className="bus-traffic">
              {messages.map((message) => (
                <Message key={message.cursor} message={message} />
              ))}
            </ol>
          )}
        </Panel>

        <Panel
          title="Roll call"
          accent="var(--series-3)"
          sub={rollCall ? `as of ${clockTime(rollCall.counted_at)}` : undefined}
        >
          {!rollCall ? (
            <Empty>Asking the bus who is present…</Empty>
          ) : rollCall.agents.length === 0 ? (
            <Empty>Nobody is on the bus.</Empty>
          ) : (
            <ul className="bus-roster">
              {rollCall.agents.map((agent) => (
                <Roster key={agent.id} agent={agent} />
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </>
  );
}

/** One thing said, with who said it and who is expected to answer. */
function Message({ message }: { message: BusMessage }) {
  const chunk = message.chunk ?? {};
  const body = useMemo(() => preview(chunk.payload), [chunk.payload]);
  return (
    <li className={message.addressed_to_me ? "bus-line addressed" : "bus-line"}>
      <span className="bus-when micro num">
        {clockTime(typeof chunk.created_at === "string" ? chunk.created_at : null)}
      </span>
      <span className="bus-who truncate" title={message.from_agent_id}>
        {shortId(message.from_agent_id)}
      </span>
      <span className="bus-said">
        <span className="bus-meta micro">
          <span className="chip">{message.topic || UNKNOWN}</span>
          {message.addressed_to.length ? (
            <span
              className="bus-to"
              title={
                "addressing, not access — point-to-point messages are not " +
                "private; this names who is expected to answer"
              }
            >
              → {message.addressed_to.map(shortId).join(", ")}
            </span>
          ) : (
            <span className="unknown-text">→ everyone</span>
          )}
          {message.reply_expected ? (
            <span className="chip reply-expected">reply expected</span>
          ) : null}
        </span>
        <span className="bus-body">{body}</span>
      </span>
    </li>
  );
}

function Roster({ agent }: { agent: BusRosterAgent }) {
  const capabilities = agent.capabilities ?? [];
  return (
    <li className="bus-roster-row">
      <span className="bus-roster-head">
        <span className="chip">
          <span
            className="swatch"
            style={{ background: agentStatusColor(agent.status ?? "") }}
          />
          <span className="truncate" title={agent.id}>
            {agent.name || shortId(agent.id)}
          </span>
        </span>
        <span className="chip">
          <span
            className="swatch"
            style={{ background: healthColor(agent.health_status ?? "") }}
          />
          {agent.health_status || UNKNOWN}
        </span>
      </span>
      <span className="micro unknown-text">
        {capabilities.length
          ? capabilities.join(" · ")
          : "no capabilities declared"}
      </span>
      {agent.current_task_id ? (
        <span className="micro">on {shortId(agent.current_task_id)}</span>
      ) : null}
    </li>
  );
}

/**
 * The token this console holds is not an agent's, so it cannot join.
 *
 * Stated as a fact about the credential rather than as a failure, because it
 * is not one: the bus is a conversation between agents, and the read endpoints
 * bind the caller to an agent identity on purpose. Widening that so a reader
 * could impersonate an agent is exactly the change this view must not motivate.
 */
function NotOnTheBus({ identity }: { identity: BusIdentity }) {
  return (
    <div className="banner serious">
      <span className="icon" aria-hidden="true">
        !
      </span>
      <span>
        <strong>This session is not on the bus.</strong> {identity.reason}
        <div className="unknown-text" style={{ marginTop: 6 }}>
          The bus reads are self-only by design — an agent connects to the bus
          as itself. Open the console with a token bound to a bus persona (see
          ADR 0025) rather than a plain read token; the alternative, letting a
          read token name any agent it likes, is the thing that boundary exists
          to prevent.
        </div>
      </span>
    </div>
  );
}

/** A bounded, readable rendering of whatever a chunk carried. */
function preview(payload: unknown): string {
  if (payload === null || payload === undefined) return UNKNOWN;
  if (typeof payload === "string") return clip(payload);
  if (typeof payload !== "object") return clip(String(payload));
  const record = payload as Record<string, unknown>;
  for (const key of ["text", "message", "summary", "body"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return clip(value);
  }
  try {
    return clip(JSON.stringify(record));
  } catch {
    return UNKNOWN;
  }
}

const PREVIEW_CHARS = 400;

function clip(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (!flat) return UNKNOWN;
  return flat.length > PREVIEW_CHARS
    ? `${flat.slice(0, PREVIEW_CHARS)}… (${count(flat.length)} chars)`
    : flat;
}
