import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ConsoleClient, EXPECTED_SCHEMA, type Snapshot } from "./api";
import { HubError, HubUnreachableError } from "./http";

export type Liveness = "connecting" | "live" | "stale" | "down";
export type StreamState = "connecting" | "streaming" | "polling";

export interface LiveState {
  snapshot: Snapshot | null;
  /** Set whenever the most recent attempt failed. Never clears the snapshot. */
  error: string | null;
  errorKind: "auth" | "unreachable" | "other" | null;
  /** Seconds since the last SUCCESSFUL read, or null if there has never been one. */
  ageSeconds: number | null;
  liveness: Liveness;
  stream: StreamState;
  streamNote: string | null;
  /** Schema the hub returned, when it is not the one this build understands. */
  schemaMismatch: string | null;
  refresh: () => void;
}

/** Beyond this the displayed numbers are old enough to call out. */
const STALE_AFTER_SECONDS = 25;
const DOWN_AFTER_SECONDS = 90;
/**
 * Floor poll. Even with a healthy stream we re-read on this cadence so a
 * missed event cannot leave the console silently frozen on old numbers — the
 * exact failure this console exists to make impossible.
 */
const STREAM_POLL_SECONDS = 20;
const FALLBACK_POLL_SECONDS = 6;
/** Coalesce bursts of stream events into one read. */
const REFRESH_DEBOUNCE_MS = 400;

export function useLive(
  client: ConsoleClient,
  windowHours: number,
  buckets: number,
): LiveState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorKind, setErrorKind] = useState<LiveState["errorKind"]>(null);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const [stream, setStream] = useState<StreamState>("connecting");
  const [streamNote, setStreamNote] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const inFlight = useRef(false);
  const nonce = useRef(0);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const mine = ++nonce.current;
    try {
      const next = await client.snapshot(windowHours, buckets);
      if (mine !== nonce.current) return;
      setSnapshot(next);
      setLastOk(Date.now());
      setError(null);
      setErrorKind(null);
    } catch (err) {
      if (mine !== nonce.current) return;
      // Deliberately do NOT clear `snapshot`. Stale-and-labelled beats blank,
      // and blank is far better than a fresh-looking zero — but the caller
      // must render the age, which is why `ageSeconds` keeps ticking.
      if (err instanceof HubError) {
        setErrorKind(err.status === 401 || err.status === 403 ? "auth" : "other");
        setError(err.message);
      } else if (err instanceof HubUnreachableError) {
        setErrorKind("unreachable");
        setError(err.message);
      } else {
        setErrorKind("other");
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      inFlight.current = false;
    }
  }, [client, windowHours, buckets]);

  // Initial + parameter-change read.
  useEffect(() => {
    void load();
  }, [load]);

  // Live spine: the hub's observability cursor stream. An event says only
  // "something moved"; the snapshot read is what fetches the new numbers.
  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    let debounce: ReturnType<typeof setTimeout> | undefined;
    let consecutiveFailures = 0;

    const nudge = () => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => void load(), REFRESH_DEBOUNCE_MS);
    };

    void (async () => {
      while (!stopped) {
        try {
          setStream((current) => (current === "streaming" ? current : "connecting"));
          await client.subscribe((event) => {
            consecutiveFailures = 0;
            setStream("streaming");
            setStreamNote(null);
            if (event.event === "updated") nudge();
          }, controller.signal);
          if (stopped) return;
          // A clean end is the hub's own stream deadline; reconnect at once.
        } catch (err) {
          if (stopped) return;
          consecutiveFailures += 1;
          if (consecutiveFailures >= 2) {
            // Say so rather than pretending: the console is still correct, it
            // is just refreshing on a timer now.
            setStream("polling");
            setStreamNote(
              `live stream unavailable (${
                err instanceof Error ? err.message : String(err)
              }); polling every ${FALLBACK_POLL_SECONDS}s`,
            );
          }
          await new Promise((resolve) =>
            setTimeout(resolve, Math.min(15_000, 1_000 * consecutiveFailures)),
          );
        }
      }
    })();

    return () => {
      stopped = true;
      if (debounce) clearTimeout(debounce);
      controller.abort();
    };
  }, [client, load]);

  // Floor poll + the 1Hz clock that ages the displayed data.
  useEffect(() => {
    const clock = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(clock);
  }, []);

  useEffect(() => {
    const period = stream === "streaming" ? STREAM_POLL_SECONDS : FALLBACK_POLL_SECONDS;
    const timer = setInterval(() => void load(), period * 1000);
    return () => clearInterval(timer);
  }, [load, stream]);

  const ageSeconds = useMemo(() => {
    void tick; // re-derive every second
    return lastOk === null ? null : (Date.now() - lastOk) / 1000;
  }, [lastOk, tick]);

  const liveness: Liveness = useMemo(() => {
    if (ageSeconds === null) return error ? "down" : "connecting";
    if (error && ageSeconds > DOWN_AFTER_SECONDS) return "down";
    if (error || ageSeconds > STALE_AFTER_SECONDS) return "stale";
    return "live";
  }, [ageSeconds, error]);

  const schemaMismatch =
    snapshot && snapshot.schema !== EXPECTED_SCHEMA ? snapshot.schema : null;

  return {
    snapshot,
    error,
    errorKind,
    ageSeconds,
    liveness,
    stream,
    streamNote,
    schemaMismatch,
    refresh: () => void load(),
  };
}
