import { useCallback, useEffect, useState } from "react";
import type { ConsoleClient, ProjectGraphResponse } from "./api";
import { HubError, HubUnreachableError } from "./http";

export interface ProjectGraphState {
  payload: ProjectGraphResponse | null;
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
 * One project's bounded graph. Reloads when the project changes and when
 * `refreshKey` ticks (the console's observability sequence), so the view
 * stays live without inventing a second stream.
 */
export function useProjectGraph(
  client: ConsoleClient,
  project: string,
  refreshKey: number,
): ProjectGraphState {
  const [payload, setPayload] = useState<ProjectGraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!project) {
      setPayload(null);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const next = await client.projectGraph(project);
      setPayload(next);
      setError(null);
    } catch (err) {
      setPayload(null);
      setError(describe(err));
    } finally {
      setLoading(false);
    }
  }, [client, project, refreshKey]);

  useEffect(() => {
    setPayload(null);
    setError(null);
  }, [project]);

  useEffect(() => {
    void load();
  }, [load]);

  return { payload, error, loading, reload: () => void load() };
}
