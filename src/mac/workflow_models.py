"""Workflow graph models.

Defines the workflow node and edge dataclasses and the node-type and
edge-condition vocabularies used to describe and validate MAC task workflows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from mac.models import EdgeCondition, JsonDict, NodeType, ValidationError, ensure_json_object


NODE_TYPES = {item.value for item in NodeType}
EDGE_CONDITIONS = {item.value for item in EdgeCondition}


@dataclass(frozen=True)
class WorkflowNode:
    node_key: str
    node_type: str = NodeType.TASK.value
    role_required: str = ""
    persona_hint: Optional[str] = None
    instructions: str = ""
    max_attempts: int = 1
    timeout_minutes: int = 0
    required_capabilities: List[str] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any, *, path: str) -> "WorkflowNode":
        if not isinstance(raw, dict):
            raise ValidationError("%s must be an object" % path)
        key = str(raw.get("node_key") or "").strip()
        if not key:
            raise ValidationError("%s.node_key is required" % path)
        node_type = str(raw.get("node_type") or NodeType.TASK.value).strip().lower()
        if node_type not in NODE_TYPES:
            raise ValidationError(
                "%s.node_type must be one of: %s" % (path, ", ".join(sorted(NODE_TYPES)))
            )
        role_required = str(raw.get("role_required") or "").strip()
        if not role_required:
            raise ValidationError("%s.role_required is required" % path)
        max_attempts = _positive_int(raw.get("max_attempts", 1), "%s.max_attempts" % path)
        timeout_minutes = _nonnegative_int(
            raw.get("timeout_minutes", 0),
            "%s.timeout_minutes" % path,
        )
        return cls(
            node_key=key,
            node_type=node_type,
            role_required=role_required,
            persona_hint=_optional_string(raw.get("persona_hint")),
            instructions=str(raw.get("instructions") or "").strip(),
            max_attempts=max_attempts,
            timeout_minutes=timeout_minutes,
            required_capabilities=_string_list(raw.get("required_capabilities")),
            metadata=ensure_json_object(raw.get("metadata")),
        )

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        if self.persona_hint is None:
            data.pop("persona_hint", None)
        if not self.instructions:
            data.pop("instructions", None)
        if not self.timeout_minutes:
            data.pop("timeout_minutes", None)
        if not self.required_capabilities:
            data.pop("required_capabilities", None)
        if not self.metadata:
            data.pop("metadata", None)
        return data


@dataclass(frozen=True)
class WorkflowEdge:
    from_node_key: str
    to_node_key: str
    condition: str = EdgeCondition.SUCCESS.value
    priority: int = 100
    metadata: JsonDict = field(default_factory=dict)

    @classmethod
    def parse(
        cls,
        raw: Any,
        *,
        path: str,
        valid_node_keys: Sequence[str],
    ) -> "WorkflowEdge":
        if not isinstance(raw, dict):
            raise ValidationError("%s must be an object" % path)
        from_key = str(raw.get("from_node_key") or "").strip()
        to_key = str(raw.get("to_node_key") or "").strip()
        valid_targets = set(valid_node_keys) | {""}
        if from_key not in valid_targets:
            raise ValidationError("%s.from_node_key does not match any node" % path)
        if to_key not in valid_targets:
            raise ValidationError("%s.to_node_key does not match any node" % path)
        condition = str(raw.get("condition") or EdgeCondition.SUCCESS.value).strip().lower()
        if condition not in EDGE_CONDITIONS:
            raise ValidationError(
                "%s.condition must be one of: %s"
                % (path, ", ".join(sorted(EDGE_CONDITIONS)))
            )
        return cls(
            from_node_key=from_key,
            to_node_key=to_key,
            condition=condition,
            priority=_int_value(raw.get("priority", 100), "%s.priority" % path),
            metadata=ensure_json_object(raw.get("metadata")),
        )

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        if not self.metadata:
            data.pop("metadata", None)
        return data


@dataclass(frozen=True)
class WorkflowDefinition:
    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge]
    metadata: JsonDict = field(default_factory=dict)
    schema_version: int = 1

    @classmethod
    def parse(cls, raw: Any) -> "WorkflowDefinition":
        if not isinstance(raw, dict):
            raise ValidationError("workflow definition must be an object")
        raw_nodes = raw.get("nodes")
        raw_edges = raw.get("edges")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValidationError("workflow definition.nodes must be a non-empty list")
        if not isinstance(raw_edges, list):
            raise ValidationError("workflow definition.edges must be a list")
        nodes: List[WorkflowNode] = []
        seen = set()
        for index, raw_node in enumerate(raw_nodes):
            node = WorkflowNode.parse(raw_node, path="definition.nodes[%d]" % index)
            if node.node_key in seen:
                raise ValidationError("duplicate workflow node_key: %s" % node.node_key)
            seen.add(node.node_key)
            nodes.append(node)
        node_keys = [node.node_key for node in nodes]
        edges = [
            WorkflowEdge.parse(
                raw_edge,
                path="definition.edges[%d]" % index,
                valid_node_keys=node_keys,
            )
            for index, raw_edge in enumerate(raw_edges)
        ]
        cls._validate_graph(node_keys, edges)
        return cls(
            nodes=nodes,
            edges=edges,
            metadata=ensure_json_object(raw.get("metadata")),
            schema_version=_positive_int(raw.get("schema_version", 1), "definition.schema_version"),
        )

    @staticmethod
    def _validate_graph(node_keys: Iterable[str], edges: List[WorkflowEdge]) -> None:
        keys = list(node_keys)
        start_edges = [edge for edge in edges if edge.from_node_key == ""]
        if len(start_edges) != 1:
            raise ValidationError(
                "workflow definition must have exactly one start edge (from_node_key=''); got %d"
                % len(start_edges)
            )
        inbound = {key: 0 for key in keys}
        for edge in edges:
            if edge.to_node_key:
                inbound[edge.to_node_key] = inbound.get(edge.to_node_key, 0) + 1
        for key in keys:
            if inbound.get(key, 0) == 0:
                raise ValidationError("workflow node %s is unreachable (no inbound edge)" % key)

    @staticmethod
    def find_cycles(node_keys: Iterable[str], edges: Iterable[WorkflowEdge]) -> List[List[str]]:
        """Return a list of simple cycles in the workflow DAG.

        Cycles are legitimate (e.g. review→fix→review rejection loops) but
        callers should pair them with finite max_attempts to avoid unbounded
        loops at runtime — see workflow_runtime._advance.
        """
        graph: Dict[str, List[str]] = {key: [] for key in node_keys}
        for edge in edges:
            if edge.from_node_key and edge.to_node_key and edge.from_node_key in graph:
                graph[edge.from_node_key].append(edge.to_node_key)
        cycles: List[List[str]] = []
        path: List[str] = []
        in_path: set = set()
        visited: set = set()

        def dfs(node: str) -> None:
            if node in in_path:
                start = path.index(node)
                cycles.append(path[start:] + [node])
                return
            if node in visited:
                return
            visited.add(node)
            in_path.add(node)
            path.append(node)
            for nxt in graph.get(node, []):
                dfs(nxt)
            path.pop()
            in_path.remove(node)

        for key in graph:
            if key not in visited:
                dfs(key)
        return cycles

    def to_dict(self) -> JsonDict:
        data: JsonDict = {
            "schema_version": self.schema_version,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }
        if self.metadata:
            data["metadata"] = self.metadata
        return data

    def task_preview(self, inherited_context: Optional[Dict[str, Any]] = None) -> JsonDict:
        context = ensure_json_object(inherited_context)
        dependencies_by_node: Dict[str, List[str]] = {node.node_key: [] for node in self.nodes}
        for edge in self.edges:
            if edge.from_node_key and edge.to_node_key:
                dependencies_by_node.setdefault(edge.to_node_key, []).append(edge.from_node_key)
        tasks = []
        for node in self.nodes:
            tasks.append(
                {
                    "node_key": node.node_key,
                    "title": node.node_key.replace("_", " ").replace("-", " ").strip().title(),
                    "instructions": node.instructions,
                    "node_type": node.node_type,
                    "role_required": node.role_required,
                    "required_capabilities": list(node.required_capabilities),
                    "max_attempts": node.max_attempts,
                    "timeout_minutes": node.timeout_minutes,
                    "dependencies": dependencies_by_node.get(node.node_key, []),
                    "metadata": {
                        **node.metadata,
                        "workflow_node_key": node.node_key,
                        "required_role": node.role_required,
                        "inherited_context": context,
                    },
                }
            )
        return {
            "schema": "mac.workflow.preview.v1",
            "task_count": len(tasks),
            "tasks": tasks,
            "edges": [edge.to_dict() for edge in self.edges],
            "context": context,
        }


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("required_capabilities must be a list")
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _int_value(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be an integer" % field)


def _positive_int(value: Any, field: str) -> int:
    number = _int_value(value, field)
    if number < 1:
        raise ValidationError("%s must be a positive integer" % field)
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    number = _int_value(value, field)
    if number < 0:
        raise ValidationError("%s must be zero or greater" % field)
    return number
