import { type FormEvent, useMemo, useState } from "react";
import { api, type Agent, type AgentCard, type DashboardState, type TaskDetail } from "../api/mac";

type MeshTab = "agents" | "a2a";

function initials(agent: Agent): string {
  return (agent.name || agent.id.replace(/^agent_/, ""))
    .split(/[\s_-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

function title(agent: Agent): string {
  return agent.name || agent.id.replace(/^agent_/, "");
}

export function AgentMesh({
  data,
  card,
  selectedAgentId,
  selectedTask,
  onSelectAgent,
  onRefresh,
}: {
  data: DashboardState;
  card: AgentCard | null;
  selectedAgentId: string | null;
  selectedTask: TaskDetail | null;
  onSelectAgent: (agentId: string) => void;
  onRefresh: () => void;
}) {
  const agents = data.agents.map((item) => item.agent);
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) || agents[0] || null;
  const [tab, setTab] = useState<MeshTab>("agents");
  const [composeOpen, setComposeOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [project, setProject] = useState("mac");
  const [priority, setPriority] = useState(1);
  const [capabilities, setCapabilities] = useState("");
  const [dependencies, setDependencies] = useState("");
  const [targetAgent, setTargetAgent] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const projectNames = useMemo(() => {
    const names = data.project_summaries.map((record) => String(record.name || record.project || record.id || "")).filter(Boolean);
    if (!names.includes("mac")) names.unshift("mac");
    return Array.from(new Set(names));
  }, [data.project_summaries]);

  async function dispatch(event: FormEvent) {
    event.preventDefault();
    const body = message.trim();
    if (!body) return;
    setBusy(true);
    setResult("");
    try {
      if (tab === "a2a") {
        const task = await api.sendA2AMessage(body);
        setResult(`A2A accepted · ${String(task.id || "task created")}`);
      } else {
        const task = await api.createTask({
          title: body.split("\n")[0].slice(0, 160),
          description: body,
          project,
          priority,
          required_capabilities: capabilities.split(",").map((item) => item.trim()).filter(Boolean),
          dependencies: dependencies.split(",").map((item) => item.trim()).filter(Boolean),
          metadata: { origin: { type: "fleet_workbench" } },
        });
        if (targetAgent) await api.claimTask(task.id, targetAgent);
        setResult(`${targetAgent ? "assigned" : "dispatched"} · ${task.id}`);
      }
      setMessage("");
      setComposeOpen(false);
      onRefresh();
    } catch (error) {
      setResult(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function requestReview() {
    if (!selectedTask || !selectedAgent) return;
    setBusy(true);
    try {
      await api.requestReview(selectedTask.task.id, selectedAgent.id);
      setResult(`Review requested from ${title(selectedAgent)}.`);
      onRefresh();
    } catch (error) {
      setResult(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="agent-mesh">
      <header className="mesh-header">
        <span>Agent mesh</span>
        <button className="icon-button" title="Pin inspector" type="button"><i className="codicon codicon-pin" /></button>
      </header>
      <div className="mesh-tabs" role="tablist">
        <button aria-selected={tab === "agents"} className={tab === "agents" ? "active" : ""} onClick={() => setTab("agents")} role="tab" type="button">Agents</button>
        <button aria-selected={tab === "a2a"} className={tab === "a2a" ? "active" : ""} onClick={() => setTab("a2a")} role="tab" type="button">A2A</button>
      </div>

      {tab === "agents" ? (
        <>
          <div className="mesh-agent-strip">
            {agents.slice(0, 8).map((agent) => (
              <button
                className={selectedAgent?.id === agent.id ? "selected" : ""}
                key={agent.id}
                onClick={() => onSelectAgent(agent.id)}
                title={title(agent)}
                type="button"
              >
                <span className="agent-avatar">{initials(agent)}</span>
                <span className={`presence ${agent.health_status === "healthy" ? "online" : "offline"}`} />
              </button>
            ))}
          </div>
          {selectedAgent ? <AgentInspector agent={selectedAgent} selectedTask={selectedTask} /> : <MeshEmpty />}
        </>
      ) : (
        <A2AInspector card={card} />
      )}

      <div className="mesh-thread">
        <div className="thread-heading">
          <span>{selectedTask ? "Task thread" : "Fleet activity"}</span>
          {selectedTask && selectedAgent ? (
            <button className="text-button" disabled={busy} onClick={requestReview} type="button">Request review</button>
          ) : null}
        </div>
        <TaskThread data={data} task={selectedTask} />
      </div>

      <form className={`mesh-composer ${composeOpen ? "expanded" : ""}`} onSubmit={dispatch}>
        <textarea
          aria-label="Message or delegate work"
          onChange={(event) => setMessage(event.target.value)}
          onFocus={() => setComposeOpen(true)}
          placeholder={tab === "a2a" ? "Delegate through A2A…" : "Message or delegate…"}
          value={message}
        />
        {composeOpen && tab === "agents" ? (
          <div className="composer-fields">
            <label>Project<select onChange={(event) => setProject(event.target.value)} value={project}>{projectNames.map((name) => <option key={name}>{name}</option>)}</select></label>
            <label>Priority<input max={9} min={0} onChange={(event) => setPriority(Number(event.target.value))} type="number" value={priority} /></label>
            <label>Assign<select onChange={(event) => setTargetAgent(event.target.value)} value={targetAgent}><option value="">Dispatcher chooses</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{title(agent)}</option>)}</select></label>
            <label className="wide">Capabilities<input onChange={(event) => setCapabilities(event.target.value)} placeholder="python, security" value={capabilities} /></label>
            <label className="wide">Dependencies<input onChange={(event) => setDependencies(event.target.value)} placeholder="task_id, task_id" value={dependencies} /></label>
          </div>
        ) : null}
        <div className="composer-footer">
          <span className="composer-protocol">{tab === "a2a" ? `A2A ${card?.protocolVersion || ""}` : "MAC ledger"}</span>
          <button className="send-button" disabled={busy || !message.trim()} title="Send" type="submit"><i className="codicon codicon-send" /></button>
        </div>
        {result ? <div className="composer-result">{result}</div> : null}
      </form>
    </aside>
  );
}

function AgentInspector({ agent, selectedTask }: { agent: Agent; selectedTask: TaskDetail | null }) {
  const currentTask = agent.current_task_id || (selectedTask?.task.owner_agent_id === agent.id ? selectedTask.task.id : null);
  return (
    <section className="agent-inspector">
      <div className="selected-agent-head">
        <span className="agent-avatar large">{initials(agent)}</span>
        <span><strong>{title(agent)}</strong><small>{agent.role_id || (currentTask ? "active agent" : "available agent")}</small></span>
        <span className={`health-label ${agent.health_status === "healthy" ? "online" : "offline"}`}>{agent.health_status || agent.status}</span>
      </div>
      <Definition label="Current task" value={currentTask || "idle"} mono />
      <Definition label="Status" value={agent.status || "unknown"} />
      <div className="inspector-section"><span>Declared capabilities</span><div className="capability-list">{(agent.capabilities || []).map((item) => <span key={item}>{item}</span>)}</div></div>
      <div className="inspector-section"><span>Protocols</span><div className="protocol-list"><span>A2A routable</span><span>ACP</span></div></div>
    </section>
  );
}

function A2AInspector({ card }: { card: AgentCard | null }) {
  return (
    <section className="agent-inspector a2a-inspector">
      <div className="selected-agent-head">
        <span className="agent-avatar large"><i className="codicon codicon-radio-tower" /></span>
        <span><strong>{card?.name || "A2A gateway"}</strong><small>{card?.protocolVersion || "Agent Card unavailable"}</small></span>
      </div>
      <p>{card?.description || "The selected hub did not publish an Agent Card."}</p>
      <Definition label="Endpoint" value={card?.url || "—"} mono />
      <Definition label="Streaming" value={String(card?.capabilities?.streaming ?? false)} />
      <div className="inspector-section"><span>Published skills</span><div className="capability-list">{(card?.skills || []).map((skill) => <span key={skill.id || skill.name}>{skill.name || skill.id}</span>)}</div></div>
    </section>
  );
}

function TaskThread({ data, task }: { data: DashboardState; task: TaskDetail | null }) {
  const activity = task?.task.metadata?.activity || [];
  const messages = task ? activity.slice(-8).map((entry, index) => ({
    id: `${entry.at}-${index}`,
    actor: entry.actor,
    at: entry.at,
    summary: entry.summary,
    phase: entry.phase,
  })) : data.messages.slice(-8).map((entry, index) => ({
    id: String(entry.id || index),
    actor: String(entry.sender_agent_id || entry.actor || "fleet"),
    at: String(entry.created_at || ""),
    summary: String(entry.body || entry.content || entry.summary || "message"),
    phase: String(entry.kind || "message"),
  }));
  if (!messages.length) return <div className="empty-state"><span>No activity in this thread yet.</span></div>;
  return (
    <div className="thread-list">
      {messages.map((entry) => (
        <div className="thread-entry" key={entry.id}>
          <span className="thread-avatar"><i className="codicon codicon-hubot" /></span>
          <div><div className="thread-meta"><strong>{entry.actor.replace(/^agent_/, "")}</strong><time>{entry.at.slice(11, 16)}</time></div><p>{entry.summary}</p><span className="thread-phase">{entry.phase}</span></div>
        </div>
      ))}
    </div>
  );
}

function Definition({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="definition"><span>{label}</span><strong className={mono ? "mono" : ""}>{value}</strong></div>;
}

function MeshEmpty() {
  return <div className="empty-state"><span>No agent is registered with this hub.</span></div>;
}
