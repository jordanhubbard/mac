"""Focused branch-coverage tests for src/mac/workflow_models.py.

These exercise the pure parse/validation, serialization, cycle-detection, and
integer-coercion branches of the workflow graph model that were otherwise only
reachable through the higher-level workflow service. Keeping them here as direct
unit tests raises whole-repo branch-coverage headroom and pins the validation
contract of the model in isolation.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.workflow_models import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    _int_value,
    _nonnegative_int,
    _optional_string,
    _positive_int,
    _string_list,
)


def _valid_node(**overrides) -> dict:
    base = {"node_key": "investigate", "node_type": "task", "role_required": "qa"}
    base.update(overrides)
    return base


class TestWorkflowNodeParse:
    def test_rejects_non_dict(self):
        with pytest.raises(ValidationError, match="must be an object"):
            WorkflowNode.parse(["not", "a", "dict"], path="node")

    def test_requires_node_key(self):
        with pytest.raises(ValidationError, match="node_key is required"):
            WorkflowNode.parse({"node_key": "  "}, path="node")

    def test_rejects_unknown_node_type(self):
        with pytest.raises(ValidationError, match="node_type must be one of"):
            WorkflowNode.parse(_valid_node(node_type="bogus"), path="node")

    def test_requires_role_required(self):
        with pytest.raises(ValidationError, match="role_required is required"):
            WorkflowNode.parse(_valid_node(role_required=""), path="node")

    def test_parses_full_node_and_normalizes_type(self):
        node = WorkflowNode.parse(
            _valid_node(
                node_type="TASK",
                persona_hint=" analyst ",
                instructions=" do work ",
                max_attempts=3,
                timeout_minutes=15,
                required_capabilities=[" b ", "a", "a"],
                metadata={"k": "v"},
            ),
            path="node",
        )
        assert node.node_type == "task"
        assert node.persona_hint == "analyst"
        assert node.instructions == "do work"
        assert node.required_capabilities == ["a", "b"]
        assert node.metadata == {"k": "v"}


class TestWorkflowNodeToDict:
    def test_omits_defaulted_optional_fields(self):
        node = WorkflowNode(node_key="n", role_required="qa")
        data = node.to_dict()
        assert data == {
            "node_key": "n",
            "node_type": "task",
            "role_required": "qa",
            "max_attempts": 1,
        }
        for absent in (
            "persona_hint",
            "instructions",
            "timeout_minutes",
            "required_capabilities",
            "metadata",
        ):
            assert absent not in data

    def test_keeps_populated_optional_fields(self):
        node = WorkflowNode(
            node_key="n",
            role_required="qa",
            persona_hint="p",
            instructions="i",
            timeout_minutes=5,
            required_capabilities=["c"],
            metadata={"m": 1},
        )
        data = node.to_dict()
        assert data["persona_hint"] == "p"
        assert data["instructions"] == "i"
        assert data["timeout_minutes"] == 5
        assert data["required_capabilities"] == ["c"]
        assert data["metadata"] == {"m": 1}


class TestWorkflowEdgeParse:
    def test_rejects_non_dict(self):
        with pytest.raises(ValidationError, match="must be an object"):
            WorkflowEdge.parse("nope", path="edge", valid_node_keys=["a"])

    def test_rejects_unknown_from_node(self):
        with pytest.raises(ValidationError, match="from_node_key does not match"):
            WorkflowEdge.parse(
                {"from_node_key": "ghost", "to_node_key": "a"},
                path="edge",
                valid_node_keys=["a"],
            )

    def test_rejects_unknown_to_node(self):
        with pytest.raises(ValidationError, match="to_node_key does not match"):
            WorkflowEdge.parse(
                {"from_node_key": "", "to_node_key": "ghost"},
                path="edge",
                valid_node_keys=["a"],
            )

    def test_rejects_unknown_condition(self):
        with pytest.raises(ValidationError, match="condition must be one of"):
            WorkflowEdge.parse(
                {"from_node_key": "", "to_node_key": "a", "condition": "maybe"},
                path="edge",
                valid_node_keys=["a"],
            )

    def test_parses_and_normalizes_condition(self):
        edge = WorkflowEdge.parse(
            {"from_node_key": "", "to_node_key": "a", "condition": "SUCCESS", "priority": 5},
            path="edge",
            valid_node_keys=["a"],
        )
        assert edge.condition == "success"
        assert edge.priority == 5

    def test_to_dict_omits_empty_metadata(self):
        edge = WorkflowEdge(from_node_key="", to_node_key="a")
        assert "metadata" not in edge.to_dict()
        edge_meta = WorkflowEdge(from_node_key="", to_node_key="a", metadata={"x": 1})
        assert edge_meta.to_dict()["metadata"] == {"x": 1}


def _definition(nodes, edges, **extra) -> dict:
    payload = {"nodes": nodes, "edges": edges}
    payload.update(extra)
    return payload


class TestWorkflowDefinitionParse:
    def test_rejects_non_dict(self):
        with pytest.raises(ValidationError, match="definition must be an object"):
            WorkflowDefinition.parse("nope")

    def test_rejects_empty_nodes(self):
        with pytest.raises(ValidationError, match="nodes must be a non-empty list"):
            WorkflowDefinition.parse(_definition([], [{"from_node_key": "", "to_node_key": "a"}]))

    def test_rejects_non_list_edges(self):
        with pytest.raises(ValidationError, match="edges must be a list"):
            WorkflowDefinition.parse(_definition([_valid_node()], edges="nope"))

    def test_rejects_duplicate_node_keys(self):
        with pytest.raises(ValidationError, match="duplicate workflow node_key"):
            WorkflowDefinition.parse(
                _definition(
                    [_valid_node(node_key="a"), _valid_node(node_key="a")],
                    [{"from_node_key": "", "to_node_key": "a"}],
                )
            )

    def test_requires_exactly_one_start_edge(self):
        with pytest.raises(ValidationError, match="exactly one start edge"):
            WorkflowDefinition.parse(_definition([_valid_node(node_key="a")], []))

    def test_rejects_unreachable_node(self):
        with pytest.raises(ValidationError, match="unreachable"):
            WorkflowDefinition.parse(
                _definition(
                    [_valid_node(node_key="a"), _valid_node(node_key="b")],
                    [{"from_node_key": "", "to_node_key": "a"}],
                )
            )

    def test_parses_valid_definition_round_trips(self):
        definition = WorkflowDefinition.parse(
            _definition(
                [_valid_node(node_key="a"), _valid_node(node_key="b")],
                [
                    {"from_node_key": "", "to_node_key": "a"},
                    {"from_node_key": "a", "to_node_key": "b"},
                ],
                metadata={"owner": "team"},
                schema_version=2,
            )
        )
        as_dict = definition.to_dict()
        assert as_dict["schema_version"] == 2
        assert as_dict["metadata"] == {"owner": "team"}
        assert [n["node_key"] for n in as_dict["nodes"]] == ["a", "b"]

    def test_to_dict_omits_empty_metadata(self):
        definition = WorkflowDefinition.parse(
            _definition(
                [_valid_node(node_key="a")],
                [{"from_node_key": "", "to_node_key": "a"}],
            )
        )
        assert "metadata" not in definition.to_dict()


class TestFindCycles:
    def _edge(self, src, dst):
        return WorkflowEdge(from_node_key=src, to_node_key=dst)

    def test_detects_simple_cycle(self):
        cycles = WorkflowDefinition.find_cycles(
            ["a", "b"],
            [self._edge("a", "b"), self._edge("b", "a")],
        )
        assert cycles
        assert cycles[0][0] == cycles[0][-1]

    def test_no_cycle_for_dag(self):
        cycles = WorkflowDefinition.find_cycles(
            ["a", "b", "c"],
            [self._edge("a", "b"), self._edge("b", "c")],
        )
        assert cycles == []

    def test_ignores_edges_outside_declared_nodes(self):
        cycles = WorkflowDefinition.find_cycles(
            ["a"],
            [self._edge("a", "ghost"), self._edge("", "a")],
        )
        assert cycles == []


class TestTaskPreview:
    def test_preview_includes_dependencies_and_context(self):
        definition = WorkflowDefinition.parse(
            _definition(
                [_valid_node(node_key="a"), _valid_node(node_key="b_node")],
                [
                    {"from_node_key": "", "to_node_key": "a"},
                    {"from_node_key": "a", "to_node_key": "b_node"},
                ],
            )
        )
        preview = definition.task_preview({"origin": "unit"})
        assert preview["task_count"] == 2
        by_key = {t["node_key"]: t for t in preview["tasks"]}
        assert by_key["b_node"]["dependencies"] == ["a"]
        assert by_key["b_node"]["title"] == "B Node"
        assert by_key["a"]["metadata"]["inherited_context"] == {"origin": "unit"}

    def test_preview_defaults_context_to_empty_dict(self):
        definition = WorkflowDefinition.parse(
            _definition(
                [_valid_node(node_key="a")],
                [{"from_node_key": "", "to_node_key": "a"}],
            )
        )
        preview = definition.task_preview()
        assert preview["context"] == {}


class TestHelperCoercions:
    def test_optional_string_variants(self):
        assert _optional_string(None) is None
        assert _optional_string("  ") is None
        assert _optional_string(" hi ") == "hi"

    def test_string_list_variants(self):
        assert _string_list(None) == []
        assert _string_list([" b ", "a", "a", "  "]) == ["a", "b"]
        with pytest.raises(ValidationError, match="required_capabilities must be a list"):
            _string_list("not-a-list")

    def test_int_value_rejects_non_int(self):
        with pytest.raises(ValidationError, match="must be an integer"):
            _int_value("abc", "field")
        assert _int_value("7", "field") == 7

    def test_positive_int_floor(self):
        assert _positive_int(1, "field") == 1
        with pytest.raises(ValidationError, match="must be a positive integer"):
            _positive_int(0, "field")

    def test_nonnegative_int_floor(self):
        assert _nonnegative_int(0, "field") == 0
        with pytest.raises(ValidationError, match="zero or greater"):
            _nonnegative_int(-1, "field")
