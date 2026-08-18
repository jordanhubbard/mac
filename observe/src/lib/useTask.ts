import { useCallback, useEffect, useState } from "react";
import type { ConsoleClient, TaskDrilldown, TranscriptEntry } from "./api";
import { HubError, HubUnreachableError } from "./http";

export interface TaskState {
  detail: TaskDrilldown | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

function describe(err: unknown): string {
  if (err instanceof HubError || err instanceof HubUnreachableError) {
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

/**
 * One task's drill-down.
 *
 * Not on the live refresh loop: a drill-down is something you open and read,
 * and re-fetching it under the cursor would move the turn you were reading.
 * It reloads on demand and when the task id changes.
 */
export function useTask(client: ConsoleClient, taskId: string | null): TaskState {
  const [detail, setDetail] = useState<TaskDrilldown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!taskId) {
      setDetail(null);
      setError(null);
      return;
    }
    setLoading(true);
    try {
      setDetail(await client.task(taskId));
      setError(null);
    } catch (err) {
      // Keep nothing: a stale drill-down for a DIFFERENT task would be worse
      // than an empty pane, because the header would name the task you asked
      // for while the body described another.
      setDetail(null);
      setError(describe(err));
    } finally {
      setLoading(false);
    }
  }, [client, taskId]);

  useEffect(() => {
    void load();
  }, [load]);

  return { detail, error, loading, reload: () => void load() };
}

export interface TranscriptState {
  entry: TranscriptEntry | null;
  error: string | null;
  loading: boolean;
}

/** The text of one expanded transcript turn, fetched lazily. */
export function useTranscript(
  client: ConsoleClient,
  transcriptId: string | null,
): TranscriptState {
  const [entry, setEntry] = useState<TranscriptEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!transcriptId) {
      setEntry(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setEntry(null);
    client
      .transcript(transcriptId)
      .then((next) => {
        if (!cancelled) {
          setEntry(next);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(describe(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, transcriptId]);

  return { entry, error, loading };
}
