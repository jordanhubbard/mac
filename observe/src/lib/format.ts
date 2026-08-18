// Formatting helpers. Every one of these has an explicit "I do not know"
// return, because the console must never print a plausible number it does not
// have. `null`/`undefined` in, "—" out — never "0".

export const UNKNOWN = "—";

/** Compact duration: 4d, 3h12m, 12m, 48s. Null-safe by design. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return UNKNOWN;
  }
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return h < 10 ? `${h}h${String(m % 60).padStart(2, "0")}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return d < 10 ? `${d}d${h % 24}h` : `${d}d`;
}

/** Thousands-separated integer, or "—" when the value is absent. */
export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return UNKNOWN;
  }
  return Math.round(value).toLocaleString("en-US");
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return UNKNOWN;
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)}${units[i]}`;
}

/** "14:07:22" in the viewer's local zone, or "—". */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return UNKNOWN;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return UNKNOWN;
  return at.toLocaleTimeString("en-GB", { hour12: false });
}

export function shortId(id: string | null | undefined): string {
  if (!id) return UNKNOWN;
  return id.length > 16 ? `${id.slice(0, 14)}…` : id;
}

/** Total of a `{key: count}` map, tolerating a missing map. */
export function sum(map: Record<string, number> | undefined | null): number {
  if (!map) return 0;
  return Object.values(map).reduce((a, b) => a + (Number(b) || 0), 0);
}

/** Entries sorted by count descending, then key — a stable render order. */
export function ranked(
  map: Record<string, number> | undefined | null,
): Array<[string, number]> {
  if (!map) return [];
  return Object.entries(map).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
