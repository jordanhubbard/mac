import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Allotment } from "allotment";
import { api, clearToken, getToken, setToken, type Agent, type Task, type TaskDetail } from "./api/mac";
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
  const [needToken, setNeedToken] = useState(() => !getToken());
  const [tokenInput, setTokenInput] = useState("");
  const [tokenError, setTokenError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [t, a] = await Promise.all([api.listTasks(), api.listAgents()]);
      setTasks(Array.isArray(t) ? t : []);
      setAgents(Array.isArray(a) ? a : []);
      setErr("");
    } catch (e: any) {
      const message = String(e?.message || e);
      setErr(message);
      if (/HTTP\s+(401|403)\b/.test(message)) {
        clearToken();
        setTokenError(
          "The hub rejected that bearer token. Use the fleet-scoped MAC_API_TOKEN__<FLEET> value when present, otherwise the hub's current MAC_API_TOKEN."
        );
        setNeedToken(true);
      }
    }
  }, []);

  const submitToken = useCallback((event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const token = setToken(tokenInput);
    if (!token) {
      setTokenError("Enter a bearer token to connect.");
      return;
    }
    setTokenError("");
    setNeedToken(false);
  }, [tokenInput]);

  const resetToken = useCallback(() => {
    clearToken();
    setTokenInput("");
    setTokenError("");
    setNeedToken(true);
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
        <form className="composer login-card" onSubmit={submitToken}>
          <div className="panel-title">Connect to the MAC hub</div>
          <p className="muted">
            Paste the hub bearer token. Fleet-scoped values named MAC_API_TOKEN__&lt;FLEET&gt; usually beat older flat tokens.
          </p>
          <input
            autoFocus
            aria-label="Hub bearer token"
            autoComplete="off"
            name="token"
            type="password"
            style={{ width: "100%" }}
            placeholder="bearer token"
            value={tokenInput}
            onChange={(e) => {
              setTokenInput(e.target.value);
              if (tokenError) setTokenError("");
            }}
          />
          <div className="controls">
            <button className="btn primary" disabled={!tokenInput.trim()} type="submit">Connect</button>
          </div>
          {tokenError ? <p className="login-error" role="alert">{tokenError}</p> : null}
        </form>
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
              <AgentsPanel agents={agents} tasks={tasks} onDispatched={refresh} />
            </Allotment.Pane>
          </Allotment>
        </div>
      </div>
      <div className="statusbar">
        <span>⛭ {agents.length} agents · {busy} busy</span>
        <span>{tasks.length} tasks</span>
        <button className="status-link" type="button" onClick={resetToken}>change token</button>
        <span style={{ marginLeft: "auto" }}>{selected ? selected : "no task selected"}</span>
      </div>
    </div>
  );
}
