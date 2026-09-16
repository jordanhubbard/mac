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
export function useTask(
  client: ConsoleClient,
  taskId: string | null,
): TaskState {
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{
    client: ConsoleClient;
    taskId: string | null;
    detail: TaskDrilldown | null;
    error: string | null;
    loading: boolean;
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    setResult((previous) => ({
      client,
      taskId,
      detail:
        taskId && previous?.client === client && previous.taskId === taskId
          ? previous.detail
          : null,
      error: null,
      loading: Boolean(taskId),
    }));
    if (taskId) {
      void client.task(taskId).then(
        (detail) => {
          if (!cancelled)
            setResult({ client, taskId, detail, error: null, loading: false });
        },
        (err) => {
          if (!cancelled)
            setResult({
              client,
              taskId,
              detail: null,
              error: describe(err),
              loading: false,
            });
        },
      );
    }
    return () => {
      cancelled = true;
    };
  }, [client, taskId, revision]);

  // Reject the previous identity even in the render before the effect runs.
  const current =
    result?.client === client && result.taskId === taskId ? result : null;
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  return {
    detail: current?.detail ?? null,
    error: current?.error ?? null,
    loading: current?.loading ?? Boolean(taskId),
    reload,
  };
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
