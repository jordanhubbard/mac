/**
 * Layered DAG layout for Mission Control. No graph library: observe/ stays
 * on react + react-dom (ADR 0018 / ADR 0034).
 *
 * Rank 0 is the left: tasks with no in-view prerequisite. Edges point
 * prerequisite → waiter, left → right.
 */

export interface DagEdge {
  from: string;
  to: string;
}

export interface LaidNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  rank: number;
}

export interface LaidEdge {
  from: string;
  to: string;
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
  isBackEdge: boolean;
}

export interface DagLayout {
  nodes: LaidNode[];
  edges: LaidEdge[];
  ranks: Array<{ rank: number; x: number; y: number; width: number; height: number }>;
  width: number;
  height: number;
}

const NODE_WIDTH = 148;
const NODE_HEIGHT = 36;
const COL_GAP = 36;
const ROW_GAP = 10;
const PAD = 12;

export function computeDagLayout(
  nodeIds: readonly string[],
  edges: readonly DagEdge[],
  opts?: { nodeWidth?: number; nodeHeight?: number },
): DagLayout {
  const nodeWidth = opts?.nodeWidth ?? NODE_WIDTH;
  const nodeHeight = opts?.nodeHeight ?? NODE_HEIGHT;
  const idset = new Set(nodeIds);
  const inbound: Record<string, string[]> = {};
  const outbound: Record<string, string[]> = {};
  for (const id of nodeIds) {
    inbound[id] = [];
    outbound[id] = [];
  }
  const visible: DagEdge[] = [];
  for (const edge of edges) {
    if (!idset.has(edge.from) || !idset.has(edge.to) || edge.from === edge.to) {
      continue;
    }
    visible.push(edge);
    inbound[edge.to].push(edge.from);
    outbound[edge.from].push(edge.to);
  }

  const rank: Record<string, number> = {};
  const visiting = new Set<string>();

  const walk = (id: string): number => {
    if (rank[id] !== undefined) return rank[id];
    if (visiting.has(id)) {
      rank[id] = 0;
      return 0;
    }
    visiting.add(id);
    let best = 0;
    for (const pred of inbound[id] ?? []) {
      best = Math.max(best, walk(pred) + 1);
    }
    visiting.delete(id);
    rank[id] = best;
    return best;
  };
  for (const id of nodeIds) walk(id);

  const columns: string[][] = [];
  for (const id of nodeIds) {
    const r = rank[id] ?? 0;
    (columns[r] ??= []).push(id);
  }
  const colCount = Math.max(1, columns.length);
  const maxRows = Math.max(1, ...columns.map((col) => col.length));
  const width = PAD * 2 + colCount * nodeWidth + Math.max(0, colCount - 1) * COL_GAP;
  const height = PAD * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * ROW_GAP;

  const laid: LaidNode[] = [];
  const byId: Record<string, LaidNode> = {};
  const rankBands: DagLayout["ranks"] = [];
  columns.forEach((col, r) => {
    const x = PAD + r * (nodeWidth + COL_GAP);
    rankBands.push({
      rank: r,
      x,
      y: PAD - 4,
      width: nodeWidth,
      height: height - PAD * 2 + 8,
    });
    col.forEach((id, row) => {
      const node: LaidNode = {
        id,
        x,
        y: PAD + row * (nodeHeight + ROW_GAP),
        width: nodeWidth,
        height: nodeHeight,
        rank: r,
      };
      laid.push(node);
      byId[id] = node;
    });
  });

  const laidEdges: LaidEdge[] = visible.map((edge) => {
    const from = byId[edge.from];
    const to = byId[edge.to];
    const isBackEdge = !from || !to || to.rank <= from.rank;
    return {
      from: edge.from,
      to: edge.to,
      sourceX: from ? from.x + from.width : 0,
      sourceY: from ? from.y + from.height / 2 : 0,
      targetX: to ? to.x : 0,
      targetY: to ? to.y + to.height / 2 : 0,
      isBackEdge,
    };
  });

  return { nodes: laid, edges: laidEdges, ranks: rankBands, width, height };
}
