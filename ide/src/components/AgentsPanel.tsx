import { useState } from "react";
import { api, type Agent, type Task } from "../api/mac";
import { agentRole } from "./TaskPipeline";

export function AgentsPanel({
  agents,
  tasks,
  onDispatched,
}: {
  agents: Agent[];
  tasks: Task[];
  onDispatched: () => void;
}) {
  const [text, setText] = useState("");
  const [project, setProject] = useState("nanolang");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function dispatch() {
    const body = text.trim();
    if (!body) return;
    setBusy(true);
    setMsg("");
    try {
      const title = body.split("\n")[0].slice(0, 80);
      const t = await api.createTask({ title, description: body, project, priority: 1 });
      setMsg("dispatched " + String(t.id || "").slice(0, 18));
      setText("");
      onDispatched();
    } catch (e: any) {
      setMsg("error: " + String(e?.message || e).slice(0, 120));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Agents</div>
      <div className="agents">
        <div className="composer">
          <textarea
            placeholder="Plan, Build — describe a task for the fleet…"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="controls">
            <select value={project} onChange={(e) => setProject(e.target.value)}>
              <option>nanolang</option>
              <option>mac</option>
              <option>Aviation</option>
            </select>
            <button className="btn primary" disabled={busy} onClick={dispatch}>
              {busy ? "…" : "Dispatch"}
            </button>
            <span className="muted" style={{ fontSize: 11 }}>{msg}</span>
          </div>
        </div>

        {agents.map((a) => {
          const { role, cls } = agentRole(a, tasks);
          return (
            <div className="agent-card" key={a.id}>
              <div>
                <span className={"dot " + (a.current_task_id ? "running" : a.status || "open")} />{" "}
                <b>{a.name || a.id.replace("agent_", "")}</b>{" "}
                <span className={"role-badge " + cls}>{role}</span>{" "}
                <span className="muted">{a.status}</span>
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                {a.current_task_id ? "▶ " + a.current_task_id.slice(0, 20) : "idle"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
