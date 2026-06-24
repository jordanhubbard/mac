// Thin client for the MAC hub. In dev, requests go to /api/* which Vite proxies
// to the hub (see vite.config.ts). The bearer token is read from VITE_MAC_TOKEN
// or localStorage("mac.token") so it can be set without rebuilding.

const BASE = "/api";

export function getToken(): string {
  return (
    localStorage.getItem("mac.token") ||
    (import.meta as any).env?.VITE_MAC_TOKEN ||
    ""
  );
}
export function setToken(t: string): void {
  localStorage.setItem("mac.token", t.trim());
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const tok = getToken();
  if (tok) headers["Authorization"] = `Bearer ${tok}`;
  const res = await fetch(BASE + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${path}: ${text.slice(0, 300)}`);
  }
  return (await res.json()) as T;
}

export interface ActivityEntry {
  phase: string;
  actor: string;
  summary: string;
  at: string;
}
export interface Task {
  id: string;
  title?: string;
  state?: string;
  project?: string;
  description?: string;
  metadata?: { activity?: ActivityEntry[]; [k: string]: unknown };
  [k: string]: unknown;
}
export interface TaskDetail {
  task: Task;
  evidence?: any[];
  history?: any[];
  reviews?: any[];
}
export interface Agent {
  id: string;
  name?: string;
  status?: string;
  health_status?: string;
  current_task_id?: string | null;
  capabilities?: string[];
}

export const api = {
  listTasks: (state?: string) =>
    req<Task[]>("GET", `/tasks${state ? `?state=${encodeURIComponent(state)}` : ""}`),
  getTask: (id: string) => req<TaskDetail>("GET", `/tasks/${encodeURIComponent(id)}`),
  listAgents: () => req<Agent[]>("GET", "/agents"),
  createTask: (payload: { title: string; description: string; project: string; priority?: number }) =>
    req<Task>("POST", "/tasks", payload),
  summary: (id: string) => req<TaskDetail>("GET", `/tasks/${encodeURIComponent(id)}`),
};
