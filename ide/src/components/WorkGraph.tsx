import { memo, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import type { Agent, Task, TaskDetail } from "../api/mac";

type TaskNodeData = {
  task: Task;
  agent?: Agent;
  selected: boolean;
  onInspect: (taskId: string) => void;
};

const STAGES = ["Intake", "Waiting", "Build", "Review", "Blocked"] as const;
const MAX_VISIBLE_NODES = 25;
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);

function stageIndex(state?: string): number {
  switch ((state || "").toLowerCase()) {
    case "open":
    case "ready":
      return 0;
    case "waiting":
      return 1;
    case "claimed":
    case "running":
    case "in_progress":
      return 2;
    case "needs_review":
    case "reviewing":
    case "in_review":
      return 3;
    case "blocked":
      return 4;
    default:
      return 2;
  }
}

function visibleTasksByStage(tasks: TaskDetail[], selectedTaskId: string | null): Task[] {
  const selected = tasks.find(({ task }) => task.id === selectedTaskId)?.task;
  const relatedIds = new Set<string>(selected ? [selected.id, ...(selected.dependencies || [])] : []);
  if (selected) {
    for (const { task } of tasks) {
      if ((task.dependencies || []).includes(selected.id)) relatedIds.add(task.id);
    }
  }
  const buckets = STAGES.map(() => [] as Task[]);
  for (const { task } of tasks) {
    if (TERMINAL_STATES.has(String(task.state))) continue;
    buckets[stageIndex(task.state)].push(task);
  }
  for (const bucket of buckets) {
    bucket.sort((left, right) => Number(relatedIds.has(right.id)) - Number(relatedIds.has(left.id)));
  }

  // Take one task from every stage before taking a second. A straight slice of
  // the ledger made later stages disappear whenever Intake/Build was busy.
  const visible: Task[] = [];
  for (let row = 0; visible.length < MAX_VISIBLE_NODES; row += 1) {
    let found = false;
    for (const bucket of buckets) {
      const task = bucket[row];
      if (!task) continue;
      found = true;
      visible.push(task);
      if (visible.length === MAX_VISIBLE_NODES) break;
    }
    if (!found) break;
  }
  return visible;
}

function initials(value: string): string {
  return value
    .replace(/^agent_/, "")
    .split(/[\s_-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "?";
}

const TaskNode = memo(function TaskNode({ data }: NodeProps<Node<TaskNodeData>>) {
  const { task, agent, selected, onInspect } = data;
  const state = String(task.state || "open");
  const agentName = agent?.name || agent?.id?.replace(/^agent_/, "") || "Unassigned";
  return (
    <div className={`work-node state-${state} ${selected ? "is-selected" : ""}`}>
      <Handle className="work-handle" position={Position.Left} type="target" />
      <div className="work-node-title">{task.title || task.id}</div>
      <div className="work-node-meta">
        <span className="agent-avatar small">{initials(agentName)}</span>
        <span>{agentName}</span>
        <span className={`state-label state-${state}`}>{state.replaceAll("_", " ")}</span>
      </div>
      {state === "blocked" ? (
        <button
          className="work-node-action nodrag nopan"
          onClick={(event) => {
            event.stopPropagation();
            onInspect(task.id);
          }}
          type="button"
        >
          Inspect block reason
        </button>
      ) : null}
      <Handle className="work-handle" position={Position.Right} type="source" />
    </div>
  );
});

const NODE_TYPES = { task: TaskNode };

export function WorkGraph({
  tasks,
  agents,
  selectedTaskId,
  onSelectTask,
  onInspectTask,
}: {
  tasks: TaskDetail[];
  agents: Agent[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  onInspectTask: (taskId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [hasSize, setHasSize] = useState(false);
  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const update = () => setHasSize(element.clientWidth > 0 && element.clientHeight > 0);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const { nodes, edges, stageCounts } = useMemo(() => {
    const visible = visibleTasksByStage(tasks, selectedTaskId);
    const visibleIds = new Set(visible.map((task) => task.id));
    const rows = STAGES.map(() => 0);
    const counts = STAGES.map(() => 0);
    for (const { task } of tasks) {
      if (TERMINAL_STATES.has(String(task.state))) continue;
      counts[stageIndex(task.state)] += 1;
    }
    const graphNodes: Array<Node<TaskNodeData>> = visible.map((task) => {
      const stage = stageIndex(task.state);
      const row = rows[stage]++;
      const agent = agents.find((candidate) =>
        candidate.id === task.owner_agent_id || candidate.current_task_id === task.id,
      );
      return {
        id: task.id,
        type: "task",
        position: { x: stage * 285, y: row * 118 + 44 },
        data: { task, agent, selected: task.id === selectedTaskId, onInspect: onInspectTask },
      };
    });
    const graphEdges: Edge[] = visible.flatMap((task) =>
      (task.dependencies || [])
        .filter((dependency) => visibleIds.has(dependency))
        .map((dependency) => ({
          id: `${dependency}-${task.id}`,
          source: dependency,
          target: task.id,
          animated: ["running", "reviewing"].includes(String(task.state)),
          className: "work-edge",
        })),
    );
    return { nodes: graphNodes, edges: graphEdges, stageCounts: counts };
  }, [agents, onInspectTask, selectedTaskId, tasks]);

  return (
    <div className="work-graph" aria-label="Live work graph" ref={containerRef}>
      <div className="graph-stage-labels">
        {STAGES.map((stage, index) => (
          <span className={stage === "Blocked" ? "stage-blocked" : ""} key={stage}>
            {stage}<strong>{stageCounts[index]}</strong>
          </span>
        ))}
      </div>
      {nodes.length && hasSize ? (
        <ReactFlow
          edges={edges}
          fitView
          fitViewOptions={{ maxZoom: 1, padding: 0.12 }}
          minZoom={0.35}
          nodeTypes={NODE_TYPES}
          nodes={nodes}
          nodesConnectable={false}
          nodesDraggable={false}
          onNodeClick={(_, node) => onSelectTask(node.id)}
          onNodeDoubleClick={(_, node) => onInspectTask(node.id)}
          proOptions={{ hideAttribution: false }}
        >
          <Background color="var(--graph-grid)" gap={24} size={1} />
          <Controls position="top-right" showInteractive={false} />
        </ReactFlow>
      ) : nodes.length ? null : (
        <div className="empty-state centered">
          <i className="codicon codicon-type-hierarchy-sub" />
          <strong>No active work</strong>
          <span>Delegate a task to start the live graph.</span>
        </div>
      )}
    </div>
  );
}
