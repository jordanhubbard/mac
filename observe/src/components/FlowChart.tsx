import { useMemo, useRef, useState } from "react";
import type { FlowSection } from "../lib/api";
import { count, clockTime } from "../lib/format";
import { orderStates, taskStateColor } from "../lib/states";
import { Empty, Legend } from "./primitives";

const HEIGHT = 150;
const PAD_LEFT = 34;
const PAD_BOTTOM = 16;
const PAD_TOP = 6;
/** 2px of surface between stacked segments, per the mark spec. */
const SEGMENT_GAP = 2;

interface Hover {
  index: number;
  x: number;
  y: number;
}

/**
 * Transitions per time bucket, stacked by the state work moved INTO.
 *
 * This is the "live view of movement" chart: a static table can tell you 360
 * tasks are blocked, but only this can tell you whether they are arriving or
 * draining. Stacked columns rather than lines because the quantity is a count
 * of discrete events per interval, and the total per interval is itself
 * meaningful (fleet throughput).
 */
export function FlowChart({
  flow,
  width = 640,
}: {
  flow: FlowSection;
  width?: number;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<Hover | null>(null);

  const states = useMemo(() => orderStates(Object.keys(flow.series)), [flow.series]);
  const buckets = flow.bucket_starts.length;

  const totals = useMemo(() => {
    const out = new Array<number>(buckets).fill(0);
    for (const values of Object.values(flow.series)) {
      values.forEach((v, i) => {
        if (i < buckets) out[i] += v;
      });
    }
    return out;
  }, [flow.series, buckets]);

  const peak = Math.max(1, ...totals);
  const plotW = width - PAD_LEFT - 4;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const colW = buckets > 0 ? plotW / buckets : plotW;
  const barW = Math.max(1, colW - (colW > 4 ? 1 : 0));

  if (!buckets || flow.total === 0) {
    return (
      <Empty>
        No task transitions in this window. The fleet moved nothing — that is a
        real reading, not a missing one.
      </Empty>
    );
  }

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = ((event.clientX - rect.left) / rect.width) * width - PAD_LEFT;
    const index = Math.floor(x / colW);
    if (index < 0 || index >= buckets) {
      setHover(null);
      return;
    }
    setHover({ index, x: event.clientX, y: event.clientY });
  };

  const gridSteps = [0, 0.5, 1];

  return (
    <div style={{ position: "relative" }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        width="100%"
        height={HEIGHT}
        role="img"
        aria-label={`Task transitions per ${Math.round(
          flow.bucket_seconds,
        )} seconds, stacked by destination state`}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {gridSteps.map((step) => {
          const y = PAD_TOP + plotH - step * plotH;
          return (
            <g key={step}>
              <line
                x1={PAD_LEFT}
                x2={width - 4}
                y1={y}
                y2={y}
                stroke="var(--grid)"
                strokeWidth={1}
              />
              <text
                x={PAD_LEFT - 6}
                y={y + 3}
                textAnchor="end"
                fontSize={9}
                fill="var(--ink-muted)"
                fontFamily="var(--mono)"
              >
                {Math.round(step * peak)}
              </text>
            </g>
          );
        })}

        {flow.bucket_starts.map((start, index) => {
          let cursor = PAD_TOP + plotH;
          return (
            <g key={start}>
              {states.map((state) => {
                const value = flow.series[state]?.[index] ?? 0;
                if (value <= 0) return null;
                const h = (value / peak) * plotH;
                cursor -= h;
                const drawH = Math.max(1, h - SEGMENT_GAP);
                return (
                  <rect
                    key={state}
                    x={PAD_LEFT + index * colW}
                    y={cursor}
                    width={barW}
                    height={drawH}
                    rx={drawH > 4 ? 1.5 : 0}
                    fill={taskStateColor(state)}
                  />
                );
              })}
            </g>
          );
        })}

        {hover ? (
          <rect
            x={PAD_LEFT + hover.index * colW}
            y={PAD_TOP}
            width={barW}
            height={plotH}
            fill="rgba(255,255,255,0.07)"
            pointerEvents="none"
          />
        ) : null}

        <line
          x1={PAD_LEFT}
          x2={width - 4}
          y1={PAD_TOP + plotH}
          y2={PAD_TOP + plotH}
          stroke="var(--axis)"
          strokeWidth={1}
        />
        <text
          x={PAD_LEFT}
          y={HEIGHT - 4}
          fontSize={9}
          fill="var(--ink-muted)"
          fontFamily="var(--mono)"
        >
          {clockTime(flow.bucket_starts[0])}
        </text>
        <text
          x={width - 4}
          y={HEIGHT - 4}
          fontSize={9}
          textAnchor="end"
          fill="var(--ink-muted)"
          fontFamily="var(--mono)"
        >
          now
        </text>
      </svg>

      {hover ? (
        <div
          className="tooltip"
          style={{ left: hover.x + 12, top: hover.y + 12 }}
          role="status"
        >
          {[
            clockTime(flow.bucket_starts[hover.index]),
            ...states
              .map((state) => ({ state, n: flow.series[state]?.[hover.index] ?? 0 }))
              .filter((row) => row.n > 0)
              .map((row) => `${row.state.padEnd(13)} ${count(row.n)}`),
            `${"total".padEnd(13)} ${count(totals[hover.index])}`,
          ].join("\n")}
        </div>
      ) : null}

      <Legend
        items={states.map((state) => ({ key: state, color: taskStateColor(state) }))}
      />
    </div>
  );
}
