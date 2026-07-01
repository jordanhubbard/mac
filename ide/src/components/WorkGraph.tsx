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
};

const STAGES = ["Intake", "Build", "Review", "Publish"] as const;

function stageIndex(state?: string): number {
  switch ((state || "").toLowerCase()) {
    case "open":
    case "ready":
    case "blocked":
      return 0;
    case "claimed":
    case "running":
    case "in_progress":
      return 1;
    case "needs_review":
    case "reviewing":
    case "in_review":
      return 2;
    case "completed":
    case "published":
      return 3;
    default:
      return 1;
  }
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
  const { task, agent, selected } = data;
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
}: {
  tasks: TaskDetail[];
  agents: Agent[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
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
  const { nodes, edges } = useMemo(() => {
    const visible = tasks
      .map((detail) => detail.task)
      .filter((task) => !["cancelled", "failed"].includes(String(task.state)))
      .slice(0, 18);
    const visibleIds = new Set(visible.map((task) => task.id));
    const rows = [0, 0, 0, 0];
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
        data: { task, agent, selected: task.id === selectedTaskId },
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
    return { nodes: graphNodes, edges: graphEdges };
  }, [agents, selectedTaskId, tasks]);

  return (
    <div className="work-graph" aria-label="Live work graph" ref={containerRef}>
      <div className="graph-stage-labels" aria-hidden="true">
        {STAGES.map((stage) => <span key={stage}>{stage}</span>)}
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
