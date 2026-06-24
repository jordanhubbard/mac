import { useCallback, useEffect, useState } from "react";
import { Allotment } from "allotment";
import { api, getToken, setToken, type Agent, type Task, type TaskDetail } from "./api/mac";
import { Sidebar } from "./components/Sidebar";
import { EditorArea } from "./components/EditorArea";
import { BottomPanel } from "./components/BottomPanel";
import { AgentsPanel } from "./components/AgentsPanel";

export function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [err, setErr] = useState<string>("");
  const [needToken, setNeedToken] = useState(!getToken());

  const refresh = useCallback(async () => {
    try {
      const [t, a] = await Promise.all([api.listTasks(), api.listAgents()]);
      setTasks(Array.isArray(t) ? t : []);
      setAgents(Array.isArray(a) ? a : []);
      setErr("");
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    if (needToken) return;
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [refresh, needToken]);

  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    let live = true;
    const load = () => api.getTask(selected).then((d) => live && setDetail(d)).catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => { live = false; clearInterval(id); };
  }, [selected]);

  if (needToken) {
    return (
      <div className="ide" style={{ alignItems: "center", justifyContent: "center" }}>
        <div className="composer" style={{ width: 420, marginTop: "20vh" }}>
          <div className="panel-title">Connect to the MAC hub</div>
          <p className="muted">Paste a hub bearer token (MAC_API_TOKEN). It's stored locally.</p>
          <input
            style={{ width: "100%" }}
            placeholder="bearer token"
            onKeyDown={(e) => {
              if (e.key === "Enter") { setToken((e.target as HTMLInputElement).value); setNeedToken(false); }
            }}
          />
        </div>
      </div>
    );
  }

  const busy = agents.filter((a) => a.current_task_id).length;
  return (
    <div className="ide">
      <div className="titlebar">MAC — Fleet IDE{err ? ` · ⚠ ${err.slice(0, 80)}` : ""}</div>
      <div className="body">
        <div className="activitybar">
          <button className="active" title="Explorer">🗂</button>
          <button title="Search">🔎</button>
          <button title="Source Control">⌥</button>
          <button title="Run">▷</button>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Allotment>
            <Allotment.Pane minSize={180} preferredSize={260}>
              <Sidebar tasks={tasks} selected={selected} onSelect={setSelected} />
            </Allotment.Pane>
            <Allotment.Pane>
              <Allotment vertical>
                <Allotment.Pane preferredSize="65%">
                  <EditorArea detail={detail} />
                </Allotment.Pane>
                <Allotment.Pane minSize={120} preferredSize="35%">
                  <BottomPanel detail={detail} />
                </Allotment.Pane>
              </Allotment>
            </Allotment.Pane>
            <Allotment.Pane minSize={240} preferredSize={320}>
              <AgentsPanel agents={agents} onDispatched={refresh} />
            </Allotment.Pane>
          </Allotment>
        </div>
      </div>
      <div className="statusbar">
        <span>⛭ {agents.length} agents · {busy} busy</span>
        <span>{tasks.length} tasks</span>
        <span style={{ marginLeft: "auto" }}>{selected ? selected : "no task selected"}</span>
      </div>
    </div>
  );
}
