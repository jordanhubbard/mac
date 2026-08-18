import type { ReactNode } from "react";
import { UNKNOWN, count } from "../lib/format";

export function Panel({
  title,
  sub,
  accent,
  children,
  wide,
}: {
  title: string;
  sub?: ReactNode;
  accent?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <section
      className={wide ? "panel span-2" : "panel"}
      style={accent ? ({ "--accent": accent } as React.CSSProperties) : undefined}
    >
      <header>
        <h2>{title}</h2>
        {sub ? <span className="sub">{sub}</span> : null}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

/**
 * The only way this app renders "this section is missing". It exists so that a
 * section the hub could not read is visually unmistakable from a section that
 * read successfully and found nothing.
 */
export function Unavailable({ what, reason }: { what: string; reason?: string }) {
  return (
    <div className="banner serious">
      <span className="icon" aria-hidden="true">
        !
      </span>
      <span>
        <strong>{what} unavailable.</strong> The hub did not return this section,
        so its numbers are unknown — not zero.
        {reason ? <div className="unknown-text">{reason}</div> : null}
      </span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Tile({
  label,
  value,
  note,
  accent,
  tone,
}: {
  label: string;
  value: number | string | null | undefined;
  note?: ReactNode;
  accent?: string;
  tone?: "good" | "warn" | "bad";
}) {
  const known = value !== null && value !== undefined;
  const text = typeof value === "number" ? count(value) : (value ?? UNKNOWN);
  const icon = tone === "bad" ? "▲ " : tone === "warn" ? "! " : "";
  return (
    <div
      className="tile"
      style={accent ? ({ "--accent": accent } as React.CSSProperties) : undefined}
    >
      <div className={known ? "value" : "value unknown"}>
        {known ? `${icon}${text}` : "unknown"}
      </div>
      <div className="label micro">{label}</div>
      {note ? <div className="note">{note}</div> : null}
    </div>
  );
}

export interface BarDatum {
  key: string;
  label?: string;
  value: number;
  color: string;
  title?: string;
}

/**
 * Horizontal magnitude bars. Chosen over a pie/donut deliberately: these are
 * category magnitudes with long labels and a wide dynamic range, which is the
 * bar's job. Data-ends are 4px-rounded and anchored to a common baseline;
 * every row is directly labelled, so the colour never has to carry identity.
 */
export function Bars({ data, max }: { data: BarDatum[]; max?: number }) {
  const ceiling = Math.max(1, max ?? Math.max(...data.map((d) => d.value), 1));
  if (!data.length) return <Empty>Nothing in any state.</Empty>;
  return (
    <div className="bars">
      {data.map((d) => (
        <Fragmented key={d.key} datum={d} ceiling={ceiling} />
      ))}
    </div>
  );
}

function Fragmented({ datum, ceiling }: { datum: BarDatum; ceiling: number }) {
  const pct = Math.max(1.2, (datum.value / ceiling) * 100);
  return (
    <>
      <span className="chip" title={datum.title}>
        <span className="swatch" style={{ background: datum.color }} />
        {datum.label ?? datum.key}
      </span>
      <span className="bar-track">
        <span
          className="bar-fill"
          style={{ width: `${pct}%`, background: datum.color }}
        />
      </span>
      <span className="num" style={{ textAlign: "right" }}>
        {count(datum.value)}
      </span>
    </>
  );
}

export function Legend({ items }: { items: Array<{ key: string; color: string }> }) {
  if (items.length < 2) return null;
  return (
    <div className="legend">
      {items.map((item) => (
        <span className="chip" key={item.key}>
          <span className="swatch" style={{ background: item.color }} />
          {item.key}
        </span>
      ))}
    </div>
  );
}
