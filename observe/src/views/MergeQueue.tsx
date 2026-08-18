import type { MergeQueueRow, Snapshot } from "../lib/api";
import { Bars, Empty, Panel, Tile, Unavailable } from "../components/primitives";
import { UNKNOWN } from "../lib/format";

function reason(snap: Snapshot, section: string): string | undefined {
  return snap.degraded.find((d) => d.section === section)?.reason;
}

/**
 * How full the queue is relative to how much of it can be tested at once.
 *
 * The AIMD window is the queue's control signal: it grows by one on a land and
 * halves on a failure. Depth alone does not say whether a queue is healthy —
 * depth 6 behind a window of 4 is throughput, depth 6 behind a window of 1 is
 * a queue that has backed all the way off and is landing one change at a time.
 * Showing them together is the only way to tell those apart at a glance.
 */
function WindowMeter({ row }: { row: MergeQueueRow }) {
  const window = row.window_size;
  const slots = window ?? 0;
  const cells = Math.max(slots, Math.min(row.depth, 8));
  return (
    <div className="mq-meter" title={`depth ${row.depth}, window ${window ?? UNKNOWN}`}>
      {Array.from({ length: Math.max(cells, 1) }, (_, i) => (
        <span
          key={i}
          className={
            "mq-slot" +
            (i < slots ? " mq-slot-open" : "") +
            (i < row.depth ? " mq-slot-filled" : "")
          }
        />
      ))}
    </div>
  );
}

/**
 * The merge queue: what is waiting to land, and why something did not.
 *
 * This exists because the queue's own design note names the risk it is trying
 * to avoid — this repository has produced several gates that reported healthy
 * while enforcing nothing, and a queue nobody can watch is the next one. So
 * eviction reasons are given a panel of their own rather than left in logs:
 * "why did my change not land" is the question an operator arrives with.
 */
export function MergeQueueView({ snap }: { snap: Snapshot }) {
  const mq = snap.merge_queue;
  if (!mq) {
    return <Unavailable what="Merge queue" reason={reason(snap, "merge_queue")} />;
  }

  const attempts = mq.total_landed + mq.total_failed;

  return (
    <>
      <div className="tiles">
        <Tile label="queues" value={mq.queue_count} accent="var(--series-3)" />
        <Tile label="waiting to land" value={mq.total_depth} accent="var(--series-1)" />
        <Tile label="landed" value={mq.total_landed} accent="var(--status-good)" />
        <Tile
          label="evicted"
          value={mq.total_failed}
          accent={mq.total_failed > 0 ? "var(--status-warning)" : "var(--ink-muted)"}
        />
        <Tile
          label="land rate"
          /* Undefined, not 0%, before anything has been attempted: a queue
             that has never run has no rate, and 0% reads as total failure. */
          value={attempts ? `${Math.round((mq.total_landed / attempts) * 100)}%` : UNKNOWN}
          accent="var(--series-2)"
        />
      </div>

      <Panel title="Queues" sub="deepest first">
        {mq.queues.length === 0 ? (
          <Empty>
            No queue has been opened. A repository enrolls on its first
            publication; one with a forge merge queue never enrolls here.
          </Empty>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>repository</th>
                <th>branch</th>
                <th className="n">depth</th>
                <th className="n">window</th>
                <th>occupancy</th>
                <th className="n">landed</th>
                <th className="n">evicted</th>
                <th className="n">discarded</th>
                <th>last event</th>
              </tr>
            </thead>
            <tbody>
              {mq.queues.map((row) => (
                <tr key={`${row.repository}@${row.branch}`}>
                  <td>{row.repository}</td>
                  <td>{row.branch}</td>
                  <td className="n">{row.depth}</td>
                  {/* UNKNOWN, not 1: never-sized and backed-off-to-floor are
                      different facts and must not render the same. */}
                  <td className="n">{row.window_size ?? UNKNOWN}</td>
                  <td>
                    <WindowMeter row={row} />
                  </td>
                  <td className="n">{row.landed_count}</td>
                  <td className="n">{row.failure_count}</td>
                  <td className="n" title="speculative results thrown away after an eviction ahead of them">
                    {row.speculation_discarded}
                  </td>
                  <td>{row.last_event || UNKNOWN}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title="Queue depth" sub="live entries per queue">
        {mq.queues.length === 0 ? (
          <Empty>Nothing queued.</Empty>
        ) : (
          <Bars
            data={mq.queues.map((row) => ({
              key: `${row.repository}@${row.branch}`,
              value: row.depth,
              color: "var(--series-1)",
            }))}
          />
        )}
      </Panel>

      <Panel title="Why changes did not land" sub="ten most recent evictions">
        {mq.recent_evictions.length === 0 ? (
          <Empty>
            Nothing has been evicted. An eviction is recorded with its reason,
            so an empty panel here means no change has been turned away — not
            that the reason was lost.
          </Empty>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>repository</th>
                <th>PR</th>
                <th>reason</th>
                <th>when</th>
              </tr>
            </thead>
            <tbody>
              {mq.recent_evictions.map((e) => (
                <tr key={`${e.task_id}-${e.updated_at ?? ""}`}>
                  <td>
                    {e.repository}
                    <span style={{ opacity: 0.6 }}>@{e.branch}</span>
                  </td>
                  <td className="n">{e.pull_request_number || UNKNOWN}</td>
                  <td>{e.eviction_reason}</td>
                  <td>{e.updated_at ?? UNKNOWN}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </>
  );
}
