"""Pure, deterministic contracts for durable parallel work packages.

This module deliberately has no store, clock, UUID, or control-plane service
dependency.  The same proposed plan therefore compiles to the same canonical
definition and digest in an API process, a worker, or an offline audit.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from mac.models import JsonDict, ValidationError, json_dumps


WORK_PACKAGE_PLAN_SCHEMA = "mac.work_package.plan.v1"
WORK_PACKAGE_PLAN_DIGEST_ALGORITHM = "sha256"
WORK_PACKAGE_MAX_NODES = 100
WORK_PACKAGE_MAX_DEPENDENCIES = 100
WORK_PACKAGE_MAX_EFFECTS_PER_KIND = 500
WORK_PACKAGE_MAX_OUTPUTS = 100
WORK_PACKAGE_COMPILER_VERSION = "work-package-compiler-v1"
WORK_PACKAGE_POLICY_VERSION = "work-package-policy-v2"

_NODE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_NODE_TYPES = {"analysis", "mutation", "integration", "certification"}
_NODE_TYPE_ALIASES = {
    "task": "mutation",
    "plan": "analysis",
    "approval": "certification",
    "commit": "integration",
    "verify": "certification",
}
_TOP_LEVEL_FIELDS = {
    "schema",
    "goal",
    "package_id",
    "project",
    "repository_id",
    "resource_namespace",
    "planning_base_ref",
    "planning_base_sha",
    "plan_generation",
    "max_in_flight",
    "max_mutation_wip",
    "mutation_wip",
    "integration",
    "metadata",
    "nodes",
}
_NODE_FIELDS = {
    "node_key",
    "node_id",
    "id",
    "key",
    "title",
    "description",
    "instructions",
    "node_type",
    "kind",
    "depends_on",
    "dependencies",
    "external_dependencies",
    "inputs",
    "priority",
    "required_capabilities",
    "capabilities",
    "max_attempts",
    "effects",
    "expected_outputs",
    "verification",
    "estimate",
    "estimates",
    "rework",
    "scope_confidence",
    "metadata",
}


@dataclass(frozen=True)
class WorkPackageEffects:
    """Resources a node observes or changes, used by allocation policy."""

    reads: Tuple[str, ...] = ()
    writes: Tuple[str, ...] = ()
    exclusive: Tuple[str, ...] = ()
    external: Tuple[str, ...] = ()
    external_contract: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "reads": list(self.reads),
            "writes": list(self.writes),
            "exclusive": list(self.exclusive),
            "external": list(self.external),
            "external_contract": _copy_json_object(self.external_contract or {}),
        }


@dataclass(frozen=True)
class WorkPackageNodeSpec:
    node_key: str
    title: str
    description: str
    node_type: str
    depends_on: Tuple[str, ...]
    external_dependencies: Tuple[JsonDict, ...]
    input_lineage_status: str
    carry_forward_eligible: bool
    priority: int
    required_capabilities: Tuple[str, ...]
    max_attempts: int
    effects: WorkPackageEffects
    inputs: Tuple[JsonDict, ...]
    expected_outputs: Tuple[JsonDict, ...]
    verification: JsonDict
    estimates: JsonDict
    rework: JsonDict
    scope_confidence: float
    metadata: JsonDict
    effects_digest: str
    contract_digest: str
    input_digest: str

    def to_dict(self) -> JsonDict:
        return {
            "node_key": self.node_key,
            "title": self.title,
            "description": self.description,
            "kind": self.node_type,
            "depends_on": list(self.depends_on),
            "external_dependencies": [
                _copy_json_object(item) for item in self.external_dependencies
            ],
            "input_lineage_status": self.input_lineage_status,
            "carry_forward_eligible": self.carry_forward_eligible,
            "priority": self.priority,
            "required_capabilities": list(self.required_capabilities),
            "effects": self.effects.to_dict(),
            "inputs": [_copy_json_object(item) for item in self.inputs],
            "expected_outputs": [
                _copy_json_object(item) for item in self.expected_outputs
            ],
            "verification": _copy_json_object(self.verification),
            "estimates": _copy_json_object(self.estimates),
            "rework": _copy_json_object(self.rework),
            "metadata": _copy_json_object(self.metadata),
            "effects_digest": self.effects_digest,
            "contract_digest": self.contract_digest,
            "input_digest": self.input_digest,
        }


@dataclass(frozen=True)
class _TopologyNode:
    """Minimal canonical node shape used to revalidate persisted plans."""

    node_key: str
    node_type: str
    depends_on: Tuple[str, ...]


@dataclass(frozen=True)
class CompiledWorkPackagePlan:
    """Canonical plan plus task materialization inputs in DAG order."""

    definition: JsonDict
    plan_digest: str
    topological_order: Tuple[str, ...]
    task_specs: Tuple[WorkPackageNodeSpec, ...]
    levels: JsonDict
    critical_path_rank: JsonDict
    conflict_domains: JsonDict
    integration_groups: Tuple[JsonDict, ...]
    capacity_scopes: JsonDict
    materialization_map: JsonDict

    def to_dict(self) -> JsonDict:
        return {
            "definition": _copy_json_object(self.definition),
            "plan_digest": self.plan_digest,
            "topological_order": list(self.topological_order),
            "task_specs": [spec.to_dict() for spec in self.task_specs],
            "levels": _copy_json_object(self.levels),
            "critical_path_rank": _copy_json_object(self.critical_path_rank),
            "conflict_domains": _copy_json_object(self.conflict_domains),
            "integration_groups": [
                _copy_json_object(group) for group in self.integration_groups
            ],
            "capacity_scopes": _copy_json_object(self.capacity_scopes),
            "materialization_map": _copy_json_object(self.materialization_map),
        }


def compile_work_package_plan(raw: Mapping[str, Any]) -> CompiledWorkPackagePlan:
    """Validate and canonicalize a work DAG without reading mutable state.

    Internal dependencies are symbolic node keys.  Existing task dependencies
    must be declared separately in ``external_dependencies`` so a later
    transactional materializer cannot accidentally confuse the two domains.
    """

    if not isinstance(raw, Mapping):
        raise ValidationError("work package plan must be an object")
    unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise ValidationError(
            "work package plan contains unknown fields: %s" % ", ".join(unknown)
        )
    schema = str(raw.get("schema") or WORK_PACKAGE_PLAN_SCHEMA).strip()
    if schema != WORK_PACKAGE_PLAN_SCHEMA:
        raise ValidationError(
            "work package plan.schema must be %s" % WORK_PACKAGE_PLAN_SCHEMA
        )
    goal = _required_string(raw.get("goal"), "work package plan.goal", maximum=4000)
    package_id = _required_string(
        raw.get("package_id"), "work package plan.package_id", maximum=240
    )
    project = _optional_string(
        raw.get("project"), "work package plan.project", maximum=240
    )
    repository_id = _required_string(
        raw.get("repository_id"),
        "work package plan.repository_id",
        maximum=240,
    )
    planning_base_ref = _normalize_ref(
        raw.get("planning_base_ref"), "work package plan.planning_base_ref"
    )
    planning_base_sha = _normalize_sha(
        raw.get("planning_base_sha"), "work package plan.planning_base_sha"
    )
    resource_namespace = _normalize_resource_namespace(raw.get("resource_namespace"))
    plan_generation = _bounded_int(
        raw.get("plan_generation"),
        "work package plan.plan_generation",
        minimum=1,
        maximum=2_147_483_647,
    )
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValidationError("work package plan.nodes must be a non-empty list")
    if len(raw_nodes) > WORK_PACKAGE_MAX_NODES:
        raise ValidationError(
            "work package plan.nodes may contain at most %d nodes"
            % WORK_PACKAGE_MAX_NODES
        )

    parsed: Dict[str, WorkPackageNodeSpec] = {}
    for index, raw_node in enumerate(raw_nodes):
        node = _parse_node(
            raw_node,
            index=index,
            resource_namespace=resource_namespace,
        )
        if node.node_key in parsed:
            raise ValidationError("duplicate work package node_key: %s" % node.node_key)
        parsed[node.node_key] = node

    for node in parsed.values():
        for dependency in node.depends_on:
            if dependency == node.node_key:
                raise ValidationError(
                    "work package node %s cannot depend on itself" % node.node_key
                )
            if dependency not in parsed:
                raise ValidationError(
                    "work package node %s dependency %s does not reference a planned node"
                    % (node.node_key, dependency)
                )

    order = _topological_order(parsed)
    _validate_flat_mutation_wave(parsed, order)
    _validate_integration_fan_in(parsed, order)
    _validate_external_waves(parsed, order)
    digested: Dict[str, WorkPackageNodeSpec] = {}
    for key in order:
        node = parsed[key]
        effects_digest = _digest(node.effects.to_dict())
        node = replace(node, effects_digest=effects_digest)
        contract_digest = _digest(_node_contract_payload(node))
        input_digest = _digest(
            {
                "dependency_lineage": sorted(
                    "%s+%s"
                    % (
                        digested[dependency].contract_digest,
                        digested[dependency].input_digest,
                    )
                    for dependency in node.depends_on
                ),
                "external_dependencies": [
                    _copy_json_object(item) for item in node.external_dependencies
                ],
            }
        )
        carry_forward_eligible = all(
            digested[dependency].carry_forward_eligible
            for dependency in node.depends_on
        ) and all(
            dependency["lineage_status"] == "resolved"
            for dependency in node.external_dependencies
        )
        digested[key] = replace(
            node,
            contract_digest=contract_digest,
            input_digest=input_digest,
            input_lineage_status=(
                "resolved" if carry_forward_eligible else "unresolved"
            ),
            carry_forward_eligible=carry_forward_eligible,
        )
    ordered_nodes = tuple(digested[key] for key in order)
    max_in_flight = _bounded_int(
        raw.get("max_in_flight", min(4, len(ordered_nodes))),
        "work package plan.max_in_flight",
        minimum=1,
        maximum=WORK_PACKAGE_MAX_NODES,
    )
    mutation_wip = _normalize_mutation_wip(raw, ordered_nodes)
    integration = _normalize_integration(raw.get("integration"))
    metadata = _json_object(raw.get("metadata"), "work package plan.metadata")

    definition: JsonDict = {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "compiler_version": WORK_PACKAGE_COMPILER_VERSION,
        "policy_version": WORK_PACKAGE_POLICY_VERSION,
        "goal": goal,
        "package_id": package_id,
        "project": project,
        "repository_id": repository_id,
        "resource_namespace": resource_namespace,
        "planning_base_ref": planning_base_ref,
        "planning_base_sha": planning_base_sha,
        "plan_generation": plan_generation,
        "max_in_flight": max_in_flight,
        "mutation_wip": mutation_wip,
        "integration": integration,
        "metadata": metadata,
        "nodes": [node.to_dict() for node in ordered_nodes],
    }
    levels = _topological_levels(ordered_nodes)
    critical_path_rank = _critical_path_ranks(ordered_nodes, order)
    conflict_domains = _conflict_domains(ordered_nodes)
    integration_groups = _integration_groups(ordered_nodes)
    capacity_scopes = _capacity_scopes(ordered_nodes)
    definition["derived"] = {
        "levels": levels,
        "critical_path_rank": critical_path_rank,
        "conflict_domains": conflict_domains,
        "integration_groups": list(integration_groups),
        "capacity_scopes": capacity_scopes,
    }
    canonical = json_dumps(definition)
    digest = "%s:%s" % (
        WORK_PACKAGE_PLAN_DIGEST_ALGORITHM,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
    materialization_map = {
        node.node_key: {
            "task_id": "task_wp_%s"
            % hashlib.sha256(
                (package_id + "\x00" + digest + "\x00" + node.node_key).encode("utf-8")
            ).hexdigest()[:24],
            "node_generation": plan_generation,
            "contract_digest": node.contract_digest,
            "input_digest": node.input_digest,
            "input_lineage_status": node.input_lineage_status,
            "carry_forward_eligible": node.carry_forward_eligible,
            "declared_effects_digest": node.effects_digest,
        }
        for node in ordered_nodes
    }
    return CompiledWorkPackagePlan(
        definition=_copy_json_object(definition),
        plan_digest=digest,
        topological_order=order,
        task_specs=ordered_nodes,
        levels=levels,
        critical_path_rank=critical_path_rank,
        conflict_domains=conflict_domains,
        integration_groups=integration_groups,
        capacity_scopes=capacity_scopes,
        materialization_map=materialization_map,
    )


def validate_supported_work_package_topology(definition: Mapping[str, Any]) -> None:
    """Revalidate the execution-safety envelope of a persisted canonical plan.

    Admission always runs the full compiler.  Activation and claim use this
    narrower validator as a rolling-upgrade fence for plans persisted by an
    older compiler: no already-admitted graph may bypass a newly tightened
    repository-lineage invariant.
    """

    if not isinstance(definition, Mapping):
        raise ValidationError("persisted work package definition must be an object")
    raw_nodes = definition.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValidationError(
            "persisted work package definition must contain canonical nodes"
        )
    parsed: Dict[str, _TopologyNode] = {}
    for index, raw_node in enumerate(raw_nodes):
        path = "persisted work package definition.nodes[%d]" % index
        if not isinstance(raw_node, Mapping):
            raise ValidationError("%s must be an object" % path)
        node_key = str(raw_node.get("node_key") or "").strip()
        if not node_key or not _NODE_KEY_RE.fullmatch(node_key):
            raise ValidationError("%s has no canonical node_key" % path)
        if node_key in parsed:
            raise ValidationError(
                "persisted work package definition has duplicate node_key: %s"
                % node_key
            )
        node_type = str(raw_node.get("kind") or "").strip()
        if node_type not in _NODE_TYPES:
            raise ValidationError("%s has no canonical kind" % path)
        raw_dependencies = raw_node.get("depends_on")
        if not isinstance(raw_dependencies, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in raw_dependencies
        ):
            raise ValidationError("%s has no canonical depends_on list" % path)
        depends_on = tuple(str(value).strip() for value in raw_dependencies)
        if len(set(depends_on)) != len(depends_on):
            raise ValidationError("%s has duplicate dependencies" % path)
        parsed[node_key] = _TopologyNode(
            node_key=node_key,
            node_type=node_type,
            depends_on=depends_on,
        )
    for node in parsed.values():
        for dependency in node.depends_on:
            if dependency == node.node_key:
                raise ValidationError(
                    "work package node %s cannot depend on itself" % node.node_key
                )
            if dependency not in parsed:
                raise ValidationError(
                    "work package node %s dependency %s does not reference a planned node"
                    % (node.node_key, dependency)
                )
    order = _topological_order(parsed)
    _validate_flat_mutation_wave(parsed, order)


def validate_executable_work_package_effects(
    compiled: CompiledWorkPackagePlan,
) -> None:
    """Fail closed until external actions have controller-owned fencing.

    An idempotency-key declaration is planning metadata, not authority at the
    target system.  Workers are lease-fenced only at the hub and can continue
    an external action after partition or lease expiry.  Managed execution
    therefore rejects such nodes until a controller outbox/effector can bind
    the declared key to the target system's real idempotency or fencing API.
    """

    external_nodes = tuple(
        node.node_key for node in compiled.task_specs if node.effects.external
    )
    if external_nodes:
        raise ValidationError(
            "executable external effects require a controller-owned fenced "
            "effector and are not currently dispatchable: %s"
            % ", ".join(external_nodes)
        )


def work_package_effect_conflicts(
    left: WorkPackageEffects,
    right: WorkPackageEffects,
) -> List[str]:
    """Return deterministic conflict reasons for two declared effect sets.

    Read/read overlap is safe.  Writes conflict with reads or writes, exclusive
    resources conflict with any local access, and external effects serialize
    against the same external resource.  Resource tokens are intentionally
    opaque: planners can use paths, subsystems, database tables, or service
    names without teaching the compiler domain-specific alias rules.
    """

    reasons = set()
    for resource in _overlapping_resources(
        left.writes, right.reads + right.writes + right.exclusive
    ):
        reasons.add("write:%s" % resource)
    for resource in _overlapping_resources(
        right.writes, left.reads + left.writes + left.exclusive
    ):
        reasons.add("write:%s" % resource)
    for resource in _overlapping_resources(
        left.exclusive, right.reads + right.writes + right.exclusive
    ):
        reasons.add("exclusive:%s" % resource)
    for resource in _overlapping_resources(
        right.exclusive, left.reads + left.writes + left.exclusive
    ):
        reasons.add("exclusive:%s" % resource)
    for resource in _overlapping_resources(left.external, right.external):
        reasons.add("external:%s" % resource)
    for reason in tuple(reasons):
        if reason.startswith("exclusive:"):
            reasons.discard("write:%s" % reason.split(":", 1)[1])
    return sorted(reasons)


def _parse_node(
    raw: Any,
    *,
    index: int,
    resource_namespace: Mapping[str, Any],
) -> WorkPackageNodeSpec:
    path = "work package plan.nodes[%d]" % index
    if not isinstance(raw, Mapping):
        raise ValidationError("%s must be an object" % path)
    unknown = sorted(set(raw) - _NODE_FIELDS)
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    for aliases in (
        ("node_key", "node_id", "id", "key"),
        ("description", "instructions"),
        ("node_type", "kind"),
        ("depends_on", "dependencies"),
        ("required_capabilities", "capabilities"),
        ("estimate", "estimates"),
        ("max_attempts", "rework"),
    ):
        present = [name for name in aliases if name in raw]
        if len(present) > 1:
            raise ValidationError(
                "%s may set only one of: %s" % (path, ", ".join(aliases))
            )
    node_key = _required_string(
        raw.get("node_key") or raw.get("node_id") or raw.get("id") or raw.get("key"),
        "%s.node_key" % path,
        maximum=64,
    )
    if not _NODE_KEY_RE.fullmatch(node_key):
        raise ValidationError(
            "%s.node_key must match %s" % (path, _NODE_KEY_RE.pattern)
        )
    title = _required_string(raw.get("title"), "%s.title" % path, maximum=240)
    description = (
        _optional_string(
            raw.get("description", raw.get("instructions")),
            "%s.description" % path,
            maximum=100_000,
        )
        or ""
    )
    raw_type = (
        str(raw.get("node_type") or raw.get("kind") or "mutation").strip().lower()
    )
    node_type = _NODE_TYPE_ALIASES.get(raw_type, raw_type)
    if node_type not in _NODE_TYPES:
        raise ValidationError(
            "%s.node_type must be one of: %s" % (path, ", ".join(sorted(_NODE_TYPES)))
        )
    depends_on = _string_tuple(
        raw.get("depends_on", raw.get("dependencies")),
        "%s.depends_on" % path,
        maximum=WORK_PACKAGE_MAX_DEPENDENCIES,
    )
    external_dependencies = _parse_external_dependencies(
        raw.get("external_dependencies"),
        path="%s.external_dependencies" % path,
    )
    capabilities = _string_tuple(
        raw.get("required_capabilities", raw.get("capabilities")),
        "%s.required_capabilities" % path,
        maximum=WORK_PACKAGE_MAX_DEPENDENCIES,
    )
    controller_station_capability = {
        "integration": "work_package_integrator_v1",
        "certification": "work_package_certifier_v1",
    }.get(node_type)
    capabilities = tuple(
        sorted(
            set(capabilities)
            | {"work_package_v1"}
            | ({controller_station_capability} if controller_station_capability else set())
        )
    )
    effects = _parse_effects(
        raw.get("effects"),
        path="%s.effects" % path,
        resource_namespace=resource_namespace,
    )
    if node_type == "mutation" and resource_namespace["status"] == "unresolved":
        effects = replace(
            effects,
            exclusive=tuple(sorted(set(effects.exclusive) | {"repo:*"})),
        )
    inputs = _parse_contract_items(raw.get("inputs"), path="%s.inputs" % path)
    expected_outputs = _parse_outputs(
        raw.get("expected_outputs"), path="%s.expected_outputs" % path
    )
    verification = _parse_verification(
        raw.get("verification"), path="%s.verification" % path
    )
    expected_profile = {
        "analysis": "analysis-default",
        "mutation": "repository-default",
        "integration": "integration-default",
        "certification": "certification-default",
    }[node_type]
    if verification and verification.get("profile") != expected_profile:
        raise ValidationError(
            "%s verification profile for %s nodes must be %s"
            % (path, node_type, expected_profile)
        )
    estimates, scope_confidence = _parse_estimates(
        raw.get("estimates", raw.get("estimate")),
        legacy_scope_confidence=raw.get("scope_confidence"),
        path="%s.estimates" % path,
    )
    rework = _parse_rework(
        raw.get("rework"),
        legacy_max_attempts=raw.get("max_attempts"),
        path="%s.rework" % path,
    )
    if node_type == "mutation":
        if not (effects.writes or effects.exclusive or effects.external):
            raise ValidationError(
                "%s mutation node must declare a write, exclusive, or external effect"
                % path
            )
        if not expected_outputs:
            raise ValidationError(
                "%s mutation node must declare at least one expected output" % path
            )
        if not verification:
            raise ValidationError(
                "%s mutation node must declare a verification contract" % path
            )
        if scope_confidence < 0.5 and "*" not in effects.exclusive:
            raise ValidationError(
                "%s low-confidence mutation must declare exclusive effect '*'" % path
            )
    elif not expected_outputs or not verification:
        raise ValidationError(
            "%s node must declare meaningful expected_outputs and verification" % path
        )
    return WorkPackageNodeSpec(
        node_key=node_key,
        title=title,
        description=description,
        node_type=node_type,
        depends_on=depends_on,
        external_dependencies=external_dependencies,
        input_lineage_status="unresolved",
        carry_forward_eligible=False,
        priority=_bounded_int(
            raw.get("priority", 0),
            "%s.priority" % path,
            minimum=-1_000_000,
            maximum=1_000_000,
        ),
        required_capabilities=capabilities,
        max_attempts=int(rework["max_cycles"]) + 1,
        effects=effects,
        inputs=inputs,
        expected_outputs=expected_outputs,
        verification=verification,
        estimates=estimates,
        rework=rework,
        scope_confidence=scope_confidence,
        metadata=_json_object(raw.get("metadata"), "%s.metadata" % path),
        effects_digest="",
        contract_digest="",
        input_digest="",
    )


def _parse_effects(
    raw: Any,
    *,
    path: str,
    resource_namespace: Mapping[str, Any],
) -> WorkPackageEffects:
    value = _json_object(raw, path)
    known = {
        "reads",
        "writes",
        "exclusive",
        "external",
        "external_effects",
        "external_contract",
    }
    unknown = sorted(set(value) - known)
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    if "external" in value and "external_effects" in value:
        raise ValidationError("%s cannot set both external and external_effects" % path)
    external = _effect_tuple(
        value.get("external", value.get("external_effects")),
        "%s.external" % path,
        maximum=WORK_PACKAGE_MAX_EFFECTS_PER_KIND,
        resource_namespace=resource_namespace,
        repository_path=False,
    )
    external_contract = _json_object(
        value.get("external_contract"), "%s.external_contract" % path
    )
    if external:
        unknown_contract = sorted(
            set(external_contract) - {"idempotency_key", "exclusive"}
        )
        if unknown_contract:
            raise ValidationError(
                "%s.external_contract contains unknown fields: %s"
                % (path, ", ".join(unknown_contract))
            )
        idempotency_key = _required_string(
            external_contract.get("idempotency_key"),
            "%s.external_contract.idempotency_key" % path,
            maximum=240,
        )
        if external_contract.get("exclusive") is not True:
            raise ValidationError(
                "%s external effects require external_contract.exclusive=true" % path
            )
        external_contract = {
            "idempotency_key": idempotency_key,
            "exclusive": True,
        }
    elif external_contract:
        raise ValidationError(
            "%s.external_contract requires at least one external effect" % path
        )
    return WorkPackageEffects(
        reads=_effect_tuple(
            value.get("reads"),
            "%s.reads" % path,
            maximum=WORK_PACKAGE_MAX_EFFECTS_PER_KIND,
            resource_namespace=resource_namespace,
            repository_path=True,
        ),
        writes=_effect_tuple(
            value.get("writes"),
            "%s.writes" % path,
            maximum=WORK_PACKAGE_MAX_EFFECTS_PER_KIND,
            resource_namespace=resource_namespace,
            repository_path=True,
        ),
        exclusive=_effect_tuple(
            value.get("exclusive"),
            "%s.exclusive" % path,
            maximum=WORK_PACKAGE_MAX_EFFECTS_PER_KIND,
            resource_namespace=resource_namespace,
            repository_path=True,
        ),
        external=external,
        external_contract=external_contract,
    )


def _parse_verification(raw: Any, *, path: str) -> JsonDict:
    value = _json_object(raw, path)
    if not value:
        return {}
    unknown = sorted(
        set(value)
        - {
            "profile",
            "command",
            "commands",
            "required_evidence",
            "timeout_seconds",
            "metadata",
        }
    )
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    if "command" in value and "commands" in value:
        raise ValidationError("%s cannot set both command and commands" % path)
    commands: Tuple[str, ...] = ()
    if "command" in value:
        commands = (_verification_command(value["command"], "%s.command" % path),)
    elif "commands" in value:
        raw_commands = value["commands"]
        if not isinstance(raw_commands, list) or not raw_commands:
            raise ValidationError("%s.commands must be a non-empty list" % path)
        if len(raw_commands) > 20:
            raise ValidationError("%s.commands may contain at most 20 items" % path)
        commands = tuple(
            _verification_command(command, "%s.commands[%d]" % (path, index))
            for index, command in enumerate(raw_commands)
        )
    required_evidence = _string_tuple(
        value.get("required_evidence"),
        "%s.required_evidence" % path,
        maximum=50,
    )
    profile = _required_string(value.get("profile"), "%s.profile" % path, maximum=120)
    if profile not in {
        "repository-default",
        "analysis-default",
        "integration-default",
        "certification-default",
    }:
        raise ValidationError("%s.profile is not a trusted verification profile" % path)
    result: JsonDict = {
        "profile": profile,
        "commands": list(commands),
        "command_authority": "advisory",
        "policy_resolution": "required",
        "required_evidence": list(required_evidence),
        "timeout_seconds": _bounded_int(
            value.get("timeout_seconds", 3600),
            "%s.timeout_seconds" % path,
            minimum=1,
            maximum=86_400,
        ),
        "metadata": _json_object(value.get("metadata"), "%s.metadata" % path),
    }
    return result


def _verification_command(value: Any, field_name: str) -> str:
    command = _required_string(value, field_name, maximum=2000)
    if "\x00" in command or "\n" in command or "\r" in command:
        raise ValidationError(
            "%s must be one bounded, single-line command" % field_name
        )
    normalized = " ".join(command.lower().split())
    if normalized in {"true", ":", "exit 0", "echo", "printf", "test 1 = 1"}:
        raise ValidationError("%s may not be a no-op verification command" % field_name)
    if re.search(r"[;&|<>`]", command) or "$(" in command or "${" in command:
        raise ValidationError(
            "%s is advisory only and may not contain shell metacharacters" % field_name
        )
    if re.search(
        r"(?:^|\s)(?:curl|wget|ssh|scp|sftp|nc|ncat|telnet)(?:\s|$)",
        normalized,
    ) or re.search(
        r"(?:^|\s)git\s+(?:push|pull|fetch|clone|remote)(?:\s|$)",
        normalized,
    ):
        raise ValidationError(
            "%s is advisory only and may not perform network operations" % field_name
        )
    if re.search(
        r"(?:^|\s)(?:rm|mv|cp|install|chmod|chown|sudo|tee|touch)(?:\s|$)",
        normalized,
    ):
        raise ValidationError(
            "%s is advisory only and may not perform direct mutations" % field_name
        )
    return command


def _normalize_integration(raw: Any) -> JsonDict:
    path = "work package plan.integration"
    value = _json_object(raw, path)
    allowed = {
        "mode",
        "strategy",
        "required",
        "worker_completion",
        "target_ref",
        "metadata",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    result = _copy_json_object(value)
    result["mode"] = _required_string(
        value.get("mode") or "package_batch", "%s.mode" % path, maximum=80
    )
    completion = _required_string(
        value.get("worker_completion") or "candidate_submission",
        "%s.worker_completion" % path,
        maximum=80,
    )
    if completion != "candidate_submission":
        raise ValidationError(
            "%s.worker_completion must be candidate_submission" % path
        )
    result["worker_completion"] = completion
    required = _boolean(value.get("required", True), "%s.required" % path)
    if not required:
        raise ValidationError("%s.required must be true" % path)
    result["required"] = True
    if "target_ref" in value:
        result["target_ref"] = _normalize_ref(
            value["target_ref"], "%s.target_ref" % path
        )
    if "metadata" in value:
        result["metadata"] = _json_object(value["metadata"], "%s.metadata" % path)
    return result


def _normalize_mutation_wip(
    raw: Mapping[str, Any], nodes: Tuple[WorkPackageNodeSpec, ...]
) -> JsonDict:
    path = "work package plan.mutation_wip"
    value = _json_object(raw.get("mutation_wip"), path)
    unknown = sorted(
        set(value)
        - {
            "max_tokens",
            "transfer_on",
            "release_condition",
            "fan_in_reservation",
            "metadata",
        }
    )
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    if "max_mutation_wip" in raw and "max_tokens" in value:
        raise ValidationError(
            "work package plan cannot set both max_mutation_wip and mutation_wip.max_tokens"
        )
    max_tokens = _bounded_int(
        raw.get("max_mutation_wip", value.get("max_tokens", 1)),
        "%s.max_tokens" % path,
        minimum=1,
        maximum=WORK_PACKAGE_MAX_NODES,
    )
    release_condition = _required_string(
        value.get("release_condition") or "candidate_resolved",
        "%s.release_condition" % path,
        maximum=80,
    )
    if release_condition != "candidate_resolved":
        raise ValidationError("%s.release_condition must be candidate_resolved" % path)
    transfer_on = _required_string(
        value.get("transfer_on") or "candidate_submission",
        "%s.transfer_on" % path,
        maximum=80,
    )
    if transfer_on != "candidate_submission":
        raise ValidationError("%s.transfer_on must be candidate_submission" % path)
    fan_in_reservation = _boolean(
        value.get("fan_in_reservation", True),
        "%s.fan_in_reservation" % path,
    )
    max_fan_in = max((len(node.depends_on) for node in nodes), default=0)
    if max_fan_in > max_tokens and not fan_in_reservation:
        raise ValidationError(
            "%s.fan_in_reservation must be true when DAG fan-in exceeds max_tokens"
            % path
        )
    return {
        "max_tokens": max_tokens,
        "transfer_on": transfer_on,
        "release_condition": release_condition,
        "fan_in_reservation": fan_in_reservation,
        "max_fan_in": max_fan_in,
        "metadata": _json_object(value.get("metadata"), "%s.metadata" % path),
    }


def _normalize_resource_namespace(raw: Any) -> JsonDict:
    path = "work package plan.resource_namespace"
    value = _json_object(raw, path)
    unknown = sorted(
        set(value) - {"case_sensitive", "unicode_normalization", "symlink_resolution"}
    )
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    case_sensitive = _boolean(
        value.get("case_sensitive", False), "%s.case_sensitive" % path
    )
    unicode_normalization = str(value.get("unicode_normalization") or "NFC").upper()
    if unicode_normalization not in {"NFC", "NFD", "NFKC", "NFKD"}:
        raise ValidationError(
            "%s.unicode_normalization must be NFC, NFD, NFKC, or NFKD" % path
        )
    symlink_resolution = (
        str(value.get("symlink_resolution") or "unresolved").strip().lower()
    )
    if symlink_resolution not in {"resolved", "unresolved"}:
        raise ValidationError(
            "%s.symlink_resolution must be resolved or unresolved" % path
        )
    status = (
        "resolved"
        if {
            "case_sensitive",
            "unicode_normalization",
            "symlink_resolution",
        }
        <= set(value)
        and symlink_resolution == "resolved"
        else "unresolved"
    )
    return {
        "status": status,
        "case_sensitive": case_sensitive,
        "unicode_normalization": unicode_normalization,
        "symlink_resolution": symlink_resolution,
        "conflict_policy": "exact" if status == "resolved" else "conservative",
    }


def _normalize_ref(value: Any, field_name: str) -> str:
    ref = _required_string(value, field_name, maximum=500)
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in ref)
        or ref.startswith(("-", ".", "/"))
        or ref.endswith((".", "/", ".lock"))
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or any(char in ref for char in " ~^:?*[\\")
    ):
        raise ValidationError("%s is not a safe repository ref" % field_name)
    return ref


def _normalize_sha(value: Any, field_name: str) -> str:
    sha = _required_string(value, field_name, maximum=64).lower()
    if len(sha) not in {40, 64} or not re.fullmatch(r"[0-9a-f]+", sha):
        raise ValidationError("%s must be a full 40- or 64-hex object id" % field_name)
    return sha


def _normalize_sha256_digest(value: Any, field_name: str) -> str:
    digest = _required_string(value, field_name, maximum=71).lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValidationError("%s must be a full sha256 digest" % field_name)
    return digest


def _effect_tuple(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    resource_namespace: Mapping[str, Any],
    repository_path: bool,
) -> Tuple[str, ...]:
    raw = _string_tuple(value, field_name, maximum=maximum)
    return tuple(
        sorted(
            {
                _normalize_effect(
                    item,
                    field_name,
                    resource_namespace=resource_namespace,
                    repository_path=repository_path,
                )
                for item in raw
            }
        )
    )


def _normalize_effect(
    value: str,
    field_name: str,
    *,
    resource_namespace: Mapping[str, Any],
    repository_path: bool,
) -> str:
    value = unicodedata.normalize("NFC", value)
    if any(char.isspace() for char in value) or "\x00" in value:
        raise ValidationError(
            "%s effect resources may not contain whitespace" % field_name
        )
    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            raise ValidationError("%s contains an invalid URL effect" % field_name)
        if parsed.username is not None or parsed.password is not None:
            raise ValidationError(
                "%s may not contain credential-bearing URLs" % field_name
            )
        credential_query = re.search(
            r"(?:^|[&;])(?:[^=]*_)?(token|access_token|api_key|key|secret|client_secret|password|credential|signature|sig|x-amz-signature|x-amz-credential|awsaccesskeyid)=",
            parsed.query,
            re.IGNORECASE,
        )
        if credential_query or parsed.fragment:
            raise ValidationError(
                "%s may not contain credential-bearing URLs" % field_name
            )
        path = _normalize_hierarchical_path(
            parsed.path,
            field_name,
            allow_absolute=True,
            unicode_normalization="NFC",
            case_sensitive=True,
        )
        hostname = (parsed.hostname or "").lower()
        netloc = hostname
        try:
            port = parsed.port
        except ValueError:
            raise ValidationError(
                "%s contains an invalid URL effect" % field_name
            ) from None
        if port is not None:
            netloc = "%s:%d" % (hostname, port)
        return urlunsplit(
            (
                parsed.scheme.lower(),
                netloc,
                "/" + path if path else "",
                parsed.query,
                "",
            )
        )
    if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[/\\]", value):
        raise ValidationError(
            "%s effect paths must be repository-relative" % field_name
        )
    return _normalize_hierarchical_path(
        value,
        field_name,
        allow_absolute=False,
        unicode_normalization=str(resource_namespace["unicode_normalization"]),
        case_sensitive=(
            bool(resource_namespace["case_sensitive"]) if repository_path else False
        ),
    )


def _normalize_hierarchical_path(
    value: str,
    field_name: str,
    *,
    allow_absolute: bool,
    unicode_normalization: str,
    case_sensitive: bool,
) -> str:
    normalized = unicodedata.normalize(unicode_normalization, value).replace("\\", "/")
    if not case_sensitive:
        normalized = normalized.casefold()
    if allow_absolute:
        normalized = normalized.lstrip("/")
    parts = []
    for part in normalized.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValidationError("%s effect paths may not contain .." % field_name)
        parts.append(part)
    if not parts:
        raise ValidationError("%s contains an empty effect resource" % field_name)
    return "/".join(parts)


def _resources_overlap(left: str, right: str) -> bool:
    if left == right or left == "*" or right == "*":
        return True
    if left.startswith("repo:") or right.startswith("repo:"):
        return True
    return left.startswith(right.rstrip("/") + "/") or right.startswith(
        left.rstrip("/") + "/"
    )


def _overlapping_resources(
    left: Tuple[str, ...], right: Tuple[str, ...]
) -> Tuple[str, ...]:
    overlaps = set()
    for left_resource in left:
        for right_resource in right:
            if _resources_overlap(left_resource, right_resource):
                if left_resource == right_resource:
                    overlaps.add(left_resource)
                else:
                    overlaps.add(
                        "%s~%s" % tuple(sorted((left_resource, right_resource)))
                    )
    return tuple(sorted(overlaps))


def _node_contract_payload(node: WorkPackageNodeSpec) -> JsonDict:
    data = node.to_dict()
    for field_name in (
        "node_key",
        "depends_on",
        "external_dependencies",
        "input_lineage_status",
        "carry_forward_eligible",
        "effects_digest",
        "contract_digest",
        "input_digest",
    ):
        data.pop(field_name, None)
    return data


def _digest(value: Any) -> str:
    return "%s:%s" % (
        WORK_PACKAGE_PLAN_DIGEST_ALGORITHM,
        hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest(),
    )


def _parse_contract_items(raw: Any, *, path: str) -> Tuple[JsonDict, ...]:
    return _parse_outputs(raw, path=path)


def _parse_external_dependencies(raw: Any, *, path: str) -> Tuple[JsonDict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("%s must be a list" % path)
    if len(raw) > WORK_PACKAGE_MAX_DEPENDENCIES:
        raise ValidationError(
            "%s may contain at most %d items" % (path, WORK_PACKAGE_MAX_DEPENDENCIES)
        )
    dependencies: Dict[str, JsonDict] = {}
    digest_fields = (
        "accepted_evidence_digest",
        "output_digest",
        "contract_digest",
    )
    for index, item in enumerate(raw):
        item_path = "%s[%d]" % (path, index)
        if isinstance(item, str):
            task_id = _required_string(item, "%s.task_id" % item_path, maximum=240)
            supplied: Mapping[str, Any] = {}
        elif isinstance(item, Mapping):
            unknown = sorted(set(item) - {"task_id", *digest_fields})
            if unknown:
                raise ValidationError(
                    "%s contains unknown fields: %s" % (item_path, ", ".join(unknown))
                )
            task_id = _required_string(
                item.get("task_id"), "%s.task_id" % item_path, maximum=240
            )
            supplied = item
        else:
            raise ValidationError("%s must be a task id or object" % item_path)
        if task_id in dependencies:
            raise ValidationError("duplicate external dependency task_id: %s" % task_id)
        present = [
            field_name for field_name in digest_fields if supplied.get(field_name)
        ]
        if present and len(present) != len(digest_fields):
            raise ValidationError(
                "%s must supply accepted evidence, output, and contract digests together"
                % item_path
            )
        resolved = len(present) == len(digest_fields)
        dependency: JsonDict = {
            "task_id": task_id,
            "lineage_status": "resolved" if resolved else "unresolved",
            "carry_forward_eligible": resolved,
            "accepted_evidence_digest": None,
            "output_digest": None,
            "contract_digest": None,
        }
        if resolved:
            for field_name in digest_fields:
                dependency[field_name] = _normalize_sha256_digest(
                    supplied[field_name], "%s.%s" % (item_path, field_name)
                )
        dependencies[task_id] = dependency
    return tuple(
        _copy_json_object(dependencies[task_id]) for task_id in sorted(dependencies)
    )


def _parse_estimates(
    raw: Any, *, legacy_scope_confidence: Any, path: str
) -> Tuple[JsonDict, float]:
    value = _json_object(raw, path)
    unknown = sorted(set(value) - {"duration_seconds", "cost_units", "confidence"})
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    if legacy_scope_confidence is not None and "confidence" in value:
        raise ValidationError(
            "%s cannot combine confidence with legacy scope_confidence" % path
        )
    if legacy_scope_confidence is not None:
        confidence_value = _bounded_float(
            legacy_scope_confidence,
            "%s.confidence" % path,
            minimum=0.0,
            maximum=1.0,
        )
        confidence = (
            "high"
            if confidence_value >= 0.75
            else "medium"
            if confidence_value >= 0.5
            else "low"
        )
    else:
        confidence = str(value.get("confidence") or "low").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            raise ValidationError("%s.confidence must be low, medium, or high" % path)
        confidence_value = {"low": 0.25, "medium": 0.6, "high": 0.9}[confidence]
    return (
        {
            "duration_seconds": _optional_nonnegative_number(
                value.get("duration_seconds"), "%s.duration_seconds" % path
            ),
            "cost_units": _optional_nonnegative_number(
                value.get("cost_units"), "%s.cost_units" % path
            ),
            "confidence": confidence,
        },
        confidence_value,
    )


def _parse_rework(raw: Any, *, legacy_max_attempts: Any, path: str) -> JsonDict:
    value = _json_object(raw, path)
    unknown = sorted(set(value) - {"max_cycles"})
    if unknown:
        raise ValidationError(
            "%s contains unknown fields: %s" % (path, ", ".join(unknown))
        )
    if legacy_max_attempts is not None:
        attempts = _bounded_int(
            legacy_max_attempts,
            "%s.max_attempts" % path,
            minimum=1,
            maximum=11,
        )
        max_cycles = attempts - 1
    else:
        max_cycles = _bounded_int(
            value.get("max_cycles", 1),
            "%s.max_cycles" % path,
            minimum=0,
            maximum=10,
        )
    return {"max_cycles": max_cycles}


def _optional_nonnegative_number(value: Any, field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError("%s must be a non-negative number or null" % field_name)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValidationError(
            "%s must be a non-negative number or null" % field_name
        ) from None
    if not math.isfinite(parsed) or parsed < 0:
        raise ValidationError("%s must be a non-negative number or null" % field_name)
    return parsed


def _parse_outputs(raw: Any, *, path: str) -> Tuple[JsonDict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValidationError("%s must be a list" % path)
    if len(raw) > WORK_PACKAGE_MAX_OUTPUTS:
        raise ValidationError(
            "%s may contain at most %d items" % (path, WORK_PACKAGE_MAX_OUTPUTS)
        )
    outputs: Dict[str, JsonDict] = {}
    for index, item in enumerate(raw):
        item_path = "%s[%d]" % (path, index)
        if isinstance(item, str):
            output: JsonDict = {
                "name": _required_string(item, "%s.name" % item_path, maximum=240),
                "kind": "artifact",
                "required": True,
                "metadata": {},
            }
        elif isinstance(item, Mapping):
            output = {
                "name": _required_string(
                    item.get("name"), "%s.name" % item_path, maximum=240
                ),
                "kind": _required_string(
                    item.get("kind") or "artifact",
                    "%s.kind" % item_path,
                    maximum=120,
                ),
                "required": _boolean(
                    item.get("required", True), "%s.required" % item_path
                ),
                "metadata": _json_object(
                    item.get("metadata"), "%s.metadata" % item_path
                ),
            }
            if item.get("schema") is not None:
                output["schema"] = _required_string(
                    item.get("schema"), "%s.schema" % item_path, maximum=500
                )
            unknown = sorted(
                set(item) - {"name", "kind", "required", "schema", "metadata"}
            )
            if unknown:
                raise ValidationError(
                    "%s contains unknown fields: %s" % (item_path, ", ".join(unknown))
                )
        else:
            raise ValidationError("%s must be a string or object" % item_path)
        name = str(output["name"])
        if name in outputs:
            raise ValidationError("duplicate expected output name: %s" % name)
        outputs[name] = output
    return tuple(_copy_json_object(outputs[name]) for name in sorted(outputs))


def _topological_order(nodes: Mapping[str, WorkPackageNodeSpec]) -> Tuple[str, ...]:
    dependents: Dict[str, List[str]] = {key: [] for key in nodes}
    indegree = {key: 0 for key in nodes}
    for node in nodes.values():
        indegree[node.node_key] = len(node.depends_on)
        for dependency in node.depends_on:
            dependents[dependency].append(node.node_key)
    ready = [key for key, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order: List[str] = []
    while ready:
        key = heapq.heappop(ready)
        order.append(key)
        for dependent in sorted(dependents[key]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) != len(nodes):
        cyclic = sorted(key for key, count in indegree.items() if count > 0)
        raise ValidationError(
            "work package plan contains a dependency cycle involving: %s"
            % ", ".join(cyclic)
        )
    return tuple(order)


def _ancestor_sets(
    nodes: Mapping[str, WorkPackageNodeSpec], order: Tuple[str, ...]
) -> Dict[str, set[str]]:
    ancestors: Dict[str, set[str]] = {}
    for key in order:
        node_ancestors: set[str] = set()
        for dependency in nodes[key].depends_on:
            node_ancestors.add(dependency)
            node_ancestors.update(ancestors[dependency])
        ancestors[key] = node_ancestors
    return ancestors


def _validate_flat_mutation_wave(
    nodes: Mapping[str, WorkPackageNodeSpec], order: Tuple[str, ...]
) -> None:
    """Reject mutation bases that the current executor cannot reproduce safely.

    Worker assignments are pinned to the epoch planning base.  Until a
    controller-owned composition station can create and attest an immutable
    predecessor base before claim, a mutation must not consume any earlier
    mutation or integration candidate.  The check is transitive so an
    analysis/certification node cannot hide an unsupported repository lineage.
    """

    ancestors = _ancestor_sets(nodes, order)
    for key in order:
        node = nodes[key]
        if node.node_type != "mutation":
            continue
        repository_ancestors = sorted(
            ancestor
            for ancestor in ancestors[key]
            if nodes[ancestor].node_type in {"mutation", "integration"}
        )
        if not repository_ancestors:
            continue
        detail = ", ".join(
            "%s (%s)" % (ancestor, nodes[ancestor].node_type)
            for ancestor in repository_ancestors
        )
        raise ValidationError(
            "work package flat mutation wave cannot compose a predecessor base: "
            "mutation node %s has repository-producing ancestor(s): %s; every "
            "mutation must execute from the epoch planning base" % (key, detail)
        )


def _validate_integration_fan_in(
    nodes: Mapping[str, WorkPackageNodeSpec], order: Tuple[str, ...]
) -> None:
    downstream_mutations = {key: False for key in nodes}
    ancestors = _ancestor_sets(nodes, order)
    for node in nodes.values():
        if node.node_type != "mutation":
            continue
        for ancestor in ancestors[node.node_key]:
            if nodes[ancestor].node_type == "mutation":
                downstream_mutations[ancestor] = True
    mutation_leaves = sorted(
        node.node_key
        for node in nodes.values()
        if node.node_type == "mutation" and not downstream_mutations[node.node_key]
    )
    if len(mutation_leaves) <= 1:
        return
    for node in nodes.values():
        if (
            node.node_type == "integration"
            and set(mutation_leaves) <= ancestors[node.node_key]
        ):
            return
    raise ValidationError(
        "work package plan with multiple mutation leaves requires an integration "
        "fan-in node covering: %s" % ", ".join(mutation_leaves)
    )


def _validate_external_waves(
    nodes: Mapping[str, WorkPackageNodeSpec], order: Tuple[str, ...]
) -> None:
    ancestors = _ancestor_sets(nodes, order)
    external_nodes = [node for node in nodes.values() if node.effects.external]
    for index, left in enumerate(external_nodes):
        for right in external_nodes[index + 1 :]:
            ordered = (
                left.node_key in ancestors[right.node_key]
                or right.node_key in ancestors[left.node_key]
            )
            if ordered:
                continue
            conflicts = work_package_effect_conflicts(left.effects, right.effects)
            if any(conflict.startswith("external:") for conflict in conflicts):
                raise ValidationError(
                    "incompatible external effects must be dependency-ordered: %s, %s"
                    % (left.node_key, right.node_key)
                )


def _topological_levels(nodes: Tuple[WorkPackageNodeSpec, ...]) -> JsonDict:
    levels: Dict[str, int] = {}
    for node in nodes:
        levels[node.node_key] = (
            0
            if not node.depends_on
            else 1 + max(levels[dependency] for dependency in node.depends_on)
        )
    return {key: levels[key] for key in sorted(levels)}


def _critical_path_ranks(
    nodes: Tuple[WorkPackageNodeSpec, ...], order: Tuple[str, ...]
) -> JsonDict:
    by_key = {node.node_key: node for node in nodes}
    dependents: Dict[str, List[str]] = {key: [] for key in by_key}
    for node in nodes:
        for dependency in node.depends_on:
            dependents[dependency].append(node.node_key)
    ranks: Dict[str, float] = {}
    for key in reversed(order):
        duration = by_key[key].estimates.get("duration_seconds")
        own = float(duration) if duration is not None else 1.0
        ranks[key] = own + max(
            (ranks[dependent] for dependent in dependents[key]), default=0.0
        )
    return {key: ranks[key] for key in sorted(ranks)}


def _conflict_domains(nodes: Tuple[WorkPackageNodeSpec, ...]) -> JsonDict:
    result: JsonDict = {}
    for node in nodes:
        domains = []
        for effect_kind in ("reads", "writes", "exclusive", "external"):
            for resource in getattr(node.effects, effect_kind):
                domains.append("%s:%s" % (effect_kind, resource))
        result[node.node_key] = sorted(domains)
    return {key: result[key] for key in sorted(result)}


def _integration_groups(nodes: Tuple[WorkPackageNodeSpec, ...]) -> Tuple[JsonDict, ...]:
    by_key = {node.node_key: node for node in nodes}
    groups = []
    for node in nodes:
        if node.node_type != "integration":
            continue
        frontier = set()
        pending = list(node.depends_on)
        while pending:
            key = pending.pop()
            dependency = by_key[key]
            if dependency.node_type in {"mutation", "integration"}:
                frontier.add(key)
            else:
                pending.extend(dependency.depends_on)
        groups.append(
            {
                "integration_node_key": node.node_key,
                "member_node_keys": sorted(frontier),
                "capacity_scope": "integration:%s" % node.node_key,
            }
        )
    return tuple(groups)


def _capacity_scopes(nodes: Tuple[WorkPackageNodeSpec, ...]) -> JsonDict:
    result: JsonDict = {}
    for node in nodes:
        mutation_resources = sorted(
            set(node.effects.writes)
            | set(node.effects.exclusive)
            | set(node.effects.external)
        )
        result[node.node_key] = {
            "execution": ["work_package_v1"],
            "mutation_wip": mutation_resources if node.node_type == "mutation" else [],
            "integration": (
                ["integration:%s" % node.node_key]
                if node.node_type == "integration"
                else []
            ),
        }
    return {key: result[key] for key in sorted(result)}


def _json_object(value: Any, field: str) -> JsonDict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("%s must be an object" % field)
    canonical = _canonical_json_value(dict(value), field)
    assert isinstance(canonical, dict)
    return canonical


def _canonical_json_value(value: Any, field: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("%s must contain only finite numbers" % field)
        return value
    if isinstance(value, Mapping):
        result: JsonDict = {}
        if any(not isinstance(key, str) for key in value):
            raise ValidationError("%s object keys must be strings" % field)
        for key in sorted(value):
            result[key] = _canonical_json_value(value[key], "%s.%s" % (field, key))
        return result
    if isinstance(value, (list, tuple)):
        return [
            _canonical_json_value(item, "%s[%d]" % (field, index))
            for index, item in enumerate(value)
        ]
    raise ValidationError("%s must contain only JSON values" % field)


def _copy_json_object(value: Mapping[str, Any]) -> JsonDict:
    # Definitions are already validated JSON. A canonical round-trip also
    # prevents callers from mutating nested compiler state through to_dict().
    return json.loads(json_dumps(value))


def _required_string(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError("%s must be a string" % field)
    text = value.strip()
    if not text:
        raise ValidationError("%s is required" % field)
    if len(text) > maximum:
        raise ValidationError("%s may contain at most %d characters" % (field, maximum))
    return text


def _optional_string(value: Any, field: str, *, maximum: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("%s must be a string" % field)
    text = value.strip()
    if len(text) > maximum:
        raise ValidationError("%s may contain at most %d characters" % (field, maximum))
    return text or None


def _string_tuple(value: Any, field: str, *, maximum: int) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValidationError("%s must be a list" % field)
    if len(value) > maximum:
        raise ValidationError("%s may contain at most %d items" % (field, maximum))
    items = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValidationError("%s[%d] must be a string" % (field, index))
        text = item.strip()
        if not text:
            raise ValidationError("%s[%d] may not be empty" % (field, index))
        if len(text) > 1000:
            raise ValidationError(
                "%s[%d] may contain at most 1000 characters" % (field, index)
            )
        items.add(text)
    return tuple(sorted(items))


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError("%s must be an integer" % field)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be an integer" % field) from None
    if isinstance(value, float) and not value.is_integer():
        raise ValidationError("%s must be an integer" % field)
    if parsed < minimum or parsed > maximum:
        raise ValidationError(
            "%s must be between %d and %d" % (field, minimum, maximum)
        )
    return parsed


def _bounded_float(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValidationError("%s must be a number" % field)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % field) from None
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValidationError(
            "%s must be between %s and %s" % (field, minimum, maximum)
        )
    return parsed


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("%s must be a boolean" % field)
    return value


__all__ = [
    "CompiledWorkPackagePlan",
    "WORK_PACKAGE_PLAN_DIGEST_ALGORITHM",
    "WORK_PACKAGE_PLAN_SCHEMA",
    "WorkPackageEffects",
    "WorkPackageNodeSpec",
    "compile_work_package_plan",
    "work_package_effect_conflicts",
]
